"""微信 WPP 平台适配器 (WeChatPadPro)。

AstrBot 平台适配器插件范式 (docs/zh/dev/plugin-platform-adapter.md):
  1. main.py 必须有 Star 类 (否则 star_manager 拒绝加载)
  2. 适配器放独立文件, 用 @register_platform_adapter 装饰
  3. main.py __init__ 里 import 适配器模块触发注册

契约 (已从 WPP 插件源码 + swagger 核实):
  - WPP basePath = /api, 鉴权: X-Access-Token header + authcode query
  - 文本发送: POST /api/Msg/SendTxt  body={ToWxid, Content, At, Type:1}
  - 图片发送: POST /api/Msg/UploadImg  body={ToWxid, Base64}
  - 消息 push: vendor WS v2 `{Data:{type:"sync_message", data:{messages:[...]}}}`
  - 消息类型数字: 1=text 3=image 6=file 34=voice 43=video 47=emoji 49=app 51=relay 10002=revoke

账号模型 (v0.10.0):
  - 一个平台实例 = 一个 `wpp_auth_token` (X-Access-Token = authcode) = 一个微信账号
  - 多账号 = 多个平台实例 (id 唯一, 各自 authcode), 不支持单实例内挂多个账号
  - 同一个 authcode 只允许一个实例轮询, 否则两个 WS 会互抢消息
  - WS 是主通道; webhook 保留为兼容兜底 (wpp_webhook_enabled=True)
"""

import asyncio
import hashlib
import hmac
import json
import secrets
from typing import Any

from astrbot import logger
from astrbot.api.event import MessageChain
from astrbot.api.platform import (
    Platform,
    PlatformMetadata,
    register_platform_adapter,
)

# 抑制第三方库的 DEBUG 噪音日志。
# AstrBot v4 把 root logger 强制设为 DEBUG, 且只压了 aiosqlite/filelock/asyncio/tzlocal/apscheduler,
# 漏压了 websockets (WPP 插件 WS 客户端) 和 httpcore (HTTP 调用)。
# 这些库的 DEBUG 会在每次 WS PING/PONG/TEXT 帧和 HTTP 请求时刷屏, 严重影响可读性。
# 局部修复: 在插件加载时把相关 logger 压到 WARNING (只影响第三方库, 不影响 AstrBot/插件自身 logger)。
import logging as _logging

for _noisy in ("websockets", "websockets.client", "websockets.protocol", "httpcore", "httpcore._trace"):
    _logging.getLogger(_noisy).setLevel(_logging.WARNING)

from astrbot.core.platform.message_session import MessageSession

from .wpp_account import WppAccount
from .wpp_event import WppMessageEvent
from .wpp_heartflow import (
    HEARTFLOW_WHITELIST_MODES,
    resolve_heartflow_groups,
    sync_heartflow_whitelist as sync_heartflow_config,
)
from .wpp_messages import extract_all_msg_src, safe_str, split_users
from .wpp_send import send_chain
from .wpp_storage import plugin_data_dir, safe_account_id


