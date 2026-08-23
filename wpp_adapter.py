"""微信 WPP 平台适配器 (独立文件, main.py import 后由 @register_platform_adapter 自动注册)。

AstrBot 平台适配器插件范式 (见 docs/zh/dev/plugin-platform-adapter.md):
  1. main.py 必须有 Star 类 (否则 star_manager 拒绝加载)
  2. 适配器放独立文件, 用 @register_platform_adapter 装饰
  3. main.py __init__ 里 import 适配器模块触发注册

契约 (已从 WPP 插件源码 + swagger 核实):
  - WPP basePath = /api, 鉴权: X-Access-Token header + authcode query
  - 文本发送: POST /api/Msg/SendTxt  body={ToWxid, Content, At, Type:1}
  - 图片发送: POST /api/Msg/UploadImg  body={ToWxid, Base64}
  - 消息 push: vendor webhook 推送 { Wxid, EventType, Data:{ Data:{ AddMsgs:[...] } } }
  - 消息类型数字: 1=text 3=image 34=voice 43=video 47=emoji 49=app 51=relay 10002=revoke
"""

import asyncio
import hmac
import json
import secrets
from typing import Any

from astrbot import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain
from astrbot.api.platform import (
    AstrBotMessage,
    Group,
    MessageMember,
    MessageType,
    Platform,
    PlatformMetadata,
    register_platform_adapter,
)
from astrbot.core.platform.astr_message_event import MessageSesion

from .wpp_client import WppClient

MSG_TYPE_TEXT = 1
MSG_TYPE_IMAGE = 3
MSG_TYPE_FILE = 6
MSG_TYPE_VOICE = 34
MSG_TYPE_VIDEO = 43
MSG_TYPE_EMOJI = 47
MSG_TYPE_APP = 49
MSG_TYPE_RELAY = 51
MSG_TYPE_REVOKE = 10002


def _safe_str(v: Any) -> str:
    return "" if v is None else str(v)


def _load_or_create_webhook_token() -> str:
    """加载或创建 webhook token (P0-1 持久化)。

    优先读插件目录 .webhook_token 文件 (重启稳定), 无则生成 32 位 hex 并写入。
    用 importlib.metadata 定位插件根目录 (避免硬编码路径)。
    """
    import os
    from pathlib import Path

    # 插件根目录: 优先取本文件所在目录 (dev + 部署后同构)
    plugin_dir = Path(__file__).resolve().parent
    token_file = plugin_dir / ".webhook_token"
    try:
        if token_file.exists():
            tok = token_file.read_text().strip()
            if tok:
                return tok
    except Exception:  # noqa: BLE001
        pass
    tok = secrets.token_hex(16)
    try:
        token_file.write_text(tok)
        os.chmod(token_file, 0o600)  # 仅 owner 可读写
    except Exception:  # noqa: BLE001
        pass
    logger.warning(
        f"[WPP] webhook_token 未配置, 自动生成并持久化: {tok}\n"
        f"[WPP] 实际 webhook 路径: /wpp/webhook/{tok} (若 nginx 反代需放行该路径; 也可在配置里显式设 webhook_token)"
    )
    return tok


def _safe_num(v: Any, fallback: int = 1) -> int:
    try:
        if isinstance(v, bool):
            return fallback
        return int(float(v))
    except (TypeError, ValueError):
        return fallback


