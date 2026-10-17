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

import re
import time
from typing import Any

from astrbot.api.message_components import At, File, Plain, Reply
from astrbot.api.platform import AstrBotMessage, Group, MessageMember, MessageType

from . import wpp_appmsg as appmsg

# WPP vendor 消息类型 (WeChat MsgType)
MSG_TYPE_TEXT = 1
MSG_TYPE_IMAGE = 3
MSG_TYPE_FILE = 6
MSG_TYPE_VOICE = 34
MSG_TYPE_CONTACT = 42
MSG_TYPE_VIDEO = 43
MSG_TYPE_EMOJI = 47
MSG_TYPE_LOCATION = 48
MSG_TYPE_APP = 49
MSG_TYPE_STATUS = 51
MSG_TYPE_SYSTEM = 10000
MSG_TYPE_REVOKE = 10002

# 旧版/部分 vendor 用字符串表示消息类型
_MSG_TYPE_NAMES = {
    "text": MSG_TYPE_TEXT,
    "image": MSG_TYPE_IMAGE,
    "file": MSG_TYPE_FILE,
    "voice": MSG_TYPE_VOICE,
    "audio": MSG_TYPE_VOICE,
    "contact": MSG_TYPE_CONTACT,
    "card": MSG_TYPE_CONTACT,
    "video": MSG_TYPE_VIDEO,
    "emoji": MSG_TYPE_EMOJI,
    "sticker": MSG_TYPE_EMOJI,
    "location": MSG_TYPE_LOCATION,
    "app": MSG_TYPE_APP,
    "link": MSG_TYPE_APP,
    "miniprogram": MSG_TYPE_APP,
    "mini_program": MSG_TYPE_APP,
    "system": MSG_TYPE_SYSTEM,
    "revoke": MSG_TYPE_REVOKE,
    "status": MSG_TYPE_STATUS,
}

# 微信内置的系统/服务会话 (不是真人好友)。这些会话的消息**任何模式下都不应触发 AI**。
# 已用 vendor /Friend/GetContractDetail 实测确认:
#   weixin=微信团队, notifymessage=服务通知, filehelper=文件传输助手,
#   weixinsrc=微信安全中心, tenpay=财付通, wechatpay=WeChat Payment,
#   wxpayhelper=智慧商户服务助手, weixinpay=已注销支付号
# wxpay=微信支付 / brandsessionholder=订阅号消息: 微信内部服务号, 不返回通讯录详情,
# 但社区与协议侧长期使用这两个固定 username, 一并拦截。
SYSTEM_SENDERS = frozenset({
    "weixin",            # 微信团队
    "wxpay",             # 微信支付
    "weixinpay",         # 微信支付 (旧/已注销)
    "tenpay",            # 财付通
    "wechatpay",         # WeChat Payment
    "wxpayhelper",       # 智慧商户服务助手
    "notifymessage",     # 服务通知
    "weixinsrc",         # 微信安全中心
    "filehelper",        # 文件传输助手 (命令已在前面处理; 非命令不进 AI)
    "brandsessionholder",  # 订阅号消息
    "qqmail",            # QQ邮箱提醒
    "weixinreminder",    # 语音提醒
    "fmessage",          # 好友请求 / 系统好友
    "mphelper",          # 公众平台助手
    "newsapp",           # 腾讯新闻 / 系统资讯
    "medianote",         # 语音记事本
    "floatbottle",       # 漂流瓶
    "weixinlianbo",      # 微信连播
})