def _as_csv(value: Any) -> set[str]:
    """配置值 (list 或逗号串) → wxid/群 ID 集合。"""
    if isinstance(value, (list, tuple, set)):
        return {str(v).strip() for v in value if str(v).strip()}
    return split_users(str(value or ""))


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _as_int(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _parse_account(platform_config: dict) -> dict | None:
    """解析当前平台实例的唯一账号。

    一个平台实例 = 一个 `wpp_auth_token` (X-Access-Token = authcode) =
    一个微信账号。多账号请添加多个平台实例 (id 唯一, 各自 authcode)。
    返回 {id, authcode, allow_users, group_reply, blacklist_groups,
          require_at_mention, proactive_groups, base_url, ws_url}；
    未配置 authcode 时返回 None。
    """
    base_url = str(platform_config.get("wpp_base_url", "http://127.0.0.1:18062")).rstrip("/")
    ws_url = str(platform_config.get("wpp_ws_url", "") or "").strip()

    # 旧版单实例多账号配置已移除。静默忽略会把账号"吞掉"，这里明确告警，
    # 提示按实例拆分；不输出 authcode/旧账号内容。
    if str(platform_config.get("wpp_accounts", "") or "").strip():
        logger.warning(
            "[WPP] wpp_accounts 多账号配置已移除：一个平台实例只能对应一个 "
            "authcode/微信账号。请为每个账号各添加一个「微信 WPP」平台实例，"
            "id 唯一并分别填写 wpp_auth_token。"
        )

    authcode = str(platform_config.get("wpp_auth_token", "") or "").strip()
    if not authcode:
        return None
    return {
        "id": str(platform_config.get("id", "wpp") or "wpp"),
        "authcode": authcode,
        "allow_users": _as_csv(platform_config.get("wpp_allow_users", "")),
        "group_reply": str(platform_config.get("wpp_group_reply", "atbot") or "atbot"),
        "blacklist_groups": set(),
        "block_users": _as_csv(platform_config.get("wpp_block_users", "")),
        "require_at_mention": False,
        "proactive_groups": _as_csv(platform_config.get("wpp_proactive_groups", "")),
        "base_url": base_url,
        "ws_url": ws_url,
    }


def authcode_fingerprint(authcode: str) -> str:
    """authcode 锁标识 (不落盘/不打印明文)。"""
    return hashlib.sha256(str(authcode).encode("utf-8")).hexdigest()[:12]


# Dashboard WebUI 表单配置元数据 (config_metadata)
WPP_CONFIG_METADATA = {
    "wpp_base_url": {
        "description": "WPP API 接口地址",
        "type": "string",
        "hint": "宿主机IP:18062 (wechatpadpromax API; 18080是mitmdump代理非API)。AstrBot容器用宿主IP可达, 勿用容器名(DNS不通)",
    },
    "wpp_auth_token": {
        "description": "微信访问凭证 (X-Access-Token = authcode)",
        "type": "string",
        "secret": True,
        "hint": "一个平台实例只能填一个 authcode，对应一个微信账号。多账号请再添加一个「微信 WPP」平台实例，id 唯一、各自填 authcode。",
    },
    "wpp_allow_users": {
        "description": "白名单 wxid / 群ID (逗号分隔)",
        "type": "string",
        "hint": "wxid 控制私聊, @chatroom 群ID 同时作为群白名单和主动回复候选（与白名单共用）。作为启动初始值；日常修改请在 WPP 控制台进行。",
    },
    "wpp_block_users": {
        "description": "私聊黑名单 wxid (逗号分隔)",
        "type": "string",
        "hint": "全部放行模式下用它排除特定 wxid (私聊/群聊都生效)。公众号/微信团队/微信支付/服务通知等系统消息已内置静默, 无需填写。作为启动初始值；日常修改请在 WPP 控制台进行",
    },
    "wpp_group_reply": {
        "description": "群消息回复模式",
        "type": "string",
        "hint": "atbot=只放行@ / none=忽略群 / all=放行所有群消息(观察模式, 不强制回复)。作为启动初始值；日常修改请在 WPP 控制台进行",
    },
    "wpp_proactive_groups": {
        "description": "额外主动回复候选群 (逗号分隔群ID)",
        "type": "string",
        "hint": "群白名单 (wpp_allow_users 中的 @chatroom) 已自动作为主动候选, 通常无需填写; 此项只用于未配置群白名单时显式补充。未@消息只放行不唤醒, 由 Heartflow/active_reply 决定是否接话。作为启动初始值；日常修改请在 WPP 控制台进行。",
    },
    "wpp_sync_heartflow_whitelist": {
        "description": "群白名单自动同步到 Heartflow",
        "type": "bool",
        "hint": "默认 true。把各账号的群白名单 + 主动候选群同步到 Heartflow 的 chat_whitelist (只替换本平台的 UMO 条目, 保留其他平台条目), 写盘并实时更新已加载实例。启动/策略变更/热更新即时同步, 并每 5 分钟自愈一次。未安装 Heartflow 时自动跳过。",
    },
    "wpp_heartflow_whitelist_mode": {
        "description": "Heartflow 白名单来源",
        "type": "string",
        "hint": 'auto=参考 WPP 群白名单 (默认); custom=用下方 wpp_heartflow_groups 单独设置。custom 中的群必须在群白名单 (allow_users 的 @chatroom) 中, 否则会被拒绝并告警。',
    },
    "wpp_heartflow_groups": {
        "description": "单独设置的 Heartflow 群白名单 (custom 模式)",
        "type": "string",
        "hint": "逗号分隔群 ID, 仅 wpp_heartflow_whitelist_mode=custom 时生效。群白名单必须包含这些群, 适配器才会放行未@消息。",
    },
    "wpp_lazy_media": {
        "description": "未@群消息媒体懒加载",
        "type": "bool",
        "hint": "默认 true。主动候选群/观察模式下, 图片/文件/视频只在真正调用 LLM 前下载。若启用了内置 group_icl_enable / group_message_history_enable (需要在事件创建时拿到真实媒体), 请设为 false 改为即时下载。",
    },
    "wpp_send_interval_ms": {
        "description": "同一会话发送最小间隔 (毫秒)",
        "type": "int",
        "hint": "默认 300。同一会话的多分片/多事件回复会串行并保持该间隔, 降低微信风控概率; 0=不节流。",
    },
    "wpp_outbound_repeat_guard": {
        "description": "出站复读保护",
        "type": "bool",
        "hint": "默认 true。同一会话相同文本在窗口内超过次数上限后跳过, 防止 LLM 复读刷屏。",
    },
    "wpp_outbound_repeat_window": {
        "description": "复读保护窗口 (秒)",
        "type": "int",
        "hint": "默认 300 (5 分钟)。",
    },
    "wpp_outbound_repeat_limit": {
        "description": "复读保护次数上限",
        "type": "int",
        "hint": "默认 2。窗口内第 3 条相同文本开始跳过 (前两条正常发送, 避免误杀正常重复回答)。",
    },
    "wpp_ws_url": {
        "description": "WS 实时消息通道地址 (vendor /ws/sync)",
        "type": "string",
        "hint": "如 wss://<vendor-domain>/ws/sync (vendor 的 WebSocket 入口)。WS 连接建立即推全量离线+实时消息, 是主通道",
    },
    "wpp_webhook_enabled": {
        "description": "是否启用 webhook 兜底通道",
        "type": "bool",
        "hint": "默认 false=仅 WS (推荐)。true=额外开 webhook server + vendor 注册 (WS 断时 webhook 也收不到, 实际无用)",
    },
}


@register_platform_adapter(
    "wpp",
    "微信 WPP 适配器 (WeChatPadPro)",
    adapter_display_name="微信 WPP",
    logo_path="logo.png",
    default_config_tmpl={
        "wpp_base_url": "http://127.0.0.1:18062",
        "wpp_auth_token": "",
        "wpp_allow_users": "",
        "wpp_block_users": "",
        "wpp_group_reply": "atbot",
        "wpp_proactive_groups": "",
        "wpp_sync_heartflow_whitelist": True,
        "wpp_heartflow_whitelist_mode": "auto",
        "wpp_heartflow_groups": "",
        "wpp_lazy_media": True,
        "wpp_send_interval_ms": 300,
        "wpp_outbound_repeat_guard": True,
        "wpp_outbound_repeat_window": 300,
        "wpp_outbound_repeat_limit": 2,
        "wpp_ws_url": "",
        "wpp_webhook_enabled": False,
    },
    config_metadata=WPP_CONFIG_METADATA,
)
class WppPlatformAdapter(Platform):
    """一个平台实例 = 一个 authcode = 一个微信账号。

    平台级职责: 账号解析/生命周期/meta/create_event/send_by_session/webhook 兜底。
    账号级职责 (WppAccount): WS/队列/消息处理/白名单/filehelper/去重。
    多账号 = 多个平台实例 (id 唯一, 各自 authcode)。
    """
    #: authcode 指纹 → 持有它的实例 id；同一 authcode 只允许一个实例轮询。
    _ACTIVE_AUTHCODES: dict[str, str] = {}

    def __init__(
        self,
        platform_config: dict,
        platform_settings: dict,
        event_queue: asyncio.Queue,
    ) -> None:
        super().__init__(platform_config, event_queue)
        self.config = platform_config
        self.settings = platform_settings

        # 实例标识 (平台级, AstrBot 会话路由用)
        self.instance_id = str(platform_config.get("id", "wpp") or "wpp")

        self.base_url = str(platform_config.get("wpp_base_url", "http://127.0.0.1:18062")).rstrip("/")
        self.ws_url = str(platform_config.get("wpp_ws_url", "") or "").strip()
        self.webhook_enabled = _as_bool(platform_config.get("wpp_webhook_enabled"), False)
        # 群白名单自动同步到 Heartflow 的 chat_whitelist (可选集成, 默认开)
        self.heartflow_sync_enabled = _as_bool(
            platform_config.get("wpp_sync_heartflow_whitelist"), True
        )
        hf_mode = str(
            platform_config.get("wpp_heartflow_whitelist_mode", "auto") or "auto"
        ).strip().lower()
        if hf_mode not in HEARTFLOW_WHITELIST_MODES:
            logger.warning(
                f"[WPP] 未知 Heartflow 白名单模式 {hf_mode!r}, 回退 auto"
            )
            hf_mode = "auto"
        self.heartflow_whitelist_mode = hf_mode
        self.heartflow_groups = _as_csv(
            platform_config.get("wpp_heartflow_groups", "")
        )
        self._heartflow_rejected_warned: set[str] = set()
        self.lazy_media = _as_bool(platform_config.get("wpp_lazy_media"), True)
        self.send_interval_ms = max(
            0, _as_int(platform_config.get("wpp_send_interval_ms"), 300)
        )
        self.outbound_repeat_guard = _as_bool(
            platform_config.get("wpp_outbound_repeat_guard"), True
        )
        self.outbound_repeat_window = max(
            0, _as_int(platform_config.get("wpp_outbound_repeat_window"), 300)
        )
        self.outbound_repeat_limit = max(
            1, _as_int(platform_config.get("wpp_outbound_repeat_limit"), 2)
        )
        self._heartflow_all_warned = False

        # 缓存平台元数据 (AstrBot 每条消息都会取, 不要每次重建)
        self.metadata = PlatformMetadata(
            name="wpp",
            description="微信 WPP 适配器 (WeChatPadPro)",
            id=self.instance_id,
            adapter_display_name="微信 WPP",
            logo_path="logo.png",
            # WPP 不支持流式消息 (vendor 无消息编辑能力)
            support_streaming_message=False,
            support_proactive_message=True,
            module_path=__name__,
            config_metadata=WPP_CONFIG_METADATA,
        )

        # 一个实例只解析/创建一个账号：authcode 与微信账号一一对应。
        acfg = _parse_account(platform_config)
        self.account: WppAccount | None = None
        if acfg:
            self.account = WppAccount(
                platform=self,
                account_id=acfg["id"],
                authcode=acfg["authcode"],
                base_url=acfg["base_url"],
                ws_url=acfg["ws_url"],
                allow_users=acfg.get("allow_users") or set(),
                group_reply=acfg.get("group_reply", "atbot"),
                blacklist_groups=acfg.get("blacklist_groups") or set(),
                block_users=acfg.get("block_users") or set(),
                require_at_mention=acfg.get("require_at_mention", False),
                proactive_groups=acfg.get("proactive_groups") or set(),
                lazy_media=self.lazy_media,
                send_interval_ms=self.send_interval_ms,
                repeat_guard_window=(
                    float(self.outbound_repeat_window)
                    if self.outbound_repeat_guard
                    else 0.0
                ),
                repeat_guard_limit=self.outbound_repeat_limit,
            )
            logger.info(
                f"[WPP] 账号已注册: id={self.account.account_id} "
                f"base={self.account.base_url} "
                f"ws={'有' if self.account.ws_url else '无'}"
            )

        # webhook 兜底 (单实例一个 server)
        self.webhook_path = str(platform_config.get("webhook_path", "/wpp/webhook")).rstrip("/")
        self.webhook_token = str(platform_config.get("webhook_token", "") or "").strip()
        if not self.webhook_token:
            self.webhook_token = _webhook_token_for(self.instance_id)
        self.webhook_path = f"{self.webhook_path}/{self.webhook_token}"
        self.webhook_secret = str(platform_config.get("webhook_secret", "") or "").strip()
        self.webhook_host = str(platform_config.get("webhook_host", "0.0.0.0"))
        self.webhook_port = int(platform_config.get("webhook_port", 6199))
        self.webhook_public_url = str(platform_config.get("webhook_public_url", "") or "").rstrip("/")

        self._runner = None
        self._server = None
        self._status_task: asyncio.Task | None = None

    # ------------------------------------------------------------------ Platform
    def meta(self) -> PlatformMetadata:
        return self.metadata

    @property
    def accounts(self) -> list[WppAccount]:
        """控制台/命令按 accounts 列表展示；单实例恒为 0 或 1 个账号。"""
        return [self.account] if self.account is not None else []

    def create_event(self, message) -> WppMessageEvent:
        """AstrBot 标准事件工厂 (适配器基类约定)。"""
        acct = self.account
        return WppMessageEvent(
            message_str=message.message_str,
            message_obj=message,
            platform_meta=self.meta(),
            session_id=message.session_id,
            api=acct._api if acct else None,
            account_id=acct.account_id if acct else "",
        )

    def get_stats(self) -> dict:
        stats = super().get_stats()
        stats["accounts"] = [self.account.stats] if self.account else []
        if self.account:
            stats["meta"]["description"] = f"微信 WPP · {self.account.account_id}"
        return stats

    # ------------------------------------------------------------------ 生命周期
    async def run(self) -> None:
        """启动当前账号的 worker + WS 实时通道 (+ 可选 webhook 兜底)。"""
        acct = self.account
        if acct is None:
            raise ValueError(
                "WPP: 未配置 wpp_auth_token（X-Access-Token = authcode）。"
                "一个平台实例只能绑定一个微信账号；多账号请添加多个平台实例。"
            )

        # 一个 authcode 同时只能有一个轮询者：两个实例会各自建 WS，互抢消息。
        fp = authcode_fingerprint(acct.authcode)
        owner = self._ACTIVE_AUTHCODES.get(fp)
        if owner and owner != self.instance_id:
            raise RuntimeError(
                f"同一 authcode 已被平台实例 {owner} 使用；"
                "一个 authcode 对应一个微信账号，只能由一个实例轮询"
            )
        self._ACTIVE_AUTHCODES[fp] = self.instance_id
        try:
            # ① 确认在线 + 刷新身份 + 启动 worker/WS/维护循环
            try:
                await self._ensure_logged_in(acct)
                await self._refresh_account_status(acct)
                acct.start()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[WPP:{acct.account_id}] 账号启动失败: {e}")

            # ② 可选: webhook server (仅 wpp_webhook_enabled=true)
            if self.webhook_enabled:
                await self._start_webhook_server()

            # ③ 后台状态循环 (检查在线 + 心跳)
            self._status_task = asyncio.create_task(self._account_status_loop())

            # ④ 启动时同步 Heartflow 白名单 (此时各插件均已加载)
            self.sync_heartflow_whitelist("startup")
        except BaseException:
            self._ACTIVE_AUTHCODES.pop(fp, None)
            raise

    def sync_heartflow_whitelist(self, reason: str = "") -> None:
        """把本实例所有账号的群白名单/主动候选群同步给 Heartflow。

        Heartflow 的 chat_whitelist 是全局配置: 只替换本平台实例的 UMO 条目,
        其他平台条目保持不变。同步失败只记日志, 不影响消息收发。
        """
        if not getattr(self, "heartflow_sync_enabled", True):
            return
        try:
            groups, rejected = resolve_heartflow_groups(
                self.accounts,
                getattr(self, "heartflow_groups", set()),
                getattr(self, "heartflow_whitelist_mode", "auto"),
            )
            if rejected and rejected != self._heartflow_rejected_warned:
                self._heartflow_rejected_warned = set(rejected)
                logger.warning(
                    "[WPP] Heartflow 白名单要求群白名单必须包含它们; 以下群不在 "
                    f"allow_users 群白名单, 已跳过同步: {', '.join(sorted(rejected))}。"
                    "请先 /group add 或从 Heartflow 单独白名单移除。"
                )
            if (
                not self._heartflow_all_warned
                and any(getattr(a, "group_reply", "") == "all" for a in self.accounts)
            ):
                self._heartflow_all_warned = True
                logger.warning(
                    "[WPP] group_reply=all 会放行所有群, 但 Heartflow 白名单只能覆盖"
                    "显式列出的 allow_users 群 / proactive_groups; 群白名单中的"
                    "@chatroom 会自动作为主动候选并同步心流, 无需重复配置。"
                )
            sync_heartflow_config(self.instance_id, groups, reason)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[WPP] 同步 Heartflow 白名单失败: {e}")

    async def _start_webhook_server(self) -> None:
        """启动 webhook server + 注册 vendor (webhook 已退役, 仅向后兼容)。"""
        from aiohttp import web

        app = web.Application()
        app.router.add_post(self.webhook_path, self._handle_webhook)
        app.router.add_get(self.webhook_path, self._handle_webhook_health)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._server = web.TCPSite(self._runner, self.webhook_host, self.webhook_port)
        await self._server.start()
        logger.info(
            f"[WPP] webhook server started at "
            f"http://{self.webhook_host}:{self.webhook_port}{self.webhook_path}"
        )

        if not self.accounts:
            return
        # 用第一个账号的 api 注册 webhook (best-effort)
        try:
            if self.webhook_public_url:
                callback_url = self.webhook_public_url + self.webhook_path
            else:
                callback_url = f"http://{self.webhook_host}:{self.webhook_port}{self.webhook_path}"
            api = self.accounts[0]._api
            ok = await api.set_webhook(callback_url)
            logger.info(f"[WPP] register webhook to vendor: {'OK' if ok else 'FAILED'} url={callback_url}")
            biz = await api.set_business_webhook(callback_url)
            sync = await api.start_auto_sync(callback_url)
            logger.info(f"[WPP] business webhook: {biz} | auto-sync: {sync}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[WPP] register webhook to vendor failed: {e}")

    async def _account_status_loop(self) -> None:
        """后台循环: 每 30s 遍历所有账号检查在线; 不在线但已登录 → 自动心跳拉起。"""
        while True:
            try:
                await asyncio.sleep(30)
                for acct in self.accounts:
                    try:
                        online, resp = await acct._api.is_online()
                        if online:
                            await self._refresh_account_status(acct)
                            continue
                        if resp.get("Code") == 0:
                            hb = await acct._api.auto_heartbeat()
                            logger.info(
                                f"[WPP:{acct.account_id}] 长连接已断, 触发自动心跳: {hb.get('Message')}"
                            )
                            await asyncio.sleep(2)
                            online2, _ = await acct._api.is_online()
                            if online2:
                                logger.info(f"[WPP:{acct.account_id}] 自动心跳拉起成功")
                                await self._refresh_account_status(acct)
                        else:
                            logger.warning(
                                f"[WPP:{acct.account_id}] 账号未登录 (Code={resp.get('Code')})"
                            )
                    except Exception as e:  # noqa: BLE001
                        logger.warning(f"[WPP:{acct.account_id}] 保活检查失败: {e}")
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[WPP] 保活循环异常: {e}")

    async def _refresh_account_status(self, acct: WppAccount) -> None:
        """查账号在线状态 + 个人资料, 更新 acct._account_status / 身份。"""
        try:
            online, resp = await acct._api.is_online()
            data = resp.get("Data") or {}
            wxid = safe_str(data.get("wxid"))
            if wxid:
                acct._self_wxid = wxid
            if online:
                prof = None
                try:
                    prof = await acct._api.get_contract_profile()
                except Exception:  # noqa: BLE001
                    pass
                nickname = ""
                if prof and prof.get("Success") is not False:
                    ui = (prof.get("Data") or {}).get("userInfo") or {}
                    nv = ui.get("NickName")
                    nickname = str(nv.get("string", "")) if isinstance(nv, dict) else (str(nv) if nv else "")
                if nickname:
                    acct._self_nickname = nickname
                acct._account_status = {
                    "online": True,
                    "wxid": wxid,
                    "nickname": nickname,
                    "message": f"在线 · {nickname} ({wxid})",
                }
            else:
                acct._account_status = {
                    "online": False,
                    "wxid": wxid,
                    "nickname": "",
                    "message": "未登录",
                }
        except Exception as e:  # noqa: BLE001
            acct._account_status = {
                "online": None,
                "wxid": "",
                "nickname": "",
                "message": f"查询失败: {e}",
            }

    async def _ensure_logged_in(self, acct: WppAccount) -> None:
        """确认账号在线 (未登录 → AutoHeartBeat 拉起 → 复查)。"""
        try:
            online, resp = await acct._api.is_online()
            if online:
                data = resp.get("Data") or {}
                logger.info(f"[WPP:{acct.account_id}] 账号已在线: wxid={data.get('wxid')}")
                return
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[WPP:{acct.account_id}] get_online_info failed: {e}")

        try:
            hb = await acct._api.auto_heartbeat()
            if hb.get("Success") is not False:
                logger.info(f"[WPP:{acct.account_id}] 已触发自动心跳: {hb.get('Message')}")
                await asyncio.sleep(2)
                online, resp = await acct._api.is_online()
                if online:
                    data = resp.get("Data") or {}
                    logger.info(
                        f"[WPP:{acct.account_id}] 心跳后账号已在线: wxid={data.get('wxid')}"
                    )
                    return
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[WPP:{acct.account_id}] auto_heartbeat failed: {e}")

        logger.warning(
            f"[WPP:{acct.account_id}] 账号未在线 (微信未在 vendor 成功建立连接), "
            f"请到 WPP 管理面板检查登录状态: {acct.base_url}"
        )

    async def terminate(self) -> None:
        """清理: 账号 WS/worker + 状态循环 + webhook server + authcode 锁。"""
        try:
            if self._status_task:
                self._status_task.cancel()
                try:
                    await self._status_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
                self._status_task = None
            if self.account is not None:
                try:
                    await self.account.stop()
                except Exception:  # noqa: BLE001
                    pass
            if self._runner:
                await self._runner.cleanup()
                self._runner = None
                self._server = None
        finally:
            if self.account is not None:
                fp = authcode_fingerprint(self.account.authcode)
                if self._ACTIVE_AUTHCODES.get(fp) == self.instance_id:
                    self._ACTIVE_AUTHCODES.pop(fp, None)

    # ------------------------------------------------------------------ webhook 兜底
    async def _handle_webhook_health(self, request: Any) -> Any:
        from aiohttp import web

        return web.Response(text="ok")

    async def _handle_webhook(self, request: Any) -> Any:
        """接收 WPP vendor 消息 push (webhook 已退役, 单实例单账号)。"""
        from aiohttp import web

        req_path = request.path
        if req_path.rstrip("/") != self.webhook_path:
            logger.warning(f"[WPP] webhook path mismatch: {req_path}")
            return web.Response(status=404)

        try:
            raw = await request.text()
            payload = json.loads(raw)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[WPP] bad webhook payload: {e}")
            return web.Response(status=200)

        if self.webhook_secret:
            sig = (
                request.headers.get("X-Signature")
                or request.headers.get("X-Hub-Signature-256")
                or request.headers.get("X-WPP-Signature")
                or ""
            )
            if not sig or not self._verify_webhook_signature(raw, sig, self.webhook_secret):
                logger.warning(f"[WPP] webhook signature verify failed: path={req_path}")
                return web.Response(status=403)

        acct = self.account
        if acct is None:
            return web.Response(status=200)
        for src in extract_all_msg_src(payload):
            try:
                await acct._process_src(src)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[WPP] webhook _process_src failed: {e}")
        return web.Response(status=200)

    @staticmethod
    def _verify_webhook_signature(raw_body: str, signature: str, secret: str) -> bool:
        import hashlib

        algo = "sha256"
        sig_value = signature
        for prefix, a in (("sha256=", "sha256"), ("sha1=", "sha1"), ("md5=", "md5")):
            if signature.startswith(prefix):
                algo = a
                sig_value = signature[len(prefix):]
                break
        try:
            computed = hmac.new(secret.encode(), raw_body.encode(), getattr(hashlib, algo)).hexdigest()
            return hmac.compare_digest(computed, sig_value)
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------------ 发送
    def _find_account_for_send(self, session: MessageSession) -> WppAccount | None:
        """单实例单账号：所有会话都由当前账号发送。"""
        return self.account

    async def send_by_session(
        self, session: MessageSession, message_chain: MessageChain
    ) -> None:
        """AstrBot 回复/主动推送统一走这里 → 对应账号的 WPP 发送。"""
        try:
            acct = self._find_account_for_send(session)
            if acct is None:
                logger.warning(
                    f"[WPP] 当前平台实例未配置 authcode，无法发送到 {session.session_id}"
                )
                return
            logger.info(f"[WPP:{acct.account_id}] send_by_session: to={session.session_id}")
            await send_chain(
                acct._api,
                session.session_id,
                list(message_chain.chain),
                account_id=acct.account_id,
            )
        finally:
            # 基类负责 metric 上报
            await super().send_by_session(session, message_chain)


def _webhook_token_for(instance_id: str) -> str:
    """按实例 id 加载/创建 webhook token (存插件数据目录, 不写插件源码目录)。"""
    import os

    token_dir = plugin_data_dir() / "webhooks"
    token_dir.mkdir(parents=True, exist_ok=True)
    token_file = token_dir / f"{safe_account_id(instance_id)}.token"
    try:
        if token_file.exists():
            tok = token_file.read_text(encoding="utf-8").strip()
            if tok:
                return tok
    except Exception:  # noqa: BLE001
        pass
    tok = secrets.token_hex(16)
    try:
        token_file.write_text(tok, encoding="utf-8")
        os.chmod(token_file, 0o600)
    except Exception:  # noqa: BLE001
        pass
    return tok
