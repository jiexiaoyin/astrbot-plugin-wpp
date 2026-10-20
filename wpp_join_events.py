"""进群 / 退群系统通知的识别 (v0.9.0)。

微信把「谁进群了」推成**系统通知**，不是人说的话：

* 常见 `msgType=10000`（`wpp_messages.MSG_TYPE_SYSTEM`），部分 vendor 变体用字符串
  `kind=system` / `group_invite`；
* 厂商经常只推占位符「收到一条微信系统通知」，原文不在 `content` 里，只在
  `src` 的某个嵌套字段 —— 所以文本要在整条 src 里递归找；
* 系统通知不满足任何准入/触发条件，在 `skip_reason` 里会被静默丢弃。

本模块只回答「这条消息是不是成员变动、属于哪个群」。**不解析 wxid**：
通知文本只有昵称，昵称可能重名/是群昵称，不能当 wxid 用。真正的「谁进谁出」
由 `WppAccount` 拉一次成员列表做 diff 得到（见 wpp_join_welcome.diff_members）。

与 Hermes 版 (plugin/inbound/join_events.py) 的差异：

* 输入是 vendor 原始 `src` dict，不是框架消息对象；
* **不劫持 `10002`**：本插件把它当撤回 (`skip_reason` → revoke)，只有它明确
  带着进群/退群文本时才当成成员变动；
* `group_invite` 仅供参考，机器人自己被拉进群时不发欢迎语（没有 `/Group/List`，
  且不该欢迎自己）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .wpp_messages import MSG_TYPE_SYSTEM, safe_str, src_chatroom, src_content, src_msg_type

#: 已知的系统通知类型（字符串形式；数字 10000 单独判断）。
SYSTEM_TYPE_TOKENS = frozenset({"10000", "system"})

#: 机器人自己被拉进群的类型标记。
SELF_JOIN_TOKENS = frozenset({"group_invite"})

#: 递归找原文的边界：vendor 字段名跨版本会变，不猜名字，只按形状筛。
_MAX_RAW_DEPTH = 4
_MAX_CANDIDATE_LEN = 300

#: 昵称两侧可能是英文引号或中文「」；两种都吞掉，但不进捕获组。
_QUOTED = r'["「]?(?P<{name}>[^"「」]{1,64}?)["」]?'


def _q(name: str) -> str:
    """把 ``_QUOTED`` 里的占位符换成命名组名（``str.format`` 会吃掉 ``{1,64}``）。"""
    return _QUOTED.replace("{name}", name)


#: 进群三种文案（带邀请人的放前面，避免被最宽松那条吃掉）。
_JOIN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(_q("inviter") + r"\s*邀请\s*" + _q("member") + r"\s*加入了?群聊"),
    re.compile(_q("member") + r"\s*通过扫描\s*" + _q("inviter") + r"\s*分享的二维码加入"),
    re.compile(_q("member") + r"\s*加入了?群聊"),
)

#: 退群两种文案。
_LEAVE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(_q("kicker") + r"\s*将\s*" + _q("member") + r"\s*移出了?群聊"),
    re.compile(_q("member") + r"\s*退出了?群聊"),
)

#: 机器人自己被拉进群的文案。
_SELF_JOIN_RE = re.compile(r"邀请你加入群聊")


@dataclass(frozen=True)
class JoinNotice:
    """一次群成员变动通知（只说明「哪个群可能变了」）。"""

    kind: str  # join | leave | members_changed | self_join
    chatroom_id: str
    member_name: str = ""
    inviter: str = ""
    raw: str = ""


def _type_token(src: dict) -> str:
    """vendor 的原始类型标记（字符串小写），没有则空串。"""
    for key in ("msgType", "MsgType", "type", "msg_type", "kind"):
        token = safe_str(src.get(key)).strip().lower()
        if token:
            return token
    return ""


def _texts_from_raw(obj, depth: int = 0):
    """递归挖 src 里所有短字符串（系统通知原文在各版本字段名不同）。"""
    if depth > _MAX_RAW_DEPTH:
        return
    if isinstance(obj, str):
        if 0 < len(obj) <= _MAX_CANDIDATE_LEN:
            yield obj
    elif isinstance(obj, dict):
        for value in obj.values():
            yield from _texts_from_raw(value, depth + 1)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            yield from _texts_from_raw(value, depth + 1)


def _candidate_texts(src: dict) -> list[str]:
    """候选原文：`content` 优先，再递归扫整个 src；去重保序。"""
    out: list[str] = []
    seen: set[str] = set()
    for text in (src_content(src), *_texts_from_raw(src)):
        text = safe_str(text).strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def parse_notice(src: dict) -> JoinNotice | None:
    """识别一条 vendor 原始消息是否为群成员变动通知。

    返回 ``None`` 表示与成员变动无关（绝大多数消息）。判定顺序：

    1. ``group_invite`` / 「邀请你加入群聊」⇒ ``self_join``；
    2. 退群文案；3. 进群文案（**不管 msgType**，防厂商改类型后静默失效）；
    4. 群里的 ``10000`` / ``kind=system`` 占位符 ⇒ ``members_changed``
       —— 实测常态：原文只有「收到一条微信系统通知」；
    5. 其余 ``None``。`10002` 不单独触发（它是撤回）。
    """
    if not isinstance(src, dict):
        return None

    chatroom_id = src_chatroom(src)
    token = _type_token(src)
    texts = _candidate_texts(src)

    # ① 机器人自己被拉进群：新群可能还没 chatroom id，调用方据此跳过。
    if token in SELF_JOIN_TOKENS or any(_SELF_JOIN_RE.search(t) for t in texts):
        return JoinNotice(
            kind="self_join",
            chatroom_id=chatroom_id,
            raw=" | ".join(t[:80] for t in texts[:3]),
        )

    # 成员变动只发生在群里。
    if not chatroom_id.endswith("@chatroom"):
        return None

    # ② 退群
    for pattern in _LEAVE_PATTERNS:
        for text in texts:
            m = pattern.search(text)
            if m:
                return JoinNotice(
                    kind="leave",
                    chatroom_id=chatroom_id,
                    member_name=(m.groupdict().get("member") or "").strip(),
                    inviter=(m.groupdict().get("kicker") or "").strip(),
                    raw=text[:200],
                )

    # ③ 进群（文本模式优先于类型）
    for pattern in _JOIN_PATTERNS:
        for text in texts:
            m = pattern.search(text)
            if m:
                return JoinNotice(
                    kind="join",
                    chatroom_id=chatroom_id,
                    member_name=(m.groupdict().get("member") or "").strip(),
                    inviter=(m.groupdict().get("inviter") or "").strip(),
                    raw=text[:200],
                )

    # ④ 占位符：只知道「这个群可能有成员变动」，靠成员列表 diff 补全。
    if token in SYSTEM_TYPE_TOKENS or src_msg_type(src) == MSG_TYPE_SYSTEM:
        return JoinNotice(
            kind="members_changed",
            chatroom_id=chatroom_id,
            raw=" | ".join(t[:80] for t in texts[:3]),
        )
    return None


__all__ = ["JoinNotice", "parse_notice", "SYSTEM_TYPE_TOKENS", "SELF_JOIN_TOKENS"]