# 微信原生支付类 appmsg（转账/红包）。只在**卡片**(msgType=49)上采信，
# 绝不按纯文本里的"红包"二字判断 —— 群接龙文案常自带"红包"（历史事故）。
PAYMENT_APPMSG_TYPES = frozenset({2000, 2001, 2002})
_PAYMENT_CATEGORY_RE = re.compile(r"payment|transfer|hongbao|redpacket|pay", re.I)
# 只认“整句就是支付文案”的描述，避免把“红包活动/领红包”这类链接卡片误杀
_PAYMENT_DESC_RE = re.compile(
    r"^(?:微信)?(?:红包|转账|收款)(?:[\d.]+\s*元)?$"
    r"|^收到(?:转账|红包)"
    r"|^\[(?:红包|转账)\]$",
    re.I,
)

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
    # vendor v1: conversation_id / chatroom_id / recipient_id / peer_id 中任一以 @chatroom 结尾。
    # 不要用 sender_id: 极少数推送会把群 ID 放在 sender_id，若据此建群会把发送者误标成群。
    for key in ("conversation_id", "chatroom_id", "recipient_id", "peer_id"):
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
    direction = safe_str(src.get("direction")).lower()
    if not direction:
        return True
    return direction in ("incoming", "in", "recv", "received", "1", "true")


def src_is_filehelper(src: dict) -> bool:
    return (
        src_recipient(src) == "filehelper"
        or safe_str(src.get("conversation_id")) == "filehelper"
        or src_sender(src) == "filehelper"
    )


def src_msg_type(src: dict) -> int:
    """vendor 消息类型 (v1 用 `type`, 旧 webhook 用 `msgType`)。

    兼容字符串类型 (`"image"` / `"voice"` 等) —— 部分 vendor 变体会这样推。
    """
    raw = (
        src.get("msgType")
        or src.get("MsgType")
        or src.get("type")
        or src.get("msg_type")
    )
    if raw is None or safe_str(raw) == "":
        # 部分 vendor 变体只给 `kind` ("app"/"image"/"voice"...), 没有数字类型
        raw = src.get("kind")
    if isinstance(raw, str):
        name = raw.strip().lower()
        if name in _MSG_TYPE_NAMES:
            return _MSG_TYPE_NAMES[name]
    return safe_num(raw, MSG_TYPE_TEXT)


def _src_identity_ids(src: dict) -> list[str]:
    """消息里所有可能承载会话 username 的字段 (sender/recipient/conversation)。"""
    ids = [src_sender(src), src_recipient(src)]
    for key in ("conversation_id", "chatroom_id", "chatroomId", "roomId", "peer_id"):
        val = safe_str(src.get(key))
        if val:
            ids.append(val)
    return ids


def system_sender(src: dict) -> str:
    """返回消息所属的微信系统/服务会话 id (非真人), 没有则返回空串。

    公众号 (`gh_`) 与微信内置服务号 (微信团队/微信支付/服务通知/财付通/...)
    都不是可对话的联系人。**不依赖 allow_users 模式** —— 即使"全部放行"也必须静默。
    """
    for ident in _src_identity_ids(src):
        low = ident.strip().lower()
        if not low:
            continue
        if low.startswith("gh_"):
            return low
        if low in SYSTEM_SENDERS:
            return low
    return ""


def is_payment_card(src: dict) -> bool:
    """红包/转账/支付卡片 —— 静默，不触发 AI。

    **只认卡片结构** (msgType=49 + app 元数据/XML)，不按正文"红包"关键词判断:
    群接龙文案经常自带"红包"二字，按关键词会把正常接龙静默掉（wpp-hermes 09-27 事故）。
    """
    if src_msg_type(src) != MSG_TYPE_APP:
        return False

    info = appmsg.parse_appmsg(src)
    category = safe_str(info.get("category"))
    if category and _PAYMENT_CATEGORY_RE.search(category):
        return True

    if info.get("type") in PAYMENT_APPMSG_TYPES:
        return True

    # vendor 私有 type 字符串 (如 "hongbao")
    raw_type = src.get("type")
    if isinstance(raw_type, str) and re.search(r"hongbao|redpacket", raw_type, re.I):
        return True

    # 结构缺失时的兜底: 描述必须“整句就是支付文案”，且不能是接龙/链接/小程序
    desc = safe_str(info.get("desc") or info.get("title"))
    if desc and _PAYMENT_DESC_RE.search(desc) and not appmsg.is_relay(info):
        if not info.get("url") and not info.get("mini_program") and not info.get("quote"):
            return True

    # vendor 会把卡片正文译成固定短词；仅在卡片(49)上采信，纯文本消息不受影响
    content = src_content(src).strip()
    if content in ("微信红包", "[红包]", "微信转账", "[转账]"):
        return True
    return False


