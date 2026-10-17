"""WPP 消息解析与转换 (纯函数, 不含网络/文件 IO, 便于单测)。

职责:
  - 解析 vendor WS / webhook 推送的 payload → 单条消息 src 列表
  - 消息 src → AstrBotMessage (AstrBot 标准消息对象)
  - 消息 ID / 发送方向 / 消息类型等小工具

设计原则 (v0.3.0):
  - **只把真正的消息事件交给上层**: vendor WS 还会推送朋友圈更新 (friend_circle_update)、
    联系人更新等事件, 它们同样带 `Data.data.items`, 旧实现会误把 items 当消息处理,
    导致大量 "from= group=-" 的白名单告警。这里统一按 `Data.type` 白名单过滤。
  - 纯函数: 不依赖 WppAccount 实例, 方便在容器内直接 pytest。
"""

from __future__ import annotations

import time
from typing import Any

from astrbot.api.message_components import At, File, Plain, Reply
from astrbot.api.platform import AstrBotMessage, Group, MessageMember, MessageType

# WPP vendor 消息类型 (WeChat MsgType)
MSG_TYPE_TEXT = 1
MSG_TYPE_IMAGE = 3
MSG_TYPE_FILE = 6
MSG_TYPE_VOICE = 34
MSG_TYPE_VIDEO = 43
MSG_TYPE_EMOJI = 47
MSG_TYPE_APP = 49
MSG_TYPE_RELAY = 51
MSG_TYPE_REVOKE = 10002

# vendor WS 事件类型白名单 (只处理消息事件)。
# 其它类型 (friend_circle_update / contact_update / ...) 一律忽略。
MESSAGE_EVENT_TYPES = frozenset(
    {"sync_message", "message", "new_message", "add_message", "sync_msgs"}
)


def safe_str(v: Any) -> str:
    return "" if v is None else str(v)


def safe_num(v: Any, fallback: int = 1) -> int:
    try:
        if isinstance(v, bool):
            return fallback
        return int(float(v))
    except (TypeError, ValueError):
        return fallback


def split_users(s: str) -> set[str]:
    """逗号分隔的 wxid/群 ID 列表 → set。"""
    return {u.strip() for u in s.split(",") if u.strip()}


def split_args(args: list[str]) -> list[str]:
    """filehelper 命令参数: 支持空格/中英文逗号分隔。"""
    out: list[str] = []
    for a in args:
        for piece in a.replace("，", ",").split(","):
            piece = piece.strip()
            if piece:
                out.append(piece)
    return out


