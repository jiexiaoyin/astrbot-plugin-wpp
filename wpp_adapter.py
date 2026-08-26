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

v0.2.0 多账号: 单实例内管理多个微信账号 (WppAccount), 每账号一个 authcode + 独立
WS 连接 + 独立白名单 (accounts/<account_id>/wpp_whitelist.json)。同一 vendor 地址
(base_url/ws_url), 通过不同 authcode 隔离账号。单账号 (wpp_auth_token) 向后兼容。
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
    Platform,
    PlatformMetadata,
    register_platform_adapter,
)
from astrbot.core.platform.astr_message_event import MessageSesion

from .wpp_account import WppAccount

# 单账号向后兼容: 顶层配置字段 (wpp_auth_token 等) 作为唯一账号的配置来源
# 多账号: wpp_accounts JSON 列表, 每个元素 {id, authcode, allow_users, group_reply, ...}
# 注意: webhook 已退役 (wpp_webhook_enabled=false), 多账号时 webhook 无意义 (WS 是主通道)


def _safe_str(v: Any) -> str:
    return "" if v is None else str(v)


def _parse_accounts(platform_config: dict) -> list[dict]:
    """解析账号配置列表。

    优先 wpp_accounts (JSON 数组); 为空则回退到单账号 (wpp_auth_token + 顶层白名单)。
    返回 [{id, authcode, allow_users, group_reply, blacklist_groups, require_at_mention, ...}]
    """
    base_url = str(platform_config.get("wpp_base_url", "http://127.0.0.1:18062")).rstrip("/")
    ws_url = str(platform_config.get("wpp_ws_url", "") or "").strip()

    raw = platform_config.get("wpp_accounts", "") or ""
    if isinstance(raw, str):
        raw = raw.strip()
        accounts = []
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    accounts = parsed
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[WPP] wpp_accounts JSON 解析失败, 回退单账号: {e}")
    elif isinstance(raw, list):
        accounts = raw
    else:
        accounts = []

    if accounts:
        # 多账号模式: 每账号独立 authcode
        out = []
        for i, acct in enumerate(accounts):
            if not isinstance(acct, dict):
                continue
            aid = str(acct.get("id") or f"account{i}")
            authcode = str(acct.get("authcode") or acct.get("wpp_auth_token") or "").strip()
            if not authcode:
                continue
            out.append({
                "id": aid,
                "authcode": authcode,
                "allow_users": str(acct.get("allow_users", "") or "").strip(),
                "group_reply": str(acct.get("group_reply", "atbot") or "atbot"),
                "blacklist_groups": str(acct.get("blacklist_groups", "") or "").strip(),
                "require_at_mention": str(acct.get("require_at_mention", "false") or "false").lower() in ("1", "true", "yes", "on"),
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
            "allow_users": str(platform_config.get("wpp_allow_users", "") or "").strip(),
            "group_reply": str(platform_config.get("wpp_group_reply", "atbot") or "atbot"),
            "blacklist_groups": "",
            "require_at_mention": False,
            "base_url": base_url,
            "ws_url": ws_url,
        }]
    return []