def skip_reason(src: dict, self_wxid: str = "") -> str | None:
    """消息是否应在进入 AstrBot 之前静默丢弃；返回原因 (供日志归因), 放行返回 None。

    这里是**唯一判据** (to_astrbot_message / WppAccount 共用), 避免两处各写一份
    "什么消息不算消息"而漂移。任何模式 (白名单/全部放行/群 all) 都先过这道门。
    """
    if not isinstance(src, dict):
        return "invalid"

    # ① 系统/服务会话 (公众号、微信团队、微信支付、服务通知、filehelper...)
    sys_id = system_sender(src)
    if sys_id:
        return "official_account" if sys_id.startswith("gh_") else f"system:{sys_id}"

    sender = src_sender(src)
    if not sender:
        return "no-sender"

    # ② 自己发的 / 非入站 (防 vendor 回推自问自答)
    if not src_is_incoming(src):
        return "outgoing"
    if self_wxid and sender == self_wxid:
        return "self"

    # ③ vendor 状态推送 / 微信系统消息 / 撤回
    kind = safe_str(src.get("kind")).lower()
    if kind in ("status", "system"):
        return f"kind:{kind}"
    msg_type = src_msg_type(src)
    if msg_type == MSG_TYPE_STATUS:
        return "status"
    if msg_type == MSG_TYPE_SYSTEM:
        return "system_msg"
    if msg_type == MSG_TYPE_REVOKE:
        return "revoke"

    content = src_content(src)
    if appmsg.is_sysmsg(content):
        return "sysmsg"

    # ④ 红包/转账/支付卡片
    if is_payment_card(src):
        return "payment_card"
    return None


def mentioned(
    content: str,
    self_wxid: str,
    self_nickname: str,
    at_list: list[str] | None = None,
) -> bool:
    """判断群消息是否 @ 了机器人。

    优先采信结构化的 @ 列表 (vendor 的 at_user_list / `<atuserlist>`)，
    没有再回退到文本启发式:
      - `@nickname\\u2005你好` / `@wxid 你好`
      - 旧版 `wxid@chatroom` 形式
      - 仅昵称出现 (部分 vendor 会去掉 @ 前缀)
    """
    if at_list:
        if self_wxid and self_wxid in at_list:
            return True
        if self_nickname and self_nickname in at_list:
            return True
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


def parse_at_list(src: dict) -> list[str]:
    """群消息的 @ 列表 (vendor 结构化字段或 `<atuserlist>` XML)。"""
    return appmsg.parse_at_list(src)


