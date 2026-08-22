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
import json
from typing import Any

from astrbot import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
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
        "description": "Webhook 路径",
        "type": "string",
        "hint": "默认 /wpp/webhook",
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
        self.webhook_path = str(platform_config.get("webhook_path", "/wpp/webhook"))
        self.webhook_public_url = str(platform_config.get("webhook_public_url", "") or "").rstrip("/")

        # 白名单配置: wpp_allow_users 逗号分隔 wxid; 空则默认允许所有人
        allow_str = str(platform_config.get("wpp_allow_users", "") or "").strip()
        self.allow_users = {u.strip() for u in allow_str.split(",") if u.strip()}
        # 群消息回复模式: atbot / none / all
        self.group_reply = str(platform_config.get("wpp_group_reply", "atbot") or "atbot")

        self._api = WppClient(self.base_url, self.auth_token)
        self._runner = None
        self._server = None
        # 账号状态缓存 (get_stats 用, 后台定时刷新)
        self._account_status = {"online": None, "wxid": "", "nickname": "", "message": "查询中..."}
        self._status_task: asyncio.Task | None = None
        # 机器人自身 wxid (群@判断用, 从 GetOnlineInfo 刷新)
        self._self_wxid = ""

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

    async def terminate(self) -> None:
        if self._status_task:
            self._status_task.cancel()
        if self._runner:
            await self._runner.cleanup()

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
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[WPP] register webhook to vendor failed: {e}")

        # ③ 启动账号状态后台刷新任务 (供 get_stats / Dashboard 卡片展示)
        await self._refresh_account_status()
        self._status_task = asyncio.create_task(self._account_status_loop())

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
        if self._runner:
            await self._runner.cleanup()

    # ------------------------------------------------------------------ webhook
    async def _handle_webhook_health(self, request: Any) -> Any:
        from aiohttp import web
        return web.Response(text="ok")

    async def _handle_webhook(self, request: Any) -> Any:
        """接收 WPP vendor 消息 push。"""
        from aiohttp import web

        try:
            raw = await request.text()
            payload = json.loads(raw)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[WPP] bad webhook payload: {e}")
            return web.Response(status=200)  # 200 防 vendor 重试轰炸

        # 遍历所有消息 (vendor 一次可能推多条, 只取第一条会漏文件/图片等)
        for src in self._extract_all_msg_src(payload):
            await self._process_src(src)
        return web.Response(status=200)

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
        # v1: 只处理 incoming; 忽略公众号 (gh_)
        direction = _safe_str(src.get("direction"))
        if direction and direction not in ("incoming", "1"):
            return
        if from_wxid.startswith("gh_"):
            return

        if not self._is_allowed(from_wxid, is_group, chatroom_id, content):
            logger.debug(f"[WPP] 消息被白名单/群策略过滤: from={from_wxid} group={chatroom_id or '-'}")
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
                            import os, tempfile
                            tmp = os.path.join(tempfile.gettempdir(), f"wpp_file_{fname[:20]}")
                            with open(tmp, "wb") as f:
                                f.write(file_bytes)
                            msg.message = [Plain(f"[文件] {fname}"), File(name=fname, file=tmp)]
                            msg.message_str = f"[文件] {fname}"
                            logger.info(f"[WPP] file downloaded: {fname} ({len(file_bytes)} bytes)")
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
                            import os, tempfile
                            tmp = os.path.join(tempfile.gettempdir(), f"wpp_video_{abs(hash(video_meta.get('msg_id','')))}.mp4")
                            with open(tmp, "wb") as f:
                                f.write(video_bytes)
                            msg.message = [Plain(f"[视频] {len(video_bytes)} bytes"), Video(file=tmp)]
                            msg.message_str = "[视频]"
                            logger.info(f"[WPP] video downloaded: {len(video_bytes)} bytes")
                        else:
                            logger.warning(f"[WPP] video download failed: {video_meta}")
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[WPP] video download error: {e}")

            self._handle_msg(msg)

    def _is_allowed(self, from_wxid: str, is_group: bool, chatroom_id: str, content: str) -> bool:
        """白名单 + 群消息策略判断:
        - 私聊: from_wxid 在 allow_users 才放行 (allow_users 空则全放行)
        - 群: 按 group_reply 策略 (atbot=被@才放行 / none=忽略 / all=全放行)
        """
        if is_group:
            if self.group_reply == "none":
                return False
            if self.group_reply == "all":
                return True
            # atbot: 检测 @ 了机器人 (selfWxid)
            self_wxid = _safe_str(self._self_wxid)
            if self_wxid:
                # vendor 群消息 @ 格式: wxid@昵称 或 @wxid 或 XML
                if self_wxid in content or f"@{self_wxid}" in content:
                    return True
                # 也匹配 wxid@xxx 形式 (群@通知)
                if self_wxid + "@" in content:
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
        else:
            abm.message_str = content
            abm.message = []
            logger.debug(f"[WPP] non-text msgType={msg_type} id={msg_id} (待扩展)")

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
            single = _extract_msg_src(payload)
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