def _split_users(s: str) -> set[str]:
    return {u.strip() for u in s.split(",") if u.strip()}


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
        "isSecret": True,
        "hint": "仅单账号时用。多账号请用 wpp_accounts JSON 列表。绑定微信账号的 authcode",
    },
    "wpp_accounts": {
        "description": "多账号模式: 账号列表 (JSON 数组)",
        "type": "string",
        "hint": '多账号时用。JSON 数组, 每元素: {"id":"xieyin","authcode":"xxx","allow_users":"","group_reply":"atbot","blacklist_groups":"","require_at_mention":false}。留空=单账号(wpp_auth_token)',
    },
    "wpp_allow_users": {
        "description": "单账号模式: 白名单 wxid (逗号分隔)",
        "type": "string",
        "hint": "仅单账号时用。多账号的白名单在 wpp_accounts 每账号里配",
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
        "type": "boolean",
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
        "wpp_group_reply": "atbot",
        "wpp_ws_url": "",
        "wpp_webhook_enabled": "false",
    },
    config_metadata=WPP_CONFIG_METADATA,
)
class WppPlatformAdapter(Platform):
    """单实例多账号: 管理多个 WppAccount (每账号一个 authcode + WS + 白名单)。

    平台级职责: 账号解析/生命周期/meta/send_by_session/webhook 兜底。
    账号级职责 (WppAccount): WS 连接/消息处理/白名单/filehelper/去重。
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
        self.webhook_enabled = str(platform_config.get("wpp_webhook_enabled", "false") or "false").lower() in ("1", "true", "yes", "on")

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
                allow_users=_split_users(acfg.get("allow_users", "")),
                group_reply=acfg.get("group_reply", "atbot"),
                blacklist_groups=_split_users(acfg.get("blacklist_groups", "")),
                require_at_mention=acfg.get("require_at_mention", False),
            )
            self.accounts.append(acct)
            logger.info(f"[WPP] 账号已注册: id={acct.account_id} base={acct.base_url} ws={'有' if acct.ws_url else '无'}")

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
        # name 保持 "wpp" (平台类型), id 用实例 id (AstrBot 会话前缀/路由区分实例)
        return PlatformMetadata(
            name="wpp",
            description="微信 WPP 适配器 (WeChatPadPro)",
            id=self.instance_id,
            adapter_display_name="微信 WPP",
        )

    def get_stats(self) -> dict:
        stats = super().get_stats()
        # 聚合所有账号状态
        accts = []
        for acct in self.accounts:
            accts.append({
                "id": acct.account_id,
                "status": dict(acct._account_status),
            })
        stats["accounts"] = accts
        if self.accounts:
            stats["meta"]["description"] = f"微信 WPP · {len(self.accounts)} 账号"
        return stats

    # ------------------------------------------------------------------ 生命周期
    async def run(self) -> None:
        """启动所有账号的 WS 实时通道 (+ 可选 webhook 兜底)。"""
        # ① 每个账号: 确认在线 + 刷新身份 + 启动 WS
        for acct in self.accounts:
            try:
                await self._ensure_logged_in(acct)
                await self._refresh_account_status(acct)
                acct.start_ws()
                acct._reload_config_loop()  # 白名单文件热更新
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
        logger.info(f"[WPP] webhook server started at http://{self.webhook_host}:{self.webhook_port}{self.webhook_path}")

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
                            logger.info(f"[WPP:{acct.account_id}] 长连接已断, 触发自动心跳: {hb.get('Message')}")
                            await asyncio.sleep(2)
                            online2, _ = await acct._api.is_online()
                            if online2:
                                logger.info(f"[WPP:{acct.account_id}] 自动心跳拉起成功")
                        else:
                            logger.warning(f"[WPP:{acct.account_id}] 账号未登录 (Code={resp.get('Code')})")
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
            wxid = data.get("wxid", "")
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
                    "online": True, "wxid": wxid, "nickname": nickname,
                    "message": f"在线 · {nickname} ({wxid})",
                }
            else:
                acct._account_status = {
                    "online": False, "wxid": wxid, "nickname": "",
                    "message": "未登录",
                }
        except Exception as e:  # noqa: BLE001
            acct._account_status = {
                "online": None, "wxid": "", "nickname": "",
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
                    logger.info(f"[WPP:{acct.account_id}] 心跳后账号已在线: wxid={data.get('wxid')}")
                    return
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[WPP:{acct.account_id}] auto_heartbeat failed: {e}")

        logger.warning(
            f"[WPP:{acct.account_id}] 账号未在线 (微信未在 vendor 成功建立连接), "
            f"请到 WPP 管理面板检查登录状态: {acct.base_url}"
        )

    async def terminate(self) -> None:
        """清理: 所有账号的 WS 任务 + 状态循环 + webhook server。"""
        if self._status_task:
            self._status_task.cancel()
            try:
                await self._status_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        for acct in self.accounts:
            try:
                await acct.stop()
            except Exception:  # noqa: BLE001
                pass
        if self._runner:
            await self._runner.cleanup()
            self._runner = None

    # ------------------------------------------------------------------ webhook 兜底
    async def _handle_webhook_health(self, request: Any) -> Any:
        from aiohttp import web
        return web.Response(text="ok")

    async def _handle_webhook(self, request: Any) -> Any:
        """接收 WPP vendor 消息 push (webhook 已退役, 多账号时按 authcode 路由到账号)。

        多账号时 webhook 无法可靠区分账号 (vendor 注册的是单 URL), 主要靠 WS 通道。
        这里保留做兼容: 消息 src 里带 recipient_id/wxid 可尝试定位账号。
        """
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

        # 遍历消息, 按 recipient_id 路由到账号; 找不到则用第一个账号 (兼容)
        for src in WppAccount._extract_all_msg_src(payload):
            recipient_id = _safe_str(src.get("recipient_id") or src.get("toWxid") or src.get("ToWxid"))
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
    def _find_account_for_send(self, session: MessageSesion) -> WppAccount | None:
        """定位回复所属账号。

        WppMessageEvent 已注入对应账号的 _api, 所以正常回复不经过这里。
        这里主要处理主动推送 (cron/context.send_message): 用 session.platform_name
        匹配实例 id (单实例多账号时 platform_name 是实例 id, 无法区分账号)。
        简化: 单账号返回唯一账号; 多账号返回第一个 (主动推送场景)。
        """
        if len(self.accounts) == 1:
            return self.accounts[0]
        return self.accounts[0] if self.accounts else None

    async def send_by_session(
        self, session: MessageSesion, message_chain: MessageChain
    ) -> None:
        """AstrBot 回复消息统一走这里 → 对应账号的 WPP 发送 API。"""
        acct = self._find_account_for_send(session)
        if acct is None:
            logger.warning("[WPP] 无可用账号, 发送失败")
            return

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
        logger.info(f"[WPP:{acct.account_id}] send_by_session: to={to_wxid}")

        for img in image_parts:
            try:
                await acct._api.send_image(to_wxid, img)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[WPP:{acct.account_id}] send_image failed: {e}")
        if text_parts:
            text = "\n".join(text_parts)
            try:
                r = await acct._api.send_text(to_wxid, text)
                logger.info(f"[WPP:{acct.account_id}] send_text OK: {str(r.get('Message'))[:80]}")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[WPP:{acct.account_id}] send_text failed: {e}")

    async def send_message(self, message: MessageChain, session: MessageSesion) -> None:
        """兼容旧式 send_message 调用。"""
        await self.send_by_session(session, message)


def _webhook_token_for(instance_id: str) -> str:
    """按实例 id 加载/创建 webhook token (webhook 已退役, 保留正确性)。"""
    from pathlib import Path
    import os

    plugin_dir = Path(__file__).resolve().parent
    account_dir = plugin_dir / "accounts" / instance_id
    account_dir.mkdir(parents=True, exist_ok=True)
    token_file = account_dir / "webhook_token"
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
        os.chmod(token_file, 0o600)
    except Exception:  # noqa: BLE001
        pass
    return tok