# --------------------------------------------------------------------------- 事件解析
def ws_event_type(payload: dict) -> str:
    """取 vendor WS 事件类型 (`Data.type`); 无则返回空串。"""
    data = payload.get("Data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return ""
    return safe_str(data.get("type"))


def is_message_payload(payload: dict) -> bool:
    """判断 payload 是否为消息事件 (过滤朋友圈/联系人等非消息推送)。

    兼容三类形态:
      1. WS v2: `{Data:{type:"sync_message", data:{messages:[...]}}}` — 按 type 过滤
      2. 旧 webhook: `{EventType:"sync_message", Data:{Data:{AddMsgs:[...]}}}`
      3. 扁平回调: 顶层/Data 直接就是一条消息 (`fromUser`/`sender_id` 等)
    """
    if not isinstance(payload, dict):
        return False

    etype = ws_event_type(payload)
    if etype:
        return etype in MESSAGE_EVENT_TYPES

    if safe_str(payload.get("EventType")) in MESSAGE_EVENT_TYPES:
        return True
    if any(k in payload for k in ("fromUser", "fromWxid", "MsgId", "msgId", "sender_id")):
        return True

    data = payload.get("Data")
    if isinstance(data, dict):
        if any(k in data for k in ("fromUser", "fromWxid", "MsgId", "msgId", "sender_id")):
            return True
        inner = data.get("data")
        if isinstance(inner, dict) and isinstance(inner.get("messages"), list):
            return True
        if isinstance(data.get("messages"), list):
            return True
    return False


def extract_msg_src(payload: dict) -> dict | None:
    """从 payload 中取第一条消息 src (单条场景)。"""
    msgs = extract_all_msg_src(payload)
    return msgs[0] if msgs else None


def extract_all_msg_src(payload: dict) -> list[dict]:
    """从 vendor 推送中取出所有消息 src。

    支持形态:
      - WS v2:  `Data.data.messages` (schema wechatpad.message.v2)
      - WS v2:  `Data.data.items` (仅当 type 是消息事件时)
      - 旧接口: `Data.messages` / `Data.items`
      - 旧业务回调: `Data.Data.AddMsgs`
      - 扁平单条: 顶层或 Data 本身就是一条消息
    """
    if not isinstance(payload, dict) or not is_message_payload(payload):
        return []

    out: list[dict] = []

    def _append(arr: Any) -> None:
        if isinstance(arr, list):
            for item in arr:
                if isinstance(item, dict):
                    out.append(item)

    data = payload.get("Data")
    if isinstance(data, dict):
        inner = data.get("data")
        if isinstance(inner, dict):
            _append(inner.get("messages"))
            if not out:
                _append(inner.get("items"))
        if not out:
            _append(data.get("messages"))
            _append(data.get("items"))
        if not out:
            inner2 = data.get("Data")
            if isinstance(inner2, dict):
                _append(inner2.get("AddMsgs"))

    if not out:
        single = _extract_flat_src(payload)
        if single:
            out.append(single)
    return out


def _extract_flat_src(payload: dict) -> dict | None:
    if any(k in payload for k in ("fromUser", "fromWxid", "MsgId", "msgId", "sender_id")):
        return payload
    data = payload.get("Data")
    if isinstance(data, dict) and any(
        k in data for k in ("fromUser", "fromWxid", "MsgId", "sender_id")
    ):
        return data
    return None


# --------------------------------------------------------------------------- 单条工具
def src_sender(src: dict) -> str:
    return safe_str(
        src.get("fromUser")
        or src.get("fromWxid")
        or src.get("fromUserName")
        or src.get("FromWxid")
        or src.get("FromUser")
        or src.get("FromUserName")
        or src.get("sender_id")
    )


def src_recipient(src: dict) -> str:
    return safe_str(
        src.get("toWxid")
        or src.get("toUserName")
        or src.get("ToWxid")
        or src.get("ToUserName")
        or src.get("recipient_id")
    )


def src_chatroom(src: dict) -> str:
    """群 ID。兼容 v1 (is_group + *_id 字段) 与旧 webhook (chatroomId/roomId)。"""
    chatroom = safe_str(
        src.get("chatroomId") or src.get("ChatroomId") or src.get("roomId") or src.get("RoomId")
    )
    if chatroom:
        return chatroom
    # vendor v1: conversation_id / recipient_id / sender_id 中任一以 @chatroom 结尾
    for key in ("conversation_id", "chatroom_id", "recipient_id", "sender_id"):
        val = safe_str(src.get(key))
        if val.endswith("@chatroom"):
            return val
    is_group = src.get("is_group")
    if is_group is True or is_group == "true" or is_group == 1:
        conv = safe_str(src.get("conversation_id"))
        if conv:
            return conv
    return ""


def src_content(src: dict) -> str:
    return safe_str(
        src.get("content") or src.get("Content") or src.get("text") or src.get("msg")
    )


def src_msg_key(src: dict) -> str:
    """消息去重 key (优先服务端稳定 ID)。"""
    return safe_str(
        src.get("msgId")
        or src.get("MsgId")
        or src.get("msg_id")
        or src.get("new_msg_id")
        or src.get("newMsgId")
        or src.get("NewMsgId")
        or src.get("svr_id")
        or src.get("id")
    )


def src_is_incoming(src: dict) -> bool:
    direction = safe_str(src.get("direction"))
    return not direction or direction in ("incoming", "1")


def src_is_filehelper(src: dict) -> bool:
    return (
        src_recipient(src) == "filehelper"
        or safe_str(src.get("conversation_id")) == "filehelper"
        or src_sender(src) == "filehelper"
    )


def mentioned(content: str, self_wxid: str, self_nickname: str) -> bool:
    """判断群消息是否 @ 了机器人。

    兼容微信实际推送的几种形态:
      - `@nickname\\u2005你好` / `@wxid 你好`
      - 旧版 `wxid@chatroom` 形式
      - 仅昵称出现 (部分 vendor 会去掉 @ 前缀)
    """
    if not content:
        return False
    ids = [x for x in (self_wxid, self_nickname) if x]
    for token in ids:
        if f"@{token}" in content:
            return True
        if token + "@" in content:
            return True
        if self_wxid and token == self_wxid and token in content:
            return True
    return False


def build_reply_component(src: dict) -> Reply | None:
    """把引用回复上下文转成 AstrBot Reply 组件 (让模型读到被引用内容)。"""
    ctx = src.get("reply_context")
    if not isinstance(ctx, dict):
        # 旧版 go_fields 里的转义结构也尝试一下
        go_fields = src.get("go_fields")
        if not isinstance(go_fields, dict):
            return None
        ctx = go_fields.get("ReplyContext") if isinstance(go_fields.get("ReplyContext"), dict) else None
    if not isinstance(ctx, dict):
        return None

    quote = safe_str(ctx.get("quote_content") or ctx.get("content"))
    if not quote:
        return None
    msg_id = safe_str(ctx.get("new_msg_id") or ctx.get("svr_id") or ctx.get("msg_id"))
    sender = safe_str(ctx.get("from_user_id"))
    try:
        # AstrBot Reply.qq 是 int 字段 (wxid 非数字时置 0)
        qq = int(sender) if sender.isdigit() else 0
    except (TypeError, ValueError):
        qq = 0
    return Reply(
        id=msg_id,
        sender_id=sender,
        sender_nickname=safe_str(ctx.get("display_name") or sender),
        message_str=quote,
        text=quote,
        qq=qq,
        time=safe_num(ctx.get("create_time"), 0),
    )


# --------------------------------------------------------------------------- 转换
def to_astrbot_message(
    src: dict,
    self_wxid: str = "",
    self_nickname: str = "",
) -> AstrBotMessage | None:
    """单条 vendor 消息 src → AstrBotMessage。

    返回 None 表示该消息不应进入 AstrBot (撤回/系统消息/空内容/非 incoming)。
    """
    from_wxid = src_sender(src)
    if not from_wxid:
        return None
    if from_wxid.startswith("gh_"):
        return None
    if not src_is_incoming(src):
        return None
    if self_wxid and from_wxid == self_wxid:
        return None

    msg_type = safe_num(
        src.get("msgType") or src.get("MsgType") or src.get("type") or src.get("msg_type"),
        MSG_TYPE_TEXT,
    )
    if msg_type == MSG_TYPE_REVOKE:
        return None

    chatroom_id = src_chatroom(src)
    is_group = bool(chatroom_id)
    content = src_content(src)
    to_wxid = src_recipient(src)
    msg_id = src_msg_key(src)
    ts = safe_num(
        src.get("createTime")
        or src.get("CreateTime")
        or src.get("created_at")
        or src.get("ts")
        or src.get("Timestamp"),
        0,
    )
    if ts > 1e12:
        ts //= 1000
    if ts <= 0:
        ts = int(time.time())

    abm = AstrBotMessage()
    abm.type = MessageType.GROUP_MESSAGE if is_group else MessageType.FRIEND_MESSAGE
    abm.self_id = to_wxid or self_wxid
    abm.message_id = msg_id or f"wpp-{ts}"
    abm.sender = MessageMember(
        user_id=from_wxid,
        nickname=safe_str(
            src.get("fromNick")
            or src.get("fromNickname")
            or src.get("FromNick")
            or src.get("sender_name")
        )
        or from_wxid,
    )
    abm.raw_message = src
    abm.timestamp = ts
    if is_group:
        abm.group = Group(group_id=chatroom_id)
        abm.session_id = chatroom_id
    else:
        abm.session_id = from_wxid

    if msg_type == MSG_TYPE_TEXT:
        if not content:
            return None
        abm.message_str = content
        abm.message = [Plain(content)]

    elif msg_type == MSG_TYPE_FILE or _is_file_app(src):
        file_obj = src.get("file") if isinstance(src.get("file"), dict) else {}
        app_obj = src.get("app") if isinstance(src.get("app"), dict) else {}
        filename = (
            safe_str(app_obj.get("title"))
            or safe_str(file_obj.get("name"))
            or safe_str(file_obj.get("filename"))
            or content
            or "file"
        )
        dc = file_obj.get("download_context") if isinstance(file_obj.get("download_context"), dict) else {}
        abm.message_str = f"[文件] {filename}"
        abm.message = [Plain(abm.message_str)]
        abm._wpp_file_meta = {
            "filename": filename,
            "attach_id": safe_str(dc.get("attach_id")),
            "user_name": safe_str(dc.get("user_name")),
            "data_len": safe_num(dc.get("data_len"), 0),
        }

    elif msg_type == MSG_TYPE_VOICE:
        voice_obj = src.get("voice") if isinstance(src.get("voice"), dict) else {}
        transcript = safe_str(voice_obj.get("transcript")) or safe_str(voice_obj.get("text"))
        if transcript:
            abm.message_str = transcript
            abm.message = [Plain(transcript)]
        else:
            # 没有转写也保留一条占位文本, 避免提交空消息链给 AI
            abm.message_str = "[语音]"
            abm.message = [Plain("[语音]")]

    elif msg_type == MSG_TYPE_IMAGE:
        img_obj = src.get("image") if isinstance(src.get("image"), dict) else {}
        local_id = safe_str(src.get("local_id")) or msg_id
        data_len = safe_num(img_obj.get("data_len"), 0)
        cdn_ctx = _pick_cdn_context(img_obj.get("cdn_download_contexts"))
        abm.message_str = "[图片]"
        abm.message = []
        # DownloadImg 的 to_wxid: 私聊=机器人自己, 群聊=群 ID (与 wpp-openclaw 一致)
        abm._wpp_image_meta = {
            "msg_id": local_id,
            "to_wxid": chatroom_id if is_group else (to_wxid or self_wxid),
            "data_len": data_len,
            "cdn": cdn_ctx,
        }

    elif msg_type == MSG_TYPE_VIDEO or src.get("kind") == "video":
        video_obj = src.get("video") if isinstance(src.get("video"), dict) else {}
        vc = video_obj.get("download_context") if isinstance(video_obj.get("download_context"), dict) else {}
        abm.message_str = "[视频]"
        abm.message = [Plain("[视频]")]
        abm._wpp_video_meta = {
            "msg_id": safe_str(vc.get("msg_id")),
            "data_len": safe_num(vc.get("data_len"), 0),
            "to_wxid": safe_str(vc.get("to_wxid")) or (chatroom_id if is_group else to_wxid),
            "compress_type": safe_num(vc.get("compress_type"), 0),
        }

    elif msg_type in (MSG_TYPE_APP, MSG_TYPE_RELAY):
        if not content:
            return None
        abm.message_str = content
        abm.message = [Plain(content)]

    elif msg_type == MSG_TYPE_EMOJI:
        abm.message_str = content or "[表情]"
        abm.message = [Plain(abm.message_str)]

    else:
        if not content:
            return None
        abm.message_str = content
        abm.message = [Plain(content)]

    _inject_at_component(src, abm, content, msg_type, self_wxid, self_nickname)
    _inject_reply_component(src, abm)
    return abm


def _is_file_app(src: dict) -> bool:
    app = src.get("app")
    return (
        src.get("kind") == "app"
        and isinstance(app, dict)
        and app.get("category") == "file"
    )


def _pick_cdn_context(cdn_list: Any) -> dict | None:
    if not isinstance(cdn_list, list):
        return None
    for item in cdn_list:
        if isinstance(item, dict) and item.get("file_aes_key") and item.get("file_no"):
            return {
                "file_aes_key": str(item["file_aes_key"]),
                "file_no": str(item["file_no"]),
            }
    return None


def _inject_at_component(
    src: dict,
    abm: AstrBotMessage,
    content: str,
    msg_type: int,
    self_wxid: str,
    self_nickname: str,
) -> None:
    if abm.type != MessageType.GROUP_MESSAGE:
        return
    if not content or "@" not in content:
        return
    if msg_type not in (MSG_TYPE_TEXT, MSG_TYPE_APP, MSG_TYPE_RELAY):
        return
    self_id = self_wxid or abm.self_id
    if not self_id or not mentioned(content, self_id, self_nickname):
        return
    abm.message.insert(0, At(qq=self_id))


def _inject_reply_component(src: dict, abm: AstrBotMessage) -> None:
    reply = build_reply_component(src)
    if reply:
        abm.message.insert(0, reply)


__all__ = [
    "MSG_TYPE_TEXT",
    "MSG_TYPE_IMAGE",
    "MSG_TYPE_FILE",
    "MSG_TYPE_VOICE",
    "MSG_TYPE_VIDEO",
    "MSG_TYPE_EMOJI",
    "MSG_TYPE_APP",
    "MSG_TYPE_RELAY",
    "MSG_TYPE_REVOKE",
    "MESSAGE_EVENT_TYPES",
    "safe_str",
    "safe_num",
    "split_users",
    "split_args",
    "ws_event_type",
    "is_message_payload",
    "extract_msg_src",
    "extract_all_msg_src",
    "src_sender",
    "src_recipient",
    "src_chatroom",
    "src_content",
    "src_msg_key",
    "src_is_incoming",
    "src_is_filehelper",
    "mentioned",
    "build_reply_component",
    "to_astrbot_message",
]