# Dashboard WebUI 表单配置元数据 (config_metadata)
# 不传则 WebUI 显示原始键值对; 传了显示带说明的表单
WPP_CONFIG_METADATA = {
    "wpp_base_url": {
        "description": "WPP API 接口地址",
        "type": "string",
        "hint": "宿主机IP:18062 (wechatpadpromax API; 18080是mitmdump代理非API)。AstrBot容器用宿主IP可达, 勿用容器名(DNS不通)",
    },
    "wpp_auth_token": {
        "description": "WPP 访问凭证 (X-Access-Token = authcode)",
        "type": "string",
        "isSecret": True,
        "hint": "绑定微信账号的 X-Access-Token (即 authcode, 同一值)。独立账号勿与 WPP-OpenClaw 共用。填你绑定微信账号的 X-Access-Token",
    },
    "wpp_allow_users": {
        "description": "白名单 wxid (逗号分隔)",
        "type": "string",
        "hint": "仅这些微信能触发 AI 回复 (私聊)。默认含自己。如: wxid_abc123,wxid_xxx",
    },
    "wpp_group_reply": {
        "description": "群消息回复模式",
        "type": "string",
        "hint": "atbot=只回@机器人的群消息 (推荐); none=忽略群消息; all=群消息都回",
    },
    "webhook_host": {
        "description": "Webhook 监听地址",
        "type": "string",
        "hint": "一般保持 0.0.0.0",
    },
    "webhook_port": {
        "description": "Webhook 监听端口",
        "type": "number",
        "hint": "AstrBot 容器已暴露 6199; 若冲突用 6194-6196",
    },
    "webhook_path": {
        "description": "Webhook 路径 (路径即密钥: 实际为 /wpp/webhook/<token>)",
        "type": "string",
        "hint": "默认 /wpp/webhook; 会自动拼上 webhook_token",
    },
    "webhook_token": {
        "description": "Webhook 路径 token (安全加固)",
        "type": "string",
        "isSecret": True,
        "hint": "随机字符串, 插入 webhook 路径形成密钥。留空自动生成 32 位随机值。有 nginx 反代时需同步放行该路径",
    },
    "webhook_public_url": {
        "description": "Webhook 公网回调地址",
        "type": "string",
        "hint": "注册给 vendor 的 AstrBot 公网地址 (如 http://<host>:6199/wpp/webhook)。留空则用 webhook_host:port",
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
        "wpp_group_reply": "atbot",
        "webhook_host": "0.0.0.0",
        "webhook_port": 6199,
        "webhook_path": "/wpp/webhook",
        "webhook_token": "",
        "webhook_public_url": "",
    },
    config_metadata=WPP_CONFIG_METADATA,
)
class WppPlatformAdapter(Platform):
    def __init__(
        self,
        platform_config: dict,
        platform_settings: dict,
        event_queue: asyncio.Queue,
    ) -> None:
        super().__init__(platform_config, event_queue)
        self.config = platform_config
        self.settings = platform_settings

        self.base_url = str(platform_config.get("wpp_base_url", "http://127.0.0.1:18062")).rstrip("/")
        self.auth_token = str(platform_config.get("wpp_auth_token", ""))
        self.webhook_host = str(platform_config.get("webhook_host", "0.0.0.0"))
        self.webhook_port = int(platform_config.get("webhook_port", 6199))
        # webhook 路径 token (安全加固, P0-1): 未配置则自动生成 32 位随机 hex 并持久化。
        # 路径即密钥 — vendor 注册的 url 带此 token, 无 token 的请求 404。
        # 持久化策略: 配置里显式配 > 插件目录 .webhook_token 文件 > 自动生成+写文件 (重启稳定)
        self.webhook_token = str(platform_config.get("webhook_token", "") or "").strip()
        if not self.webhook_token:
            self.webhook_token = _load_or_create_webhook_token()
        self.webhook_path = str(platform_config.get("webhook_path", "/wpp/webhook")).rstrip("/")
        # 注册路径 = base + /token (路径即密钥)
        self.webhook_path = f"{self.webhook_path}/{self.webhook_token}"
        # webhook HMAC secret (可选, 默认空=不验签; 需 vendor 侧配置一致才启用)
        self.webhook_secret = str(platform_config.get("webhook_secret", "") or "").strip()
        self.webhook_public_url = str(platform_config.get("webhook_public_url", "") or "").rstrip("/")

        # 白名单配置: wpp_allow_users 逗号分隔 wxid; 空则默认允许所有人
        allow_str = str(platform_config.get("wpp_allow_users", "") or "").strip()
        self.allow_users = {u.strip() for u in allow_str.split(",") if u.strip()}
        # 群消息回复模式: atbot / none / all
        self.group_reply = str(platform_config.get("wpp_group_reply", "atbot") or "atbot")
        # 群聊是否必须 @ 才回复 (默认 False = 白名单群内非@也触发; 仿 wpp-openclaw requireAtMention)
        self.require_at_mention = False
        # 黑名单群 (永远不回复, filehelper 命令管理)
        self.blacklist_groups: set[str] = set()
        # filehelper 白名单持久化: 插件目录 wpp_whitelist.json (filehelper 命令改内存+写文件, 重启不丢)
        #   启动时合并文件配置到内存 (config 为基础 + 文件增量)
        self._whitelist_file = self._get_whitelist_file_path()
        persisted = self._load_whitelist_file()
        if persisted.get("allow_users"):
            self.allow_users.update(persisted["allow_users"])
        if persisted.get("group_reply"):
            self.group_reply = persisted["group_reply"]
        if persisted.get("blacklist_groups"):
            self.blacklist_groups = set(persisted["blacklist_groups"])
        if persisted.get("require_at_mention") is not None:
            self.require_at_mention = bool(persisted["require_at_mention"])
        # 配置文件热更新: 记录当前文件 mtime, 后台循环检测外部修改 → 自动重载
        #   (手动编辑 wpp_whitelist.json 也即时生效, 对齐 wpp-openclaw 配置热重载)
        self._whitelist_mtime = self._get_file_mtime()
        self._reload_task: asyncio.Task | None = None

        self._api = WppClient(self.base_url, self.auth_token)
        self._runner = None
        self._server = None
        # 账号状态缓存 (get_stats 用, 后台定时刷新)
        self._account_status = {"online": None, "wxid": "", "nickname": "", "message": "查询中..."}
        self._status_task: asyncio.Task | None = None
        # 机器人自身 wxid + 昵称 (群@判断用, 从 GetOnlineInfo/GetContractProfile 刷新)
        self._self_wxid = ""
        self._self_nickname = ""

        # P1-1 消息去重: 记录最近 N 条消息 id (vendor 三通道 Webhook/Business/StartAutoSync
        # 会重复推送同一条消息, 不做去重会重复触发 AI → 重复回复)。环形集合, 自动淘汰最旧。
        self._seen_msg_ids: set[str] = set()
        self._seen_msg_ids_order: list[str] = []
        self._dedup_max = 200

    # ------------------------------------------------------------------ Platform
    def meta(self) -> PlatformMetadata:
        return PlatformMetadata(
            name="wpp",
            description="微信 WPP 适配器 (WeChatPadPro)",
            id="wpp",
            adapter_display_name="微信 WPP",
        )

    def get_stats(self) -> dict:
        """重写: 基类返回 + account_status 账号信息 (在线昵称/wxid, 或 未登录)。

        ⚠️ Dashboard 卡片标题用 s.type||s.id 显示 (硬编码), display_name 改不动卡片。
        卡片无法显示昵称, 除非改前端 PlatformPage 组件。account_status 数据供 API/未来前端用。
        """
        stats = super().get_stats()
        stats["account_status"] = dict(self._account_status)
        acct = self._account_status
        stats["meta"]["description"] = f"微信 WPP · {acct.get('message', '')}"
        return stats

    async def run(self) -> None:
        """启动 webhook server + 把回调 url 注册进 WPP vendor。"""
        from aiohttp import web

        app = web.Application()
        app.router.add_post(self.webhook_path, self._handle_webhook)
        app.router.add_get(self.webhook_path, self._handle_webhook_health)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._server = web.TCPSite(self._runner, self.webhook_host, self.webhook_port)
        await self._server.start()
        logger.info(
            f"[WPP] webhook server started at http://{self.webhook_host}:{self.webhook_port}{self.webhook_path}"
        )

        # ① 自动登录流程: 查在线 → 不在线则出二维码
        await self._ensure_logged_in()

        # ①.5 提前刷新账号状态: self._self_wxid/_self_nickname 用于群@判断,
        #     必须在 webhook 注册前就绪 (否则 vendor 推送第一条消息时昵称还没填上 → 群@被误过滤)
        await self._refresh_account_status()

        # ② 把回调 url 注册进 WPP vendor (best-effort, 失败不阻塞)
        # 优先用配置的 webhook_public_url (公网可达), 否则 fallback host:port
        try:
            if self.webhook_public_url:
                callback_url = self.webhook_public_url + self.webhook_path
            else:
                callback_url = f"http://{self.webhook_host}:{self.webhook_port}{self.webhook_path}"
            ok = await self._api.set_webhook(callback_url)
            status = "OK" if ok else "FAILED"
            logger.info(f"[WPP] register webhook to vendor: {status} url={callback_url}")

            # 业务回调 + StartAutoSync: 推完整消息 (只配 /Webhook/Set 只推空 Data)
            # 参考 wpp-openclaw index.ts: setBusinessWebhook + startAutoSync
            # P0-1: businessPath 也用带 token 的路径 (路径即密钥)
            biz = await self._api.set_business_webhook(callback_url)
            sync = await self._api.start_auto_sync(callback_url)
            logger.info(f"[WPP] business webhook: {biz} | auto-sync: {sync} url={callback_url}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[WPP] register webhook to vendor failed: {e}")

        # ③ 启动账号状态后台刷新任务 (供 get_stats / Dashboard 卡片展示)
        # P2-4: _refresh_account_status 已在 ①.5 调用过, 这里不再重复调用, 直接起循环任务
        self._status_task = asyncio.create_task(self._account_status_loop())
        # ③.5 配置文件热更新: 外部修改 wpp_whitelist.json → 自动重载 (即时生效)
        self._reload_config_loop()

    async def _account_status_loop(self) -> None:
        """后台循环: 每 60s 刷新账号在线状态缓存。"""
        while True:
            try:
                await asyncio.sleep(60)
                await self._refresh_account_status()
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[WPP] account status refresh failed: {e}")

    async def _refresh_account_status(self) -> None:
        """查在线状态 + 个人资料, 更新 self._account_status 缓存。"""
        try:
            online, resp = await self._api.is_online()
            data = resp.get("Data") or {}
            wxid = data.get("wxid", "")
            if wxid:
                self._self_wxid = wxid  # 群@判断用
            if online:
                prof = None
                try:
                    prof = await self._api.get_contract_profile()
                except Exception:  # noqa: BLE001
                    pass
                nickname = ""
                if prof and prof.get("Success") is not False:
                    ui = (prof.get("Data") or {}).get("userInfo") or {}
                    nv = ui.get("NickName")
                    nickname = str(nv.get("string", "")) if isinstance(nv, dict) else (str(nv) if nv else "")
                if nickname:
                    self._self_nickname = nickname  # 群@判断用 (昵称匹配)
                self._account_status = {
                    "online": True,
                    "wxid": wxid,
                    "nickname": nickname,
                    "message": f"在线 · {nickname} ({wxid})",
                }
            else:
                self._account_status = {
                    "online": False,
                    "wxid": wxid,
                    "nickname": "",
                    "message": "未登录",
                }
        except Exception as e:  # noqa: BLE001
            self._account_status = {
                "online": None,
                "wxid": "",
                "nickname": "",
                "message": f"查询失败: {e}",
            }

    async def _ensure_logged_in(self) -> None:
        """确认账号在线。

        流程:
          1. GetOnlineInfo → 若 Data.online==True 视为在线, 继续
          2. 若不在线但账号已登录 (Code==0 但 online=false) → 调 AutoHeartBeat 拉上线, 再复查
          3. 仍不在线 (未登录) → 引导用户去 vendor 面板登录
        """
        try:
            online, resp = await self._api.is_online()
            if online:
                data = resp.get("Data") or {}
                logger.info(f"[WPP] 账号已在线: wxid={data.get('wxid')} heartbeatRunning={data.get('heartbeatRunning')}")
                return
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[WPP] get_online_info failed: {e}")

        # 账号已登录 (Code==0) 但 online=false → 心跳没启动, 拉上线
        try:
            hb = await self._api.auto_heartbeat()
            if hb.get("Success") is not False:
                logger.info(f"[WPP] 已触发自动心跳: {hb.get('Message')}")
                await asyncio.sleep(2)
                online, resp = await self._api.is_online()
                if online:
                    data = resp.get("Data") or {}
                    logger.info(f"[WPP] 心跳后账号已在线: wxid={data.get('wxid')}")
                    return
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[WPP] auto_heartbeat failed: {e}")

        # 仍未在线: 引导用户去 WPP vendor 面板登录
        logger.warning(
            f"[WPP] ============================================================\n"
            f"[WPP] 账号未在线 (微信未在 vendor 成功建立连接)\n"
            f"[WPP] 请到 WPP 管理面板检查该账号登录状态 (iPad/Mac/安卓Pad):\n"
            f"[WPP]   面板地址: {self.base_url}\n"
            f"[WPP] 登录成功后再重启本平台即可接收消息。\n"
            f"[WPP] ============================================================"
        )

    async def terminate(self) -> None:
        """清理: 状态刷新任务 + webhook server + API session (P1-4 合并重复 terminate)。"""
        if self._status_task:
            self._status_task.cancel()
            try:
                await self._status_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if self._reload_task:
            self._reload_task.cancel()
            try:
                await self._reload_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        if self._api:
            try:
                await self._api.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ webhook
    async def _handle_webhook_health(self, request: Any) -> Any:
        from aiohttp import web
        return web.Response(text="ok")

    async def _handle_webhook(self, request: Any) -> Any:
        """接收 WPP vendor 消息 push。

        鉴权 (P0-1): 路径已含随机 token (路由注册时限定), 这里做第二层校验:
          1. 请求路径必须等于 {base}/{token} (防御 path traversal / 其它前缀命中)
          2. 若配置了 webhook_secret (HMAC), 校验 X-Signature / X-Hub-Signature-256
        """
        from aiohttp import web

        # ① 路径校验: 请求路径必须精确匹配注册路径 (含 token)
        #    注册路由时 aiohttp 只匹配完整 path, 但这里再确认一次 (防 /wpp/webhook/ 之类前缀)
        req_path = request.path
        if req_path.rstrip("/") != self.webhook_path:
            logger.warning(f"[WPP] webhook path mismatch: {req_path} != {self.webhook_path}")
            return web.Response(status=404)

        try:
            raw = await request.text()
            payload = json.loads(raw)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[WPP] bad webhook payload: {e}")
            return web.Response(status=200)  # 200 防 vendor 重试轰炸

        # ② HMAC 签名校验 (可选, 需要 vendor 配置 secret): 校验 X-Signature / X-Hub-Signature-256
        #    参考 wpp-openclaw signature.ts (HMAC-SHA256 + timingSafeEqual)
        webhook_secret = str(getattr(self, "webhook_secret", "") or "")
        if webhook_secret:
            sig = (
                request.headers.get("X-Signature")
                or request.headers.get("X-Hub-Signature-256")
                or request.headers.get("X-WPP-Signature")
                or ""
            )
            if not sig or not self._verify_webhook_signature(raw, sig, webhook_secret):
                logger.warning(f"[WPP] webhook signature verify failed: path={req_path}")
                return web.Response(status=403)

        # 遍历所有消息 (vendor 一次可能推多条, 只取第一条会漏文件/图片等)
        for src in self._extract_all_msg_src(payload):
            await self._process_src(src)
        return web.Response(status=200)

    @staticmethod
    def _verify_webhook_signature(raw_body: str, signature: str, secret: str) -> bool:
        """HMAC-SHA256 验签 (支持 sha256=/sha1=/md5= 前缀或裸 hex)。"""
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

    async def _process_src(self, src: dict) -> None:
        """处理单条消息: 白名单过滤 → 转换 → 媒体下载 → 提交事件。"""
        # 白名单 + 群@ 过滤 (兼容 v1 sender_id/is_group/conversation_id)
        from_wxid = _safe_str(src.get("fromUser") or src.get("fromWxid") or src.get("FromWxid") or src.get("sender_id"))
        chatroom_id = _safe_str(src.get("chatroomId") or src.get("ChatroomId"))
        v1_is_group = src.get("is_group")
        if not chatroom_id and (v1_is_group is True or v1_is_group == "true" or v1_is_group == 1):
            chatroom_id = _safe_str(src.get("conversation_id"))
        content = _safe_str(src.get("content") or src.get("Content") or src.get("text"))
        is_group = bool(chatroom_id)
        # 机器人 wxid: 消息的 recipient_id 最可靠 (当前会话的机器人身份)
        recipient_id = _safe_str(src.get("recipient_id") or src.get("toWxid") or src.get("ToWxid"))
        # filehelper 会话识别: recipient_id 或 conversation_id = "filehelper"
        #   (老板在机器人手机的文件传输助手里发命令, 参考 wpp-openclaw v1.3.39)
        conversation_id = _safe_str(src.get("conversation_id"))
        is_filehelper = (recipient_id == "filehelper") or (conversation_id == "filehelper")
        # P1-1 去重: 用消息 id (msgId/svr_id/id) 去重, 同 id 重复推送只处理一次
        msg_id_key = _safe_str(src.get("msgId") or src.get("MsgId") or src.get("svr_id") or src.get("id"))
        if msg_id_key and msg_id_key in self._seen_msg_ids:
            logger.debug(f"[WPP] 重复消息已跳过 (dedup): id={msg_id_key} content={content[:20]!r}")
            return
        # 收到消息探针 (P2-2: DEBUG 级, 避免每条消息/空消息/公众号刷屏 INFO 日志)
        logger.debug(f"[WPP] 收到: from={from_wxid} group={chatroom_id or '-'} is_group={is_group} content={content[:40]!r} recipient={recipient_id!r} self_wxid={self._self_wxid!r} self_nick={self._self_nickname!r}")
        # filehelper 命令处理 (老板拍板: 像 wpp-openclaw 一样, 文件传输助手里发指令管理白名单)
        #   放在 direction/自己消息过滤之前 — filehelper 命令是 outgoing + from_wxid=自己, 但要处理
        #   参考 wpp-openclaw v1.3.39: isFileHelperCommand 即使 outgoing 也放行
        if is_filehelper and content.startswith("/"):
            await self._handle_filehelper_command(content, from_wxid, recipient_id)
            return
        # v1: 只处理 incoming; 忽略公众号 (gh_)
        direction = _safe_str(src.get("direction"))
        if direction and direction not in ("incoming", "1"):
            return
        if from_wxid.startswith("gh_"):
            return
        # P1-3: 过滤机器人自己发的消息 (from=自己) → 防自我回复循环
        # 注意: recipient_id 是机器人自己 (被@) 不算, 只过滤发送者是自己
        if from_wxid and from_wxid == _safe_str(self._self_wxid):
            logger.debug(f"[WPP] 跳过自己消息: from={from_wxid}")
            return

        if not self._is_allowed(from_wxid, is_group, chatroom_id, content, recipient_id):
            logger.warning(f"[WPP] 消息被白名单/群策略过滤: from={from_wxid} group={chatroom_id or '-'}")
            return

        msg = self._to_astrbot_message_src(src)
        if msg:
            # 图片消息: 异步下载 → 注入 Image 组件 → 再提交
            img_meta = getattr(msg, "_wpp_image_meta", None)
            if img_meta:
                try:
                    img_bytes = None
                    # 路径1: CDN 完整大图 (优先)
                    cdn = img_meta.get("cdn")
                    if cdn:
                        img_bytes = await self._api.download_image_cdn(
                            cdn.get("file_aes_key", ""),
                            cdn.get("file_no", ""),
                        )
                        if img_bytes:
                            logger.info(f"[WPP] image CDN download: {len(img_bytes)} bytes")
                    # 路径2: DownloadImg 兜底
                    if not img_bytes:
                        img_bytes = await self._api.download_image(
                            img_meta.get("msg_id", ""),
                            img_meta.get("to_wxid", ""),
                            img_meta.get("data_len", 0),
                        )
                        if img_bytes:
                            logger.info(f"[WPP] image DownloadImg: {len(img_bytes)} bytes")
                    if img_bytes:
                        from astrbot.api.message_components import Image
                        msg.message = [Image.fromBytes(img_bytes)]
                        msg.message_str = "[图片]"
                        logger.info(f"[WPP] image injected: {len(img_bytes)} bytes")
                    else:
                        logger.warning(f"[WPP] image download failed: {img_meta}")
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[WPP] image download error: {e}")

            # 文件消息: 下载 → 注入 File 组件 → 再提交
            file_meta = getattr(msg, "_wpp_file_meta", None)
            if file_meta:
                try:
                    attach_id = file_meta.get("attach_id", "")
                    if attach_id:
                        file_bytes = await self._api.download_file_binary(
                            attach_id,
                            file_meta.get("user_name", ""),
                            file_meta.get("data_len", 0),
                        )
                        if file_bytes:
                            from astrbot.api.message_components import File
                            fname = file_meta.get("filename", "file")
                            # File 组件构造: File(name, file=本地路径)
                            # 写入 AstrBot 允许的 temp 路径 (/AstrBot/data/temp), AI 才能读取
                            # 文件名保留扩展名 (截断 base, 不截断后缀, 否则 AI 识别不了类型)
                            import os
                            from astrbot.core.utils.astrbot_path import get_astrbot_temp_path
                            base, ext = os.path.splitext(fname)
                            safe_base = base[:20] or "file"
                            tmp = os.path.join(get_astrbot_temp_path(), f"wpp_{safe_base}{ext}")
                            with open(tmp, "wb") as f:
                                f.write(file_bytes)
                            # 根据扩展名提示 AI 用对应技能 (避免 AI 用文本工具读二进制失败)
                            skill_hint = ""
                            ext_l = ext.lower()
                            if ext_l in (".xlsx", ".xlsm", ".xls", ".csv", ".tsv"):
                                skill_hint = " (这是Excel/表格文件, 请用 spreadsheets 技能读取, 不要用文本读取工具)"
                            elif ext_l in (".pdf",):
                                skill_hint = " (这是PDF文件, 请用 pdf/document 技能读取)"
                            elif ext_l in (".docx", ".doc", ".pptx"):
                                skill_hint = " (这是Office文档, 请用 documents 技能读取)"
                            msg.message = [Plain(f"[文件] {fname}{skill_hint}"), File(name=fname, file=tmp)]
                            msg.message_str = f"[文件] {fname}"
                            logger.info(f"[WPP] file downloaded: {fname} ({len(file_bytes)} bytes) -> {tmp}")
                        else:
                            logger.warning(f"[WPP] file download failed: {file_meta.get('filename')}")
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[WPP] file download error: {e}")

            # 视频消息: 下载 → 注入 Video 组件 → 再提交
            video_meta = getattr(msg, "_wpp_video_meta", None)
            if video_meta:
                try:
                    if video_meta.get("msg_id"):
                        video_bytes = await self._api.download_video(
                            video_meta.get("msg_id", ""),
                            video_meta.get("to_wxid", ""),
                            video_meta.get("data_len", 0),
                            video_meta.get("compress_type", 0),
                        )
                        if video_bytes:
                            from astrbot.api.message_components import Video
                            # P2-1: 视频也写 AstrBot temp 目录 (get_astrbot_temp_path), 不用 tempfile.gettempdir
                            #   (后者可能是 /tmp, AI 权限受限读不到 → 视频无法被 AI 理解)
                            import os
                            from astrbot.core.utils.astrbot_path import get_astrbot_temp_path
                            tmp = os.path.join(get_astrbot_temp_path(), f"wpp_video_{abs(hash(video_meta.get('msg_id','')))}.mp4")
                            with open(tmp, "wb") as f:
                                f.write(video_bytes)
                            msg.message = [Plain(f"[视频] {len(video_bytes)} bytes"), Video(file=tmp)]
                            msg.message_str = "[视频]"
                            logger.info(f"[WPP] video downloaded: {len(video_bytes)} bytes -> {tmp}")
                        else:
                            logger.warning(f"[WPP] video download failed: {video_meta}")
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[WPP] video download error: {e}")

            # 处理完成后记录 msg_id (供去重; 只在转换成功且有 id 时记录)
            self._record_seen_msg(msg_id_key)

            self._handle_msg(msg)

    def _record_seen_msg(self, msg_id_key: str) -> None:
        """记录已处理的消息 id, 维护环形集合 (自动淘汰最旧)。"""
        if not msg_id_key:
            return
        if msg_id_key in self._seen_msg_ids:
            return
        self._seen_msg_ids.add(msg_id_key)
        self._seen_msg_ids_order.append(msg_id_key)
        # 超过上限淘汰最旧 (FIFO)
        while len(self._seen_msg_ids_order) > self._dedup_max:
            old = self._seen_msg_ids_order.pop(0)
            self._seen_msg_ids.discard(old)

    # ------------------------------------------------------------------ 白名单持久化
    @staticmethod
    def _get_whitelist_file_path() -> str:
        """白名单持久化文件路径 (插件目录 wpp_whitelist.json)。"""
        from pathlib import Path
        return str(Path(__file__).resolve().parent / "wpp_whitelist.json")

    def _load_whitelist_file(self) -> dict:
        """读取持久化运行时配置 (filehelper 命令写入的增量白名单/黑名单/群策略)。

        返回 dict: {"allow_users": set, "group_reply": str, "blacklist_groups": set, "require_at_mention": bool}
        文件缺失/损坏 → 空配置 (不影响 config 基础值)。
        """
        try:
            from pathlib import Path
            p = Path(self._whitelist_file)
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    out: dict = {}
                    arr = data.get("allow_users", [])
                    if isinstance(arr, list):
                        out["allow_users"] = {str(u).strip() for u in arr if str(u).strip()}
                    if isinstance(data.get("group_reply"), str):
                        out["group_reply"] = data["group_reply"]
                    bl = data.get("blacklist_groups", [])
                    if isinstance(bl, list):
                        out["blacklist_groups"] = {str(g).strip() for g in bl if str(g).strip()}
                    if data.get("require_at_mention") is not None:
                        out["require_at_mention"] = bool(data["require_at_mention"])
                    return out
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[WPP] 读取配置文件失败: {e}")
        return {}

    def _save_whitelist_file(self) -> None:
        """把当前内存运行时配置持久化到文件 (filehelper 命令改后调用)。"""
        try:
            from pathlib import Path
            p = Path(self._whitelist_file)
            p.write_text(
                json.dumps({
                    "allow_users": sorted(self.allow_users),
                    "group_reply": self.group_reply,
                    "blacklist_groups": sorted(self.blacklist_groups),
                    "require_at_mention": self.require_at_mention,
                }, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            # 记录本次写入 mtime, 避免热更新检测到"自己写的"而重复重载
            self._whitelist_mtime = self._get_file_mtime()
            logger.info(f"[WPP] 运行时配置已持久化 → {self._whitelist_file}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[WPP] 写配置文件失败: {e}")

    # ------------------------------------------------------------------ 配置文件热更新
    def _get_file_mtime(self) -> float:
        """获取配置文件 mtime (不存在返回 0)。"""
        try:
            from pathlib import Path
            p = Path(self._whitelist_file)
            return p.stat().st_mtime if p.exists() else 0.0
        except Exception:  # noqa: BLE001
            return 0.0

    def _apply_persisted_config(self, persisted: dict) -> bool:
        """把文件配置合并到内存 (热更新重载用)。返回是否有变化。"""
        changed = False
        if persisted.get("allow_users"):
            new_set = set(persisted["allow_users"])
            if new_set != self.allow_users:
                self.allow_users = new_set
                changed = True
        if persisted.get("group_reply"):
            new_val = persisted["group_reply"]
            if new_val != self.group_reply:
                self.group_reply = new_val
                changed = True
        if persisted.get("blacklist_groups") is not None:
            new_set = set(persisted["blacklist_groups"])
            if new_set != self.blacklist_groups:
                self.blacklist_groups = new_set
                changed = True
        if persisted.get("require_at_mention") is not None:
            new_val = bool(persisted["require_at_mention"])
            if new_val != self.require_at_mention:
                self.require_at_mention = new_val
                changed = True
        return changed

    def _reload_config_loop(self) -> None:
        """后台循环: 每 3 秒检查配置文件 mtime, 外部修改 → 自动重载。"""
        import asyncio

        async def _loop() -> None:
            while True:
                try:
                    await asyncio.sleep(3)
                    mtime = self._get_file_mtime()
                    # 外部修改: mtime 变了且不是自己刚写的
                    if mtime > 0 and mtime != self._whitelist_mtime:
                        self._whitelist_mtime = mtime
                        persisted = self._load_whitelist_file()
                        if self._apply_persisted_config(persisted):
                            logger.info("[WPP] 配置文件外部修改已热更新")
                except asyncio.CancelledError:
                    break
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[WPP] 配置文件热更新检查失败: {e}")

        self._reload_task = asyncio.create_task(_loop())

    # ------------------------------------------------------------------ filehelper 命令
    async def _handle_filehelper_command(self, content: str, from_wxid: str, recipient_id: str) -> None:
        """处理文件传输助手命令: /help /adduser /deluser /addgroup /delgroup。

        参考 wpp-openclaw FILEHELPER_COMMANDS (v1.3.39): 老板在机器人手机的文件传输助手里
        发命令管理白名单, 命令执行后回发 filehelper 确认。运行时改 self.allow_users 立即生效。
        """
        # filehelper 命令只有 admin (机器人自己) 能发 — from_wxid 通常是机器人自己
        # 注意: filehelper 消息 from_wxid = 机器人自己 (senderId), 与 recipient_id=filehelper 并存
        parts = content.strip().split()
        cmd = (parts[0] if parts else "").lower()
        args = parts[1:]

        async def reply(text: str) -> None:
            # 回发到 filehelper (机器人自己看); 失败仅告警
            try:
                await self._api.send_text("filehelper", text)
                logger.info(f"[WPP FILEHELPER] → {text[:60]}")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[WPP FILEHELPER] 回发失败: {e}")

        if cmd == "/help":
            await reply(
                "【WPP 命令】\n"
                "/adduser <wxid>   授权私聊白名单\n"
                "/deluser <wxid>   移除私聊白名单\n"
                "/addgroup <群ID>  授权群聊白名单\n"
                "/delgroup <群ID>  移除群聊白名单\n"
                "/blacklist add|del|list <群ID>  黑名单群管理\n"
                "/group atbot|none|all  群回复模式\n"
                "/at on|off  群聊是否必须@才回复\n"
                "/help        显示本帮助"
            )
            return

        if cmd == "/adduser":
            target = (args[0] if args else "").strip()
            if not target:
                await reply("用法: /adduser <wxid>\n示例: /adduser wxid_abc123")
                return
            self.allow_users.add(target)
            self._save_whitelist_file()  # 持久化 (重启不丢)
            await reply(f"✅ 已授权私聊白名单: {target}\n当前 ({len(self.allow_users)}): {', '.join(sorted(self.allow_users))}")
            return

        if cmd == "/deluser":
            target = (args[0] if args else "").strip()
            if not target:
                await reply("用法: /deluser <wxid>\n示例: /deluser wxid_abc123")
                return
            if target in self.allow_users:
                self.allow_users.discard(target)
                self._save_whitelist_file()  # 持久化 (重启不丢)
                remaining = ", ".join(sorted(self.allow_users))
                await reply(f"✅ 已移除私聊白名单: {target}\n当前 ({len(self.allow_users)}): {remaining or '(空)'}")
            else:
                await reply(f"❌ {target} 不在私聊白名单中")
            return

        if cmd == "/addgroup":
            target = (args[0] if args else "").strip()
            if not target:
                await reply("用法: /addgroup <群ID>\n示例: /addgroup xxxxxxxx@chatroom")
                return
            self.allow_users.add(target)
            # 互斥: 加白名单 → 从黑名单自动移除 (老板要求: 黑白名单不能同时存在)
            if target in self.blacklist_groups:
                self.blacklist_groups.discard(target)
                removed_note = f"\n(已自动从黑名单移除 {target})"
            else:
                removed_note = ""
            self._save_whitelist_file()  # 持久化 (重启不丢)
            await reply(f"✅ 已授权群聊白名单: {target}{removed_note}\n当前群 ({sum(1 for u in self.allow_users if u.endswith('@chatroom'))}): {', '.join(sorted(u for u in self.allow_users if u.endswith('@chatroom'))) or '(空)'}")
            return

        if cmd == "/delgroup":
            target = (args[0] if args else "").strip()
            if not target:
                await reply("用法: /delgroup <群ID>\n示例: /delgroup xxxxxxxx@chatroom")
                return
            if target in self.allow_users:
                self.allow_users.discard(target)
                await reply(f"✅ 已移除群聊白名单: {target}\n当前群 ({sum(1 for u in self.allow_users if u.endswith('@chatroom'))}): {', '.join(sorted(u for u in self.allow_users if u.endswith('@chatroom'))) or '(空)'}")
            else:
                await reply(f"❌ {target} 不在群聊白名单中")
            return

        if cmd == "/group":
            mode = (args[0] if args else "").strip().lower()
            if mode not in ("atbot", "none", "all"):
                await reply("用法: /group atbot|none|all\natbot=只回@机器人的群消息 / none=忽略群消息 / all=群消息都回\n当前: " + self.group_reply)
                return
            self.group_reply = mode
            self._save_whitelist_file()  # 持久化
            await reply(f"✅ 群消息回复模式已设为: {mode}\n(atbot=只回@ / none=忽略群 / all=都回)")
            return

        if cmd == "/blacklist":
            action = (args[0] if args else "").strip().lower()
            target = (args[1] if len(args) > 1 else "").strip()
            if action == "add" and target:
                self.blacklist_groups.add(target)
                # 互斥: 加黑名单 → 从白名单自动移除 (老板要求: 黑白名单不能同时存在)
                if target in self.allow_users:
                    self.allow_users.discard(target)
                    removed_note = f"\n(已自动从白名单移除 {target})"
                else:
                    removed_note = ""
                self._save_whitelist_file()  # 持久化
                await reply(f"✅ 已加入黑名单群: {target}{removed_note}\n当前黑名单 ({len(self.blacklist_groups)}): {', '.join(sorted(self.blacklist_groups)) or '(空)'}")
            elif action == "del" and target:
                if target in self.blacklist_groups:
                    self.blacklist_groups.discard(target)
                    self._save_whitelist_file()  # 持久化
                    await reply(f"✅ 已移出黑名单群: {target}\n当前黑名单 ({len(self.blacklist_groups)}): {', '.join(sorted(self.blacklist_groups)) or '(空)'}")
                else:
                    await reply(f"❌ {target} 不在黑名单中")
            elif action == "list":
                await reply(f"当前黑名单群 ({len(self.blacklist_groups)}): {', '.join(sorted(self.blacklist_groups)) or '(空)'}")
            else:
                await reply("用法: /blacklist add <群ID> | del <群ID> | list")
            return

        if cmd == "/at":
            mode = (args[0] if args else "").strip().lower()
            if mode in ("on", "1", "true"):
                self.require_at_mention = True
                self._save_whitelist_file()  # 持久化
                await reply("✅ 群聊已设为必须 @ 才回复")
            elif mode in ("off", "0", "false"):
                self.require_at_mention = False
                self._save_whitelist_file()  # 持久化
                await reply("✅ 群聊已解除必须 @ (白名单群内非@也可触发)")
            else:
                await reply(f"用法: /at on|off\n当前: {'on (必须@)' if self.require_at_mention else 'off (不必@)'}")
            return

        await reply(f"未知命令: {cmd}\n用 /help 查看全部命令。")

    def _is_allowed(self, from_wxid: str, is_group: bool, chatroom_id: str, content: str, recipient_id: str = "") -> bool:
        """白名单 + 群消息策略判断:
        - 私聊: from_wxid 在 allow_users 才放行 (allow_users 空则全放行)
        - 群: 按 group_reply 策略 (atbot=被@才放行 / none=忽略 / all=全放行)
        - 黑名单群: 永远拒绝 (优先于白名单)
        """
        if is_group:
            # 黑名单群: 永远不回复 (filehelper /blacklist 管理)
            if chatroom_id and chatroom_id in self.blacklist_groups:
                return False
            # 群白名单: 白名单里的群 (allow_users 含 @chatroom) 优先检查
            # 若配置了白名单且群不在白名单 → 忽略 (与私聊一致)
            if self.allow_users:
                group_whitelisted = chatroom_id in self.allow_users
                # 白名单含群时, 只处理白名单群
                has_group_in_whitelist = any(u.endswith("@chatroom") for u in self.allow_users)
                if has_group_in_whitelist and not group_whitelisted:
                    return False
            if self.group_reply == "none":
                return False
            # require_at_mention: 非@消息直接拒绝 (即使 group_reply=all 也拦截), 仿 wpp-openclaw
            if self.require_at_mention and "@" not in content:
                return False
            if self.group_reply == "all":
                return True
            # atbot: 检测 @ 了机器人 (wxid 或 昵称)
            # 优先用消息自带的 recipient_id (当前会话机器人身份, 最可靠), fallback _self_wxid
            self_wxid = _safe_str(recipient_id) or _safe_str(self._self_wxid)
            self_nickname = _safe_str(self._self_nickname)
            # 兜底: vendor 明确给出 recipient_id 且等于 bot wxid + content 含 @ → 被@ 强信号
            # (即使 _self_nickname 因启动竞态未填充, 群@也能放行; 昵称在 content 里时 wxid 匹配不上)
            if recipient_id and recipient_id == _safe_str(self._self_wxid) and "@" in content:
                return True
            if self_wxid or self_nickname:
                # vendor 群消息 @ 格式: wxid@昵称 / @wxid / @昵称 / XML
                if self_wxid and (self_wxid in content or f"@{self_wxid}" in content):
                    return True
                if self_wxid and self_wxid + "@" in content:
                    return True
                # 昵称匹配: @接晓银 形式 (vendor 用昵称 @)
                if self_nickname and (
                    f"@{self_nickname}" in content or self_nickname in content
                ):
                    return True
                return False
            # 无 selfWxid 时保守: 不处理群消息
            return False
        # 私聊
        if not self.allow_users:
            return True
        return from_wxid in self.allow_users

    def _handle_msg(self, abm: AstrBotMessage) -> None:
        """构造事件并提交到 AstrBot 事件队列。

        用 WppMessageEvent (重写 send) — 否则 AstrBot 回复到 event.send() 不发到微信。
        """
        from .wpp_event import WppMessageEvent
        event = WppMessageEvent(
            message_str=abm.message_str,
            message_obj=abm,
            platform_meta=self.meta(),
            session_id=abm.session_id,
            api=self._api,
        )
        self.commit_event(event)

    # ------------------------------------------------------------------ 转换
    def _to_astrbot_message(self, payload: dict) -> AstrBotMessage | None:
        """兼容: payload → src → 转换。"""
        src = self._extract_msg_src(payload)
        if src is None:
            return None
        return self._to_astrbot_message_src(src)

    def _maybe_inject_at_component(self, src: dict, content: str, msg_type: int, abm: AstrBotMessage) -> None:
        """群消息 @ 了机器人 → 注入 At 组件 (AstrBot 唤醒判定依赖 At 组件)。

        背景: vendor 的 @ 是文本 (@wxid / @昵称), WakingCheckStage 只看 message chain 里的
              At 组件 (message.qq == self_id) 判断是否被@。纯文本 @ → is_wake=False → 主 agent 不回复。
        注入时机: 文本/app/文件等有 content 的群消息, 且 content 含 @机器人 (wxid 或 昵称)。
        """
        if abm.type != MessageType.GROUP_MESSAGE:
            return
        if not content or "@" not in content:
            return
        # 仅在文本/app 等有 content 的消息注入; 图片/视频/语音无 @ 语义
        if msg_type not in (MSG_TYPE_TEXT, MSG_TYPE_APP, MSG_TYPE_RELAY):
            return

        from astrbot.api.message_components import At

        recipient_id = _safe_str(src.get("recipient_id") or src.get("toWxid") or src.get("ToWxid"))
        # self_id 已由 _to_astrbot_message_src 设置为 to_wxid; 可能为空则 fallback _self_wxid
        self_id = abm.self_id or _safe_str(self._self_wxid)
        if not self_id:
            return
        # 匹配 @wxid 形式 (@q139198824) 或 @昵称 形式 (@接晓银)
        mentioned = (
            f"@{self_id}" in content
            or self_id + "@" in content
            or (self._self_nickname and f"@{self._self_nickname}" in content)
            or (self._self_nickname and self._self_nickname + "@" in content)
        )
        if not mentioned:
            return
        # 注入 At 组件到 chain 头部 (不影响原有 Plain 文本)
        # 注意: _to_astrbot_message_src 可能还没给 abm.message 赋值, 用 getattr 安全处理
        at_comp = At(qq=self_id)
        msg_list = getattr(abm, "message", None)
        if isinstance(msg_list, list):
            msg_list.insert(0, at_comp)
        else:
            abm.message = [at_comp]

    def _to_astrbot_message_src(self, src: dict) -> AstrBotMessage | None:
        """单条消息 src → AstrBotMessage。"""
        from_wxid = _safe_str(src.get("fromUser") or src.get("fromWxid") or src.get("FromWxid") or src.get("sender_id"))
        if not from_wxid:
            return None

        from_nick = _safe_str(
            src.get("fromNick") or src.get("fromNickname") or src.get("FromNick")
        ) or from_wxid
        to_wxid = _safe_str(src.get("toWxid") or src.get("ToWxid") or src.get("recipient_id"))
        chatroom_id = _safe_str(src.get("chatroomId") or src.get("ChatroomId"))
        # v1: is_group 布尔 + conversation_id (群消息时 conversation_id 是群ID)
        v1_is_group = src.get("is_group")
        if not chatroom_id and (v1_is_group is True or v1_is_group == "true" or v1_is_group == 1):
            chatroom_id = _safe_str(src.get("conversation_id"))
        # v1 私聊: 过滤自己发给自己的 (direction=incoming, sender=自己), 系统/公众号 (gh_)
        direction = _safe_str(src.get("direction"))
        if direction and direction not in ("incoming", "1"):
            return None  # 只处理收到的消息
        if from_wxid.startswith("gh_"):
            return None  # 公众号/服务号忽略

        msg_type = _safe_num(src.get("msgType") or src.get("MsgType") or src.get("type"), MSG_TYPE_TEXT)
        content = _safe_str(src.get("content") or src.get("Content") or src.get("text"))
        msg_id = _safe_str(src.get("msgId") or src.get("MsgId") or src.get("svr_id") or src.get("id"))
        ts = _safe_num(src.get("createTime") or src.get("CreateTime") or src.get("created_at"), 0)
        if ts > 1e12:  # ms → s
            ts = ts // 1000

        is_group = bool(chatroom_id)

        abm = AstrBotMessage()
        abm.type = MessageType.GROUP_MESSAGE if is_group else MessageType.FRIEND_MESSAGE
        abm.self_id = to_wxid
        abm.message_id = msg_id or f"wpp-{ts}"
        abm.sender = MessageMember(user_id=from_wxid, nickname=from_nick)
        abm.raw_message = src
        abm.timestamp = int(ts)
        if is_group:
            abm.group = Group(group_id=chatroom_id)
            abm.session_id = chatroom_id
        else:
            abm.session_id = from_wxid

        # 文本: Plain; 图片: 加 Image 占位 (数据在 _handle_webhook 异步下载后注入)
        # 语音: vendor 自带 voice.transcript (微信官方转写), 直接作文本, 无需 STT
        if msg_type == MSG_TYPE_TEXT:
            abm.message_str = content
            abm.message = [Plain(content)]
        elif msg_type == MSG_TYPE_FILE or (src.get("kind") == "app" and isinstance(src.get("app"), dict) and src.get("app", {}).get("category") == "file"):
            # v1 文件: kind=app + app.category=file; file.download_context 下载凭证
            file_obj = src.get("file") if isinstance(src.get("file"), dict) else {}
            app_obj = src.get("app") if isinstance(src.get("app"), dict) else {}
            filename = _safe_str(app_obj.get("title")) or _safe_str(file_obj.get("name")) or _safe_str(file_obj.get("filename")) or content
            dc = file_obj.get("download_context") if isinstance(file_obj.get("download_context"), dict) else {}
            attach_id = _safe_str(dc.get("attach_id"))
            user_name = _safe_str(dc.get("user_name"))
            data_len = _safe_num(dc.get("data_len"), 0)
            abm.message_str = f"[文件] {filename}"
            abm.message = [Plain(abm.message_str)]
            abm._wpp_file_meta = {
                "filename": filename,
                "attach_id": attach_id,
                "user_name": user_name,
                "data_len": data_len,
            }
            logger.info(f"[WPP] file msg: name={filename[:30]} attach_id={attach_id[:20] or '-'} len={data_len}")
        elif msg_type == MSG_TYPE_VOICE:
            voice_obj = src.get("voice") if isinstance(src.get("voice"), dict) else {}
            transcript = _safe_str(voice_obj.get("transcript")) or _safe_str(voice_obj.get("text"))
            if transcript:
                # vendor 已转好文字 (微信官方), 直接给纯文字 (不加 [语音] 前缀, 防 AI 误读为音频)
                abm.message_str = transcript
                abm.message = [Plain(transcript)]
                logger.info(f"[WPP] voice transcript: {transcript[:50]}")
            else:
                abm.message_str = "[语音]"
                abm.message = []
                logger.info(f"[WPP] voice no transcript, voice keys: {list(voice_obj.keys())[:10]}")
        elif msg_type == MSG_TYPE_IMAGE:
            abm.message_str = content or "[图片]"
            # 图片下载凭证: 优先 CDN (cdn_download_contexts), 兜底 DownloadImg (local_id)
            img_obj = src.get("image") if isinstance(src.get("image"), dict) else {}
            local_id = _safe_str(src.get("local_id")) or msg_id
            data_len = _safe_num(img_obj.get("data_len"), 0)
            cdn_ctx = None
            cdn_list = img_obj.get("cdn_download_contexts")
            if isinstance(cdn_list, list) and cdn_list:
                for item in cdn_list:
                    if isinstance(item, dict) and item.get("file_aes_key") and item.get("file_no"):
                        cdn_ctx = {"file_aes_key": str(item["file_aes_key"]), "file_no": str(item["file_no"])}
                        break
            abm.raw_message = src
            abm.message = []  # 图片数据由 _handle_webhook 异步填充
            abm._wpp_image_meta = {
                "msg_id": local_id,
                "to_wxid": to_wxid or (chatroom_id or ""),
                "data_len": data_len,
                "cdn": cdn_ctx,
            }
            abm.message_str = "[图片]"
        elif msg_type == MSG_TYPE_VIDEO or src.get("kind") == "video":
            # v1 视频: kind=video + video.download_context {msg_id, data_len, to_wxid}
            video_obj = src.get("video") if isinstance(src.get("video"), dict) else {}
            vc = video_obj.get("download_context") if isinstance(video_obj.get("download_context"), dict) else {}
            abm.message_str = content or "[视频]"
            abm.message = [Plain(f"[视频] {content or ''}".strip())]
            abm._wpp_video_meta = {
                "msg_id": _safe_str(vc.get("msg_id")),
                "data_len": _safe_num(vc.get("data_len"), 0),
                "to_wxid": _safe_str(vc.get("to_wxid")),
                "compress_type": _safe_num(vc.get("compress_type"), 0),
            }
            logger.info(f"[WPP] video msg: meta={abm._wpp_video_meta}")
        elif msg_type == MSG_TYPE_APP and content:
            # type=49 app 消息: 引用/链接/卡片等, 有 content 就作文本处理 (@接晓银 这类)
            abm.message_str = content
            abm.message = [Plain(content)]
            logger.info(f"[WPP] app msg text: {content[:40]}")
        else:
            abm.message_str = content
            abm.message = []
            logger.debug(f"[WPP] non-text msgType={msg_type} id={msg_id} (待扩展)")

        # 群@唤醒关键: AstrBot WakingCheckStage 靠 message chain 里的 At 组件判断"被@了"
        # vendor 的 @ 是文本形式 (@wxid / @昵称), 若不转成 At 组件 → is_wake=False → 主 agent 不回复
        # 必须在所有 message 赋值之后注入 (否则后续赋值会覆盖), 且只在 @ 了机器人时注入
        self._maybe_inject_at_component(src, content, msg_type, abm)

        return abm

    @staticmethod
    def _extract_msg_src(payload: dict) -> dict | None:
        """兼容多种 push 形态 (vendor 2026-08-21 实际推 v1 messages 格式):
        A. v1: {Wxid, Data:{messages:[{sender_id, recipient_id, content, type, is_group, direction,...}], schema}}
        B. 业务回调: {Wxid, EventType:sync_message, Data:{Data:{AddMsgs:[{...}]}}}
        C. 扁平 webhook: {fromUser, msgType, content, ...}
        D. {Wxid, Data:{...消息字段...}}
        """
        if not isinstance(payload, dict):
            return None

        # 形态 A (最优先): v1 messages 格式
        data = payload.get("Data")
        if isinstance(data, dict):
            msgs = data.get("messages")
            if isinstance(msgs, list) and msgs:
                first = msgs[0]
                if isinstance(first, dict):
                    return first
            items = data.get("items")
            if isinstance(items, list) and items:
                first = items[0]
                if isinstance(first, dict):
                    return first

        # 形态 B: EventType= sync_message 业务回调
        if payload.get("EventType") == "sync_message":
            data_outer = payload.get("Data")
            if isinstance(data_outer, dict):
                data_inner = data_outer.get("Data")
                if isinstance(data_inner, dict):
                    add_msgs = data_inner.get("AddMsgs")
                    if isinstance(add_msgs, list) and add_msgs:
                        first = add_msgs[0]
                        if isinstance(first, dict):
                            return first
            return None

        # 形态 C: 扁平 push
        if any(k in payload for k in ("fromUser", "fromWxid", "MsgId", "msgId")):
            return payload

        # 形态 D: {Wxid, Data:{...消息字段...}}
        if isinstance(data, dict):
            if any(k in data for k in ("fromUser", "fromWxid", "MsgId", "Content", "Text")):
                return data
        return None

    @staticmethod
    def _extract_all_msg_src(payload: dict) -> list[dict]:
        """提取 payload 里所有消息 (兼容多消息推送, 不丢失后面的文件/图片等)。"""
        if not isinstance(payload, dict):
            return []

        out: list[dict] = []
        data = payload.get("Data")

        # 形态 A: v1 messages / items 数组
        if isinstance(data, dict):
            for kk in ("messages", "items"):
                arr = data.get(kk)
                if isinstance(arr, list):
                    for item in arr:
                        if isinstance(item, dict):
                            out.append(item)
        # 形态 B: AddMsgs 数组
        if not out and payload.get("EventType") == "sync_message":
            data_outer = payload.get("Data")
            if isinstance(data_outer, dict):
                data_inner = data_outer.get("Data")
                if isinstance(data_inner, dict):
                    add_msgs = data_inner.get("AddMsgs")
                    if isinstance(add_msgs, list):
                        for item in add_msgs:
                            if isinstance(item, dict):
                                out.append(item)
        # 形态 C/D: 扁平单条
        if not out:
            single = WppPlatformAdapter._extract_msg_src(payload)
            if single:
                out.append(single)
        return out

    # ------------------------------------------------------------------ 发送
    async def send_by_session(
        self, session: MessageSesion, message_chain: MessageChain
    ) -> None:
        """AstrBot 回复消息统一走这里 → WPP 发送 API。"""
        text_parts = []
        image_parts = []
        for comp in message_chain.chain:
            t = getattr(comp, "type", None)
            type_name = getattr(t, "value", t) if t else None
            if type_name == "Plain":
                text_parts.append(str(getattr(comp, "text", "")))
            elif type_name == "Image":
                image_parts.append(getattr(comp, "file", "") or getattr(comp, "url", ""))

        to_wxid = session.session_id
        logger.info(f"[WPP] send_by_session called: to={to_wxid} text_parts={text_parts} img_parts={len(image_parts)}")

        for img in image_parts:
            try:
                await self._api.send_image(to_wxid, img)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[WPP] send_image failed: {e}")
        if text_parts:
            text = "\n".join(text_parts)
            try:
                r = await self._api.send_text(to_wxid, text)
                logger.info(f"[WPP] send_text OK: {str(r.get('Message'))[:80]}")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[WPP] send_text failed: {e}")

    async def send_message(self, message: MessageChain, session: MessageSesion) -> None:
        """兼容旧式 send_message 调用。"""
        await self.send_by_session(session, message)
