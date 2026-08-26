"""WPP 单实例多账号: 单个微信账号 (一个 authcode) 的封装 (v0.2.0)。

每个 WppAccount 持有该账号独立的:
  - API 客户端 (WppClient, base_url + 该账号 authcode)
  - WS 实时通道任务 (连 vendor /ws/sync, 该账号 authcode 鉴权)
  - 白名单/群策略/黑名单 (accounts/<account_id>/wpp_whitelist.json 持久化)
  - 去重状态 (_seen_msg_ids)
  - 机器人身份 (_self_wxid/_self_nickname)
  - filehelper 命令注册表

消息处理逻辑 (_process_src 等) 是账号级方法 — 操作本账号状态, 提交事件经
platform.commit_event() (平台级引用)。

参考 wpp-openclaw AccountRegistry 模式 (一 authcode = 一账号, 独立 WS + 独立白名单)。
"""

import asyncio
import json
from pathlib import Path
from typing import Any

from astrbot import logger
from astrbot.api.message_components import Plain
from astrbot.api.platform import (
    AstrBotMessage,
    Group,
    MessageMember,
    MessageType,
)

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


class WppAccount:
    """单个微信账号的完整封装 (一个 authcode = 一个账号)。

    platform: WppPlatformAdapter 引用 (用于 commit_event 提交事件到 AstrBot)。
    """

    def __init__(
        self,
        platform,
        account_id: str,
        authcode: str,
        base_url: str,
        ws_url: str,
        allow_users: set[str] | None = None,
        group_reply: str = "atbot",
        blacklist_groups: set[str] | None = None,
        require_at_mention: bool = False,
    ) -> None:
        self.platform = platform
        self.account_id = account_id
        self.authcode = authcode
        self.base_url = base_url
        self.ws_url = ws_url

        # API 客户端 (该账号 authcode)
        self._api = WppClient(self.base_url, self.authcode)

        # WS 实时通道任务
        self._ws_task: asyncio.Task | None = None

        # 白名单/群策略 (账号级, 独立)
        self.allow_users: set[str] = set(allow_users or ())
        self.group_reply = group_reply
        self.blacklist_groups: set[str] = set(blacklist_groups or ())
        self.require_at_mention = require_at_mention

        # 白名单持久化文件 (accounts/<account_id>/wpp_whitelist.json)
        self._whitelist_file = self._get_whitelist_file_path()
        self._load_whitelist_file()
        self._whitelist_mtime = self._get_file_mtime()
        self._reload_task: asyncio.Task | None = None

        # 账号状态缓存
        self._account_status = {"online": None, "wxid": "", "nickname": "", "message": "查询中..."}
        self._self_wxid = ""
        self._self_nickname = ""

        # 消息去重 (账号级, 独立)
        self._seen_msg_ids: set[str] = set()
        self._seen_msg_ids_order: list[str] = []
        self._dedup_max = 200

        # filehelper 命令注册表 (账号级)
        self._filehelper_commands: list[tuple[str, str, Any]] = [
            ("/user add|del <wxid> [wxid...]", "私聊白名单增删 (批量)", self._handle_user_command),
            ("/user list", "查看私聊白名单", self._handle_user_command),
            ("/group add|del <群ID> [群ID...]", "群白名单增删 (批量)", self._handle_group_command),
            ("/group list", "查看群白名单", self._handle_group_command),
            ("/blacklist add|del|list <群ID> [群ID...]", "黑名单群管理 (批量)", self._handle_blacklist_command),
            ("/mode atbot|none|all", "群回复模式", self._handle_mode_command),
            ("/at on|off", "群聊是否必须@才回复", self._handle_at_command),
            ("/help", "显示本帮助", self._handle_help_command),
        ]

    # ------------------------------------------------------------------ 生命周期
    def start_ws(self) -> None:
        """启动该账号的 WS 实时通道。"""
        if self.ws_url and self._ws_task is None:
            self._ws_task = asyncio.create_task(self._ws_client_loop())
            logger.info(f"[WPP:{self.account_id}] WS 实时通道已启动: {self.ws_url}")

    async def stop(self) -> None:
        """停止该账号的 WS 任务 + 配置热更新任务。"""
        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._ws_task = None
        if self._reload_task:
            self._reload_task.cancel()
            try:
                await self._reload_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._reload_task = None
        try:
            await self._api.close()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ WS 实时通道
    async def _ws_client_loop(self) -> None:
        """WS 实时消息客户端 (连 vendor /ws/sync, 该账号 authcode 鉴权, 复用 _process_src)。"""
        import websockets

        delay = 1.0
        while True:
            try:
                url = self.ws_url
                if self.authcode and "?" not in url:
                    url = f"{url}?authcode={self.authcode}"
                logger.info(f"[WPP:{self.account_id} WS] connecting: {self.ws_url}")
                async with websockets.connect(
                    url, ping_interval=20, ping_timeout=20, close_timeout=5, max_size=16 * 1024 * 1024
                ) as ws:
                    delay = 1.0  # 连接成功重置退避
                    logger.info(f"[WPP:{self.account_id} WS] connected")
                    async for raw in ws:
                        try:
                            payload = json.loads(raw)
                        except Exception:  # noqa: BLE001
                            continue
                        dtype = (payload.get("Data") or {}).get("type")
                        if dtype in ("connection_ready", "sync_update"):
                            continue
                        for src in self._extract_all_msg_src(payload):
                            try:
                                await self._process_src(src)
                            except Exception as e:  # noqa: BLE001
                                logger.warning(f"[WPP:{self.account_id} WS] _process_src failed: {e}")
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[WPP:{self.account_id} WS] 连接断开/异常, {delay}s 后重连: {e}")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)  # 指数退避 1s→30s 封顶

    # ------------------------------------------------------------------ 消息处理
    async def _process_src(self, src: dict) -> None:
        """处理单条消息: 白名单过滤 → 转换 → 媒体下载 → 提交事件。"""
        from_wxid = _safe_str(src.get("fromUser") or src.get("fromWxid") or src.get("FromWxid") or src.get("sender_id"))
        chatroom_id = _safe_str(src.get("chatroomId") or src.get("ChatroomId"))
        v1_is_group = src.get("is_group")
        if not chatroom_id and (v1_is_group is True or v1_is_group == "true" or v1_is_group == 1):
            chatroom_id = _safe_str(src.get("conversation_id"))
        content = _safe_str(src.get("content") or src.get("Content") or src.get("text"))
        is_group = bool(chatroom_id)
        recipient_id = _safe_str(src.get("recipient_id") or src.get("toWxid") or src.get("ToWxid"))
        conversation_id = _safe_str(src.get("conversation_id"))
        is_filehelper = (recipient_id == "filehelper") or (conversation_id == "filehelper")
        msg_id_key = _safe_str(src.get("msgId") or src.get("MsgId") or src.get("svr_id") or src.get("id"))
        if msg_id_key and msg_id_key in self._seen_msg_ids:
            logger.debug(f"[WPP:{self.account_id}] 重复消息已跳过 (dedup): id={msg_id_key}")
            return
        logger.debug(f"[WPP:{self.account_id}] 收到: from={from_wxid} group={chatroom_id or '-'} content={content[:40]!r} self_wxid={self._self_wxid!r}")
        if is_filehelper and content.startswith("/"):
            await self._handle_filehelper_command(content, from_wxid, recipient_id)
            return
        direction = _safe_str(src.get("direction"))
        if direction and direction not in ("incoming", "1"):
            return
        if from_wxid.startswith("gh_"):
            return
        if from_wxid and from_wxid == _safe_str(self._self_wxid):
            logger.debug(f"[WPP:{self.account_id}] 跳过自己消息: from={from_wxid}")
            return

        if not self._is_allowed(from_wxid, is_group, chatroom_id, content, recipient_id):
            logger.warning(f"[WPP:{self.account_id}] 消息被白名单/群策略过滤: from={from_wxid} group={chatroom_id or '-'}")
            return

        msg = self._to_astrbot_message_src(src)
        if msg:
            # 图片消息
            img_meta = getattr(msg, "_wpp_image_meta", None)
            if img_meta:
                try:
                    img_bytes = None
                    cdn = img_meta.get("cdn")
                    if cdn:
                        img_bytes = await self._api.download_image_cdn(
                            cdn.get("file_aes_key", ""),
                            cdn.get("file_no", ""),
                        )
                    if not img_bytes:
                        img_bytes = await self._api.download_image(
                            img_meta.get("msg_id", ""),
                            img_meta.get("to_wxid", ""),
                            img_meta.get("data_len", 0),
                        )
                    if img_bytes:
                        from astrbot.api.message_components import Image
                        msg.message = [Image.fromBytes(img_bytes)]
                        msg.message_str = "[图片]"
                    else:
                        logger.warning(f"[WPP:{self.account_id}] image download failed: {img_meta}")
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[WPP:{self.account_id}] image download error: {e}")

            # 文件消息
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
                            import os
                            from astrbot.core.utils.astrbot_path import get_astrbot_temp_path
                            base, ext = os.path.splitext(fname)
                            safe_base = base[:20] or "file"
                            tmp = os.path.join(get_astrbot_temp_path(), f"wpp_{safe_base}{ext}")
                            with open(tmp, "wb") as f:
                                f.write(file_bytes)
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
                        else:
                            logger.warning(f"[WPP:{self.account_id}] file download failed: {file_meta.get('filename')}")
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[WPP:{self.account_id}] file download error: {e}")

            # 视频消息
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
                            import os
                            from astrbot.core.utils.astrbot_path import get_astrbot_temp_path
                            tmp = os.path.join(get_astrbot_temp_path(), f"wpp_video_{abs(hash(video_meta.get('msg_id','')))}.mp4")
                            with open(tmp, "wb") as f:
                                f.write(video_bytes)
                            msg.message = [Plain(f"[视频] {len(video_bytes)} bytes"), Video(file=tmp)]
                            msg.message_str = "[视频]"
                        else:
                            logger.warning(f"[WPP:{self.account_id}] video download failed: {video_meta}")
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[WPP:{self.account_id}] video download error: {e}")

            self._record_seen_msg(msg_id_key)
            self._handle_msg(msg)

    def _record_seen_msg(self, msg_id_key: str) -> None:
        if not msg_id_key:
            return
        if msg_id_key in self._seen_msg_ids:
            return
        self._seen_msg_ids.add(msg_id_key)
        self._seen_msg_ids_order.append(msg_id_key)
        while len(self._seen_msg_ids_order) > self._dedup_max:
            old = self._seen_msg_ids_order.pop(0)
            self._seen_msg_ids.discard(old)

    def _handle_msg(self, abm: AstrBotMessage) -> None:
        """构造事件并提交到 AstrBot 事件队列 (经 platform.commit_event)。"""
        from .wpp_event import WppMessageEvent
        event = WppMessageEvent(
            message_str=abm.message_str,
            message_obj=abm,
            platform_meta=self.platform.meta(),
            session_id=abm.session_id,
            api=self._api,
        )
        self.platform.commit_event(event)

    # ------------------------------------------------------------------ 白名单持久化
    def _get_whitelist_file_path(self) -> str:
        """白名单持久化文件路径 (accounts/<account_id>/wpp_whitelist.json, 按账号隔离)。"""
        plugin_dir = Path(__file__).resolve().parent
        account_dir = plugin_dir / "accounts" / self.account_id
        account_dir.mkdir(parents=True, exist_ok=True)
        return str(account_dir / "wpp_whitelist.json")

    def _load_whitelist_file(self) -> None:
        """读取该账号的白名单持久化配置 (filehelper 命令写入)。"""
        try:
            p = Path(self._whitelist_file)
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    arr = data.get("allow_users", [])
                    if isinstance(arr, list):
                        self.allow_users.update(str(u).strip() for u in arr if str(u).strip())
                    if isinstance(data.get("group_reply"), str):
                        self.group_reply = data["group_reply"]
                    bl = data.get("blacklist_groups", [])
                    if isinstance(bl, list):
                        self.blacklist_groups = {str(g).strip() for g in bl if str(g).strip()}
                    if data.get("require_at_mention") is not None:
                        self.require_at_mention = bool(data["require_at_mention"])
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[WPP:{self.account_id}] 读取配置文件失败: {e}")

    def _save_whitelist_file(self) -> None:
        try:
            Path(self._whitelist_file).write_text(
                json.dumps({
                    "allow_users": sorted(self.allow_users),
                    "group_reply": self.group_reply,
                    "blacklist_groups": sorted(self.blacklist_groups),
                    "require_at_mention": self.require_at_mention,
                }, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self._whitelist_mtime = self._get_file_mtime()
            logger.info(f"[WPP:{self.account_id}] 运行时配置已持久化 → {self._whitelist_file}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[WPP:{self.account_id}] 写配置文件失败: {e}")

    def _get_file_mtime(self) -> float:
        try:
            p = Path(self._whitelist_file)
            return p.stat().st_mtime if p.exists() else 0.0
        except Exception:  # noqa: BLE001
            return 0.0

    @staticmethod
    def _split_args(args: list[str]) -> list[str]:
        out: list[str] = []
        for a in args:
            for piece in a.replace("，", ",").split(","):
                piece = piece.strip()
                if piece:
                    out.append(piece)
        return out

    def _reload_config_loop(self) -> None:
        """后台循环: 每 3 秒检查配置文件 mtime, 外部修改 → 自动重载。"""
        async def _loop() -> None:
            while True:
                try:
                    await asyncio.sleep(3)
                    mtime = self._get_file_mtime()
                    if mtime > 0 and mtime != self._whitelist_mtime:
                        self._whitelist_mtime = mtime
                        old_users = set(self.allow_users)
                        old_groups = set(self.blacklist_groups)
                        old_reply = self.group_reply
                        old_at = self.require_at_mention
                        self._load_whitelist_file()
                        if (self.allow_users != old_users or self.blacklist_groups != old_groups
                                or self.group_reply != old_reply or self.require_at_mention != old_at):
                            logger.info(f"[WPP:{self.account_id}] 配置文件外部修改已热更新")
                except asyncio.CancelledError:
                    break
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[WPP:{self.account_id}] 配置文件热更新检查失败: {e}")

        self._reload_task = asyncio.create_task(_loop())

    # ------------------------------------------------------------------ filehelper 命令
    async def _handle_filehelper_command(self, content: str, from_wxid: str, recipient_id: str) -> None:
        parts = content.strip().split()
        cmd = (parts[0] if parts else "").lower()
        args = parts[1:]

        async def reply(text: str) -> None:
            try:
                await self._api.send_text("filehelper", text)
                logger.info(f"[WPP:{self.account_id} FILEHELPER] → {text[:60]}")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[WPP:{self.account_id} FILEHELPER] 回发失败: {e}")

        for usage, _desc, handler in self._filehelper_commands:
            cmd_name = usage.split()[0].lower()
            if cmd == cmd_name:
                try:
                    return await handler(args, reply)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[WPP:{self.account_id} FILEHELPER] 命令 {cmd} 执行失败: {e}")
                    await reply(f"命令 {cmd} 执行失败: {e}")
                    return
        await reply(f"未知命令: {cmd}\n用 /help 查看全部命令。")

    async def _handle_user_command(self, args: list[str], reply) -> None:
        action = (args[0] if args else "").strip().lower()
        targets = self._split_args(args[1:])
        if action == "list" or (action == "" and not targets):
            users = [u for u in sorted(self.allow_users) if not u.endswith("@chatroom")]
            await reply(f"📋 私聊白名单 ({len(users)}):\n" + ("\n".join(f"- {u}" for u in users) if users else "(空)"))
            return
        if action == "add" and targets:
            added = [t for t in targets if t not in self.allow_users]
            for t in targets:
                self.allow_users.add(t)
            self._save_whitelist_file()
            await reply(f"✅ 已授权私聊白名单: {', '.join(added) or '(均已存在)'}\n当前 ({len(self.allow_users)}): {', '.join(sorted(self.allow_users))}")
            return
        if action == "del" and targets:
            removed = [t for t in targets if t in self.allow_users]
            missing = [t for t in targets if t not in self.allow_users]
            for t in removed:
                self.allow_users.discard(t)
            self._save_whitelist_file()
            remaining = ", ".join(sorted(self.allow_users))
            msg = f"✅ 已移除私聊白名单: {', '.join(removed) or '(无)'}"
            if missing:
                msg += f"\n⚠️ 不在白名单: {', '.join(missing)}"
            msg += f"\n当前 ({len(self.allow_users)}): {remaining or '(空)'}"
            await reply(msg)
            return
        await reply("用法: /user add <wxid> [wxid...] | /user del <wxid> [wxid...] | /user list")

    async def _handle_group_command(self, args: list[str], reply) -> None:
        action = (args[0] if args else "").strip().lower()
        targets = self._split_args(args[1:])
        if action == "list" or (action == "" and not targets):
            groups = [u for u in sorted(self.allow_users) if u.endswith("@chatroom")]
            await reply(f"📋 群白名单 ({len(groups)}):\n" + ("\n".join(f"- {u}" for u in groups) if groups else "(空)"))
            return
        if action == "add" and targets:
            added = [t for t in targets if t not in self.allow_users]
            for t in targets:
                self.allow_users.add(t)
                if t in self.blacklist_groups:
                    self.blacklist_groups.discard(t)
            self._save_whitelist_file()
            groups = [u for u in sorted(self.allow_users) if u.endswith("@chatroom")]
            await reply(f"✅ 已授权群白名单: {', '.join(added) or '(均已存在)'}\n当前群 ({len(groups)}): {', '.join(groups) or '(空)'}")
            return
        if action == "del" and targets:
            removed = [t for t in targets if t in self.allow_users]
            missing = [t for t in targets if t not in self.allow_users]
            for t in removed:
                self.allow_users.discard(t)
            self._save_whitelist_file()
            groups = [u for u in sorted(self.allow_users) if u.endswith("@chatroom")]
            msg = f"✅ 已移除群白名单: {', '.join(removed) or '(无)'}"
            if missing:
                msg += f"\n⚠️ 不在白名单: {', '.join(missing)}"
            msg += f"\n当前群 ({len(groups)}): {', '.join(groups) or '(空)'}"
            await reply(msg)
            return
        await reply("用法: /group add <群ID> [群ID...] | /group del <群ID> [群ID...] | /group list")

    async def _handle_blacklist_command(self, args: list[str], reply) -> None:
        action = (args[0] if args else "").strip().lower()
        targets = self._split_args(args[1:])
        if action == "add" and targets:
            added = []
            removed_from_wl = []
            for t in targets:
                self.blacklist_groups.add(t)
                added.append(t)
                if t in self.allow_users:
                    self.allow_users.discard(t)
                    removed_from_wl.append(t)
            self._save_whitelist_file()
            note = f"\n(已自动从白名单移除: {', '.join(removed_from_wl)})" if removed_from_wl else ""
            await reply(f"✅ 已加入黑名单群: {', '.join(added)}{note}\n当前黑名单 ({len(self.blacklist_groups)}): {', '.join(sorted(self.blacklist_groups)) or '(空)'}")
        elif action == "del" and targets:
            removed = [t for t in targets if t in self.blacklist_groups]
            missing = [t for t in targets if t not in self.blacklist_groups]
            for t in removed:
                self.blacklist_groups.discard(t)
            self._save_whitelist_file()
            msg = f"✅ 已移出黑名单群: {', '.join(removed) or '(无)'}"
            if missing:
                msg += f"\n⚠️ 不在黑名单: {', '.join(missing)}"
            msg += f"\n当前黑名单 ({len(self.blacklist_groups)}): {', '.join(sorted(self.blacklist_groups)) or '(空)'}"
            await reply(msg)
        elif action == "list":
            await reply(f"当前黑名单群 ({len(self.blacklist_groups)}):\n" + ("\n".join(f"- {g}" for g in sorted(self.blacklist_groups)) if self.blacklist_groups else "(空)"))
        else:
            await reply("用法: /blacklist add <群ID> [群ID...] | del <群ID> [群ID...] | list")

    async def _handle_mode_command(self, args: list[str], reply) -> None:
        mode = (args[0] if args else "").strip().lower()
        if mode not in ("atbot", "none", "all"):
            await reply("用法: /mode atbot|none|all\natbot=只回@机器人的群消息 / none=忽略群消息 / all=群消息都回\n当前: " + self.group_reply)
            return
        self.group_reply = mode
        self._save_whitelist_file()
        await reply(f"✅ 群消息回复模式已设为: {mode}\n(atbot=只回@ / none=忽略群 / all=都回)")

    async def _handle_at_command(self, args: list[str], reply) -> None:
        mode = (args[0] if args else "").strip().lower()
        if mode in ("on", "1", "true"):
            self.require_at_mention = True
            self._save_whitelist_file()
            await reply("✅ 群聊已设为必须 @ 才回复")
        elif mode in ("off", "0", "false"):
            self.require_at_mention = False
            self._save_whitelist_file()
            await reply("✅ 群聊已解除必须 @ (白名单群内非@也可触发)")
        else:
            await reply(f"用法: /at on|off\n当前: {'on (必须@)' if self.require_at_mention else 'off (不必@)'}")

    async def _handle_help_command(self, args: list[str], reply) -> None:
        lines = [f"【WPP 命令 (账号 {self.account_id})】"]
        for usage, desc, _handler in self._filehelper_commands:
            lines.append(f"{usage:<30}  {desc}")
        await reply("\n".join(lines))

    # ------------------------------------------------------------------ 白名单/群策略判断
    def _is_allowed(self, from_wxid: str, is_group: bool, chatroom_id: str, content: str, recipient_id: str = "") -> bool:
        if is_group:
            if chatroom_id and chatroom_id in self.blacklist_groups:
                return False
            if self.allow_users:
                group_whitelisted = chatroom_id in self.allow_users
                has_group_in_whitelist = any(u.endswith("@chatroom") for u in self.allow_users)
                if has_group_in_whitelist and not group_whitelisted:
                    return False
            if self.group_reply == "none":
                return False
            if self.require_at_mention and "@" not in content:
                return False
            if self.group_reply == "all":
                return True
            self_wxid = _safe_str(recipient_id) or _safe_str(self._self_wxid)
            self_nickname = _safe_str(self._self_nickname)
            if recipient_id and recipient_id == _safe_str(self._self_wxid) and "@" in content:
                return True
            if self_wxid or self_nickname:
                if self_wxid and (self_wxid in content or f"@{self_wxid}" in content):
                    return True
                if self_wxid and self_wxid + "@" in content:
                    return True
                if self_nickname and (
                    f"@{self_nickname}" in content or self_nickname in content
                ):
                    return True
                return False
            return False
        if not self.allow_users:
            return True
        return from_wxid in self.allow_users

    # ------------------------------------------------------------------ 转换
    def _to_astrbot_message(self, payload: dict) -> AstrBotMessage | None:
        src = self._extract_msg_src(payload)
        if src is None:
            return None
        return self._to_astrbot_message_src(src)

    def _maybe_inject_at_component(self, src: dict, content: str, msg_type: int, abm: AstrBotMessage) -> None:
        if abm.type != MessageType.GROUP_MESSAGE:
            return
        if not content or "@" not in content:
            return
        if msg_type not in (MSG_TYPE_TEXT, MSG_TYPE_APP, MSG_TYPE_RELAY):
            return

        from astrbot.api.message_components import At

        recipient_id = _safe_str(src.get("recipient_id") or src.get("toWxid") or src.get("ToWxid"))
        self_id = abm.self_id or _safe_str(self._self_wxid)
        if not self_id:
            return
        mentioned = (
            f"@{self_id}" in content
            or self_id + "@" in content
            or (self._self_nickname and f"@{self._self_nickname}" in content)
            or (self._self_nickname and self._self_nickname + "@" in content)
        )
        if not mentioned:
            return
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
        v1_is_group = src.get("is_group")
        if not chatroom_id and (v1_is_group is True or v1_is_group == "true" or v1_is_group == 1):
            chatroom_id = _safe_str(src.get("conversation_id"))
        direction = _safe_str(src.get("direction"))
        if direction and direction not in ("incoming", "1"):
            return None
        if from_wxid.startswith("gh_"):
            return None

        msg_type = _safe_num(src.get("msgType") or src.get("MsgType") or src.get("type"), MSG_TYPE_TEXT)
        content = _safe_str(src.get("content") or src.get("Content") or src.get("text"))
        msg_id = _safe_str(src.get("msgId") or src.get("MsgId") or src.get("svr_id") or src.get("id"))
        ts = _safe_num(src.get("createTime") or src.get("CreateTime") or src.get("created_at"), 0)
        if ts > 1e12:
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

        if msg_type == MSG_TYPE_TEXT:
            abm.message_str = content
            abm.message = [Plain(content)]
        elif msg_type == MSG_TYPE_FILE or (src.get("kind") == "app" and isinstance(src.get("app"), dict) and src.get("app", {}).get("category") == "file"):
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
        elif msg_type == MSG_TYPE_VOICE:
            voice_obj = src.get("voice") if isinstance(src.get("voice"), dict) else {}
            transcript = _safe_str(voice_obj.get("transcript")) or _safe_str(voice_obj.get("text"))
            if transcript:
                abm.message_str = transcript
                abm.message = [Plain(transcript)]
            else:
                abm.message_str = "[语音]"
                abm.message = []
        elif msg_type == MSG_TYPE_IMAGE:
            abm.message_str = content or "[图片]"
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
            abm.message = []
            abm._wpp_image_meta = {
                "msg_id": local_id,
                "to_wxid": to_wxid or (chatroom_id or ""),
                "data_len": data_len,
                "cdn": cdn_ctx,
            }
            abm.message_str = "[图片]"
        elif msg_type == MSG_TYPE_VIDEO or src.get("kind") == "video":
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
        elif msg_type == MSG_TYPE_APP and content:
            abm.message_str = content
            abm.message = [Plain(content)]
        else:
            abm.message_str = content
            abm.message = []

        self._maybe_inject_at_component(src, content, msg_type, abm)
        return abm

    @staticmethod
    def _extract_msg_src(payload: dict) -> dict | None:
        if not isinstance(payload, dict):
            return None
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
        if any(k in payload for k in ("fromUser", "fromWxid", "MsgId", "msgId")):
            return payload
        if isinstance(data, dict):
            if any(k in data for k in ("fromUser", "fromWxid", "MsgId", "Content", "Text")):
                return data
        return None

    @staticmethod
    def _extract_all_msg_src(payload: dict) -> list[dict]:
        if not isinstance(payload, dict):
            return []
        out: list[dict] = []
        data = payload.get("Data")
        # WS 形态 (Data.data.messages)
        if isinstance(data, dict):
            data_inner = data.get("data")
            if isinstance(data_inner, dict):
                for kk in ("messages", "items"):
                    arr = data_inner.get(kk)
                    if isinstance(arr, list):
                        for item in arr:
                            if isinstance(item, dict):
                                out.append(item)
        # 形态 A: v1 messages / items
        if isinstance(data, dict):
            for kk in ("messages", "items"):
                arr = data.get(kk)
                if isinstance(arr, list):
                    for item in arr:
                        if isinstance(item, dict):
                            out.append(item)
        # 形态 B: AddMsgs
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
        if not out:
            single = WppAccount._extract_msg_src(payload)
            if single:
                out.append(single)
        return out
