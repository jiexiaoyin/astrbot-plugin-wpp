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

v0.3.0 多账号:
  - 单实例内管理多个微信账号 (WppAccount), 每账号一个 authcode + 独立 WS + 独立白名单
  - WS 是主通道; webhook 保留为兼容兜底 (wpp_webhook_enabled=True)
  - `send_by_session` 按会话→账号绑定路由 (事件收到时由账号登记), 主动推送也能发对账号
"""

import asyncio
import hmac
import json
import secrets
from collections import OrderedDict
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


def _parse_accounts(platform_config: dict) -> list[dict]:
    """解析账号配置列表。

    优先 wpp_accounts (JSON 数组); 为空则回退到单账号 (wpp_auth_token + 顶层白名单)。
    返回 [{id, authcode, allow_users, group_reply, blacklist_groups, require_at_mention, base_url, ws_url}]
    """
    base_url = str(platform_config.get("wpp_base_url", "http://127.0.0.1:18062")).rstrip("/")
    ws_url = str(platform_config.get("wpp_ws_url", "") or "").strip()

    raw = platform_config.get("wpp_accounts", "") or ""
    accounts: list[Any] = []
    if isinstance(raw, str):
        raw = raw.strip()
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    accounts = parsed
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[WPP] wpp_accounts JSON 解析失败, 回退单账号: {e}")
    elif isinstance(raw, list):
        accounts = raw

    if accounts:
        out = []
        for i, acct in enumerate(accounts):
            if not isinstance(acct, dict):
                continue
            aid = str(acct.get("id") or f"account{i}")
            authcode = str(acct.get("authcode") or acct.get("wpp_auth_token") or "").strip()
            if not authcode:
                logger.warning(f"[WPP] 账号 {aid} 缺少 authcode, 已跳过")
                continue
            out.append({
                "id": aid,
                "authcode": authcode,
                "allow_users": _as_csv(acct.get("allow_users", "")),
                "group_reply": str(acct.get("group_reply", "atbot") or "atbot"),
                "blacklist_groups": _as_csv(acct.get("blacklist_groups", "")),
                "block_users": _as_csv(acct.get("block_users", "")),
                "require_at_mention": _as_bool(acct.get("require_at_mention")),
                "base_url": str(acct.get("base_url") or base_url).rstrip("/"),
                "ws_url": str(acct.get("ws_url") or ws_url).strip(),
            })
        if out:
            return out

    # 单账号回退: 顶层 wpp_auth_token + 顶层白名单
    authcode = str(platform_config.get("wpp_auth_token", "") or "").strip()
    if authcode:
        return [{
            "id": str(platform_config.get("id", "wpp") or "wpp"),
            "authcode": authcode,
            "allow_users": _as_csv(platform_config.get("wpp_allow_users", "")),
            "group_reply": str(platform_config.get("wpp_group_reply", "atbot") or "atbot"),
            "blacklist_groups": set(),
            "block_users": _as_csv(platform_config.get("wpp_block_users", "")),
            "require_at_mention": False,
            "base_url": base_url,
            "ws_url": ws_url,
        }]
    return []


# Dashboard WebUI 表单配置元数据 (config_metadata)
WPP_CONFIG_METADATA = {
    "wpp_base_url": {
        "description": "WPP API 接口地址",
        "type": "string",
        "hint": "宿主机IP:18062 (wechatpadpromax API; 18080是mitmdump代理非API)。AstrBot容器用宿主IP可达, 勿用容器名(DNS不通)",
    },
    "wpp_auth_token": {
        "description": "单账号模式: 微信访问凭证 (X-Access-Token = authcode)",
        "type": "string",
        "secret": True,
        "hint": "仅单账号时用。多账号请用 wpp_accounts JSON 列表。绑定微信账号的 authcode",
    },
    "wpp_accounts": {
        "description": "多账号模式: 账号列表 (JSON 数组)",
        "type": "string",
        "hint": '多账号时用。JSON 数组, 每元素: {"id":"xieyin","authcode":"xxx","allow_users":"","block_users":"","group_reply":"atbot","blacklist_groups":"","require_at_mention":false}。留空=单账号(wpp_auth_token)',
    },
    "wpp_allow_users": {
        "description": "单账号模式: 白名单 wxid (逗号分隔)",
        "type": "string",
        "hint": "仅单账号时用。多账号的白名单在 wpp_accounts 每账号里配",
    },
    "wpp_block_users": {
        "description": "单账号模式: 私聊黑名单 wxid (逗号分隔)",
        "type": "string",
        "hint": "全部放行模式下用它排除特定 wxid (私聊/群聊都生效)。公众号/微信团队/微信支付/服务通知等系统消息已内置静默, 无需填写",
    },
    "wpp_group_reply": {
        "description": "单账号模式: 群消息回复模式",
        "type": "string",
        "hint": "仅单账号时用。atbot=只回@ / none=忽略群 / all=都回",
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
        "wpp_accounts": "",
        "wpp_allow_users": "",
        "wpp_block_users": "",
        "wpp_group_reply": "atbot",
        "wpp_ws_url": "",
        "wpp_webhook_enabled": False,
    },
    config_metadata=WPP_CONFIG_METADATA,
)
class WppPlatformAdapter(Platform):
    """单实例多账号: 管理多个 WppAccount (每账号一个 authcode + WS + 白名单)。

    平台级职责: 账号解析/生命周期/meta/create_event/send_by_session/webhook 兜底。
    账号级职责 (WppAccount): WS/队列/消息处理/白名单/filehelper/去重。
    """

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

        # 解析账号列表 → 创建 WppAccount
        account_cfgs = _parse_accounts(platform_config)
        self.accounts: list[WppAccount] = []
        for acfg in account_cfgs:
            acct = WppAccount(
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
            )
            self.accounts.append(acct)
            logger.info(
                f"[WPP] 账号已注册: id={acct.account_id} base={acct.base_url} "
                f"ws={'有' if acct.ws_url else '无'}"
            )

        # 会话 → 账号绑定 (账号收到消息时登记, 主动推送时据此路由)
        self._session_accounts: OrderedDict[str, WppAccount] = OrderedDict()
        self._session_cache_max = 2000

        # webhook 兜底 (单实例一个 server, 多账号共用同一 webhook_path + token)
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

    def create_event(self, message) -> WppMessageEvent:
        """AstrBot 标准事件工厂 (适配器基类约定)。"""
        acct = self._session_accounts.get(message.session_id) or (
            self.accounts[0] if self.accounts else None
        )
        return WppMessageEvent(
            message_str=message.message_str,
            message_obj=message,
            platform_meta=self.meta(),
            session_id=message.session_id,
            api=acct._api if acct else None,
            account_id=acct.account_id if acct else "",
        )

    def bind_session(self, session_id: str, account: WppAccount) -> None:
        """登记会话 → 账号, 供 send_by_session (主动推送) 路由。"""
        if not session_id:
            return
        self._session_accounts[session_id] = account
        self._session_accounts.move_to_end(session_id)
        while len(self._session_accounts) > self._session_cache_max:
            self._session_accounts.popitem(last=False)

    def get_stats(self) -> dict:
        stats = super().get_stats()
        stats["accounts"] = [acct.stats for acct in self.accounts]
        if self.accounts:
            stats["meta"]["description"] = f"微信 WPP · {len(self.accounts)} 账号"
        return stats

    # ------------------------------------------------------------------ 生命周期
    async def run(self) -> None:
        """启动所有账号的消息 worker + WS 实时通道 (+ 可选 webhook 兜底)。"""
        # ① 每个账号: 确认在线 + 刷新身份 + 启动 worker/WS/维护循环
        for acct in self.accounts:
            try:
                await self._ensure_logged_in(acct)
                await self._refresh_account_status(acct)
                acct.start()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[WPP:{acct.account_id}] 账号启动失败: {e}")

        # ② 可选: webhook server (仅 wpp_webhook_enabled=true)
        if self.webhook_enabled:
            await self._start_webhook_server()

        # ③ 后台状态循环 (遍历所有账号检查在线 + 心跳)
        self._status_task = asyncio.create_task(self._account_status_loop())

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
        """清理: 所有账号的 WS/worker + 状态循环 + webhook server。"""
        if self._status_task:
            self._status_task.cancel()
            try:
                await self._status_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._status_task = None
        for acct in self.accounts:
            try:
                await acct.stop()
            except Exception:  # noqa: BLE001
                pass
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
            self._server = None

    # ------------------------------------------------------------------ webhook 兜底
    async def _handle_webhook_health(self, request: Any) -> Any:
        from aiohttp import web

        return web.Response(text="ok")

    async def _handle_webhook(self, request: Any) -> Any:
        """接收 WPP vendor 消息 push (webhook 已退役, 多账号时按 recipient 路由)。"""
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

        for src in extract_all_msg_src(payload):
            recipient_id = safe_str(src.get("recipient_id") or src.get("toWxid") or src.get("ToWxid"))
            acct = self._find_account_by_recipient(recipient_id)
            if acct is None:
                continue
            try:
                await acct._process_src(src)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[WPP] webhook _process_src failed: {e}")
        return web.Response(status=200)

    def _find_account_by_recipient(self, recipient_id: str) -> WppAccount | None:
        """按 recipient_id (机器人 wxid) 定位账号。单账号直接返回。"""
        if len(self.accounts) == 1:
            return self.accounts[0]
        for acct in self.accounts:
            if acct._self_wxid and recipient_id and recipient_id == acct._self_wxid:
                return acct
        return None

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
        """定位回复所属账号。

        1. 账号收到消息时登记的 session → 账号绑定 (最准确)
        2. 单账号直接返回
        3. 多账号且无绑定: 返回 None (宁可不发, 也不用错账号发)
        """
        acct = self._session_accounts.get(session.session_id)
        if acct is not None:
            return acct
        if len(self.accounts) == 1:
            return self.accounts[0]
        return None

    async def send_by_session(
        self, session: MessageSession, message_chain: MessageChain
    ) -> None:
        """AstrBot 回复/主动推送统一走这里 → 对应账号的 WPP 发送。"""
        try:
            acct = self._find_account_for_send(session)
            if acct is None:
                logger.warning(
                    f"[WPP] 无法确定会话 {session.session_id} 所属账号 "
                    f"(多账号且无绑定), 发送失败"
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