def build_reply_component(src: dict) -> Reply | None:
    """把引用回复上下文转成 AstrBot Reply 组件 (让模型读到被引用内容)。"""
    info = appmsg.parse_appmsg(src)
    ctx = info.get("quote")
    if not isinstance(ctx, dict):
        # 旧版 go_fields 里的转义结构也尝试一下
        go_fields = src.get("go_fields")
        if not isinstance(go_fields, dict):
            return None
        ctx = go_fields.get("ReplyContext") if isinstance(go_fields.get("ReplyContext"), dict) else None
    if not isinstance(ctx, dict):
        return None

    quote = safe_str(ctx.get("content") or ctx.get("quote_content"))
    if not quote:
        return None
    msg_id = safe_str(ctx.get("msg_id") or ctx.get("new_msg_id") or ctx.get("svr_id"))
    sender = safe_str(ctx.get("sender") or ctx.get("from_user_id"))
    try:
        # AstrBot Reply.qq 是 int 字段 (wxid 非数字时置 0)
        qq = int(sender) if sender.isdigit() else 0
    except (TypeError, ValueError):
        qq = 0
    ts = safe_num(ctx.get("time") or ctx.get("create_time"), 0)
    if ts > 1e12:
        ts //= 1000
    return Reply(
        id=msg_id,
        sender_id=sender,
        sender_nickname=safe_str(ctx.get("nickname") or ctx.get("display_name") or sender),
        message_str=quote,
        text=quote,
        qq=qq,
        time=ts,
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
    if skip_reason(src, self_wxid):
        return None
    from_wxid = src_sender(src)
    msg_type = src_msg_type(src)

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
    # 必须优先机器人自己的 wxid: 群消息的 recipient_id 可能是群 ID,
    # 若用它当 self_id, 核心的 At/引用唤醒判断 (At.qq == self_id) 会失效,
    # 心流等插件也无法正确区分"自己/群友"。
    abm.self_id = self_wxid or to_wxid
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
        info = appmsg.parse_appmsg(src)
        file_obj = src.get("file") if isinstance(src.get("file"), dict) else {}
        app_obj = appmsg.app_payload(src)
        app_file = app_obj.get("file") if isinstance(app_obj.get("file"), dict) else {}
        file_info = info.get("file") or {}
        dc: dict = {}
        for candidate in (
            file_obj.get("download_context"),
            app_file.get("download_context"),
            file_info.get("download_context"),
        ):
            if isinstance(candidate, dict) and candidate:
                dc = candidate
                break
        filename = (
            safe_str(app_obj.get("title"))
            or safe_str(file_info.get("filename"))
            or safe_str(file_obj.get("name"))
            or safe_str(file_obj.get("filename"))
            or appmsg.clean_card_text(content, 120)
            or "file"
        )
        if appmsg.is_xml_like(filename):
            filename = "file"
        abm.message_str = f"[文件] {filename}"
        abm.message = [Plain(abm.message_str)]
        abm._wpp_file_meta = {
            "filename": filename,
            "attach_id": safe_str(dc.get("attach_id")) or safe_str(file_info.get("attach_id")),
            "user_name": safe_str(dc.get("user_name")) or safe_str(file_info.get("user_name")),
            "data_len": safe_num(dc.get("data_len") or file_info.get("data_len"), 0),
        }

    elif msg_type == MSG_TYPE_VOICE:
        voice_obj = src.get("voice") if isinstance(src.get("voice"), dict) else {}
        transcript = safe_str(voice_obj.get("transcript")) or safe_str(voice_obj.get("text"))
        if transcript:
            abm.message_str = transcript
            abm.message = [Plain(transcript)]
        else:
            # 没有 vendor 转写: 保留占位 + 下载元数据, 由 wpp_media 下载成 Record
            # 交给 AstrBot 核心 preprocess 的 STT (与 NapCat 等平台一致)
            abm.message_str = "[语音]"
            abm.message = [Plain("[语音]")]
            dc = (
                voice_obj.get("download_context")
                if isinstance(voice_obj.get("download_context"), dict)
                else {}
            )
            abm._wpp_voice_meta = {
                "msg_id": safe_str(dc.get("msg_id")) or msg_id,
                "new_msg_id": safe_str(dc.get("new_msg_id")),
                "client_msg_id": safe_str(dc.get("client_msg_id")),
                "master_buf_id": safe_str(dc.get("master_buf_id")) or "0",
                "length": safe_num(dc.get("length"), 0),
                "format": safe_num(dc.get("format"), 4),
                "chat_room_name": safe_str(dc.get("chat_room_name")) or chatroom_id,
                "from_user_name": safe_str(dc.get("from_user_name")) or from_wxid,
                "to_user_name": safe_str(dc.get("to_user_name")) or to_wxid or self_wxid,
            }

    elif msg_type == MSG_TYPE_IMAGE or safe_str(src.get("kind")).lower() == "image":
        img_obj = src.get("image") if isinstance(src.get("image"), dict) else {}
        local_id = safe_str(src.get("local_id")) or msg_id
        data_len = safe_num(img_obj.get("data_len"), 0)
        cdn_ctx = _pick_cdn_context(img_obj.get("cdn_download_contexts"))
        if not cdn_ctx:
            cdn_ctx = _pick_cdn_context([img_obj.get("cdn_download_context")])
        abm.message_str = "[图片]"
        # 保留占位 Plain: 未 @ 消息懒加载时消息链不能为空,
        # 否则内置群聊上下文/主动回复插件会认为"没有可处理内容"。
        # enrich_media 会用真正的 Image 替换它。
        abm.message = [Plain("[图片]")]
        # DownloadImg 的 to_wxid: 私聊=机器人自己, 群聊=群 ID (与 wpp-openclaw 一致)
        abm._wpp_image_meta = {
            "msg_id": local_id,
            "to_wxid": chatroom_id if is_group else (to_wxid or self_wxid),
            "data_len": data_len,
            "cdn": cdn_ctx,
        }

    elif msg_type == MSG_TYPE_VIDEO or safe_str(src.get("kind")).lower() == "video":
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

    elif msg_type == MSG_TYPE_APP:
        info = appmsg.parse_appmsg(src)
        text = appmsg.format_appmsg(src, info)
        if not text:
            text = appmsg.clean_card_text(content)
        if not text:
            return None
        abm.message_str = text
        abm.message = [Plain(text)]

    elif msg_type in (MSG_TYPE_LOCATION, MSG_TYPE_CONTACT):
        info = appmsg.parse_appmsg(src)
        text = appmsg.format_appmsg(src, info)
        if not text:
            text = appmsg.clean_card_text(content)
        if not text:
            return None
        abm.message_str = text
        abm.message = [Plain(text)]

    elif msg_type == MSG_TYPE_EMOJI:
        text = appmsg.clean_card_text(content) if appmsg.is_xml_like(content) else content
        abm.message_str = text or "[表情]"
        abm.message = [Plain(abm.message_str)]

    else:
        text = appmsg.clean_card_text(content) if appmsg.is_xml_like(content) else content
        if not text:
            return None
        abm.message_str = text
        abm.message = [Plain(text)]

    _inject_at_component(src, abm, content, self_wxid, self_nickname)
    _inject_reply_component(src, abm)
    return abm


def _is_file_app(src: dict) -> bool:
    # 兼容只有 `kind="app"` 没有数字 `type` 的推送
    if src_msg_type(src) != MSG_TYPE_APP:
        return False
    app = appmsg.app_payload(src)
    return safe_str(app.get("category")).lower() in ("file", "file_app", "files")


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
    self_wxid: str,
    self_nickname: str,
) -> None:
    if abm.type != MessageType.GROUP_MESSAGE:
        return
    self_id = self_wxid or abm.self_id
    if not self_id:
        return
    at_list = appmsg.parse_at_list(src)
    if not mentioned(content, self_id, self_nickname, at_list):
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
    "MSG_TYPE_CONTACT",
    "MSG_TYPE_VIDEO",
    "MSG_TYPE_EMOJI",
    "MSG_TYPE_LOCATION",
    "MSG_TYPE_APP",
    "MSG_TYPE_STATUS",
    "MSG_TYPE_SYSTEM",
    "MSG_TYPE_REVOKE",
    "SYSTEM_SENDERS",
    "PAYMENT_APPMSG_TYPES",
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
    "src_msg_type",
    "src_is_incoming",
    "src_is_filehelper",
    "system_sender",
    "is_payment_card",
    "skip_reason",
    "mentioned",
    "parse_at_list",
    "build_reply_component",
    "to_astrbot_message",
]
