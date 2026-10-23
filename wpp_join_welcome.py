"""进群欢迎语的配置解析 / 成员 diff / 文案渲染 (v0.9.0)。

纯函数模块：不碰网络、不碰存储、不发消息，方便单测。真正的抓取/发送在
``WppAccount`` 里。

设计（移植自 Hermes plugin/inbound/join_welcome.py，去掉 LLM 润色）：

* **配置**：账号运行时 JSON ``wpp_whitelist.json`` 的 ``join_welcome`` 段，
  3 秒热更新；``enabled`` 默认 false —— 不配就一个字都不发。
* **模式**：``whitelist``（动态取当前群白名单，推荐）/ ``allowlist`` /
  ``blocklist`` / ``all``。未知模式一律回落 ``allowlist``（宁可少发，不能刷屏）。
* **触发**：厂商通常只推占位符，拿不到「谁进群」；所以欢迎语只建立在
  **成员列表 diff** 上。首次同步某群只建基线、不欢迎任何人。
* **maxBatch**：一次 diff 出太多「新人」（基线重建/清表）时整批不发，
  防止把全群欢迎一遍。
* **模板**：``{name}`` 替换为新人昵称（多人用「、」连接），``{group}`` 替换群名，
  查不到群名写「本群」。真 @ 由发送方用 vendor ``At`` 字段完成，模板里的 ``@``
  只是文字。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

MODE_ALLOWLIST = "allowlist"
MODE_BLOCKLIST = "blocklist"
MODE_ALL = "all"
MODE_WHITELIST = "whitelist"
MODES = (MODE_ALLOWLIST, MODE_BLOCKLIST, MODE_ALL, MODE_WHITELIST)

DEFAULT_TEMPLATE = "欢迎 @{name} 加入{group}！"
DEFAULT_MAX_BATCH = 3

#: 占位符通知（msgType=10000）的抓取节流：vendor 可能对一次变动连推多条。
MEMBER_EVENT_THROTTLE_SECONDS = 30.0

_NAME_RE = re.compile(r"\{name\}")
_GROUP_RE = re.compile(r"\{group\}")


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off", ""):
            return False
        return default
    if value is None:
        return default
    return bool(value)


def _as_groups(value: Any) -> tuple[str, ...]:
    """群 ID 列表：接受单个字符串或任意可迭代，去空、保留顺序。"""
    if isinstance(value, str):
        value = [value]
    if isinstance(value, Iterable):
        return tuple(str(x).strip() for x in value if str(x).strip())
    return ()


def _as_hours(value: Any) -> frozenset[int]:
    """静默小时集合：只留 0-23 的整数；字符串/bytes 不迭代。"""
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes)):
        return frozenset()
    hours: set[int] = set()
    for item in value:
        try:
            hour = int(item)
        except (TypeError, ValueError):
            continue
        if 0 <= hour <= 23:
            hours.add(hour)
    return frozenset(hours)


def resolve_cfg(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """把 ``join_welcome`` 原始 JSON 段补全为完整配置（每次调用都从原始 dict 算）。"""
    cfg = dict(raw or {})

    mode = str(cfg.get("mode") or MODE_ALLOWLIST).strip().lower()
    if mode not in MODES:
        mode = MODE_ALLOWLIST

    template = str(cfg.get("template") or DEFAULT_TEMPLATE)
    if "{name}" not in template:
        # 模板漏了占位符时补上，否则一条消息里看不出是谁进群。
        template = f"{template} {{name}}"

    try:
        max_batch = int(cfg.get("maxBatch", cfg.get("max_batch", DEFAULT_MAX_BATCH)))
    except (TypeError, ValueError):
        max_batch = DEFAULT_MAX_BATCH

    return {
        "enabled": _as_bool(cfg.get("enabled"), False),
        "mode": mode,
        "groups": _as_groups(cfg.get("groups")),
        "template": template,
        "mention": _as_bool(cfg.get("mention"), True),
        "quietHours": _as_hours(cfg.get("quietHours", cfg.get("quiet_hours"))),
        "maxBatch": max(1, max_batch),
    }


def should_welcome(
    cfg: Mapping[str, Any],
    chatroom_id: str,
    *,
    group_whitelist: Iterable[str] = (),
    blacklisted_groups: Iterable[str] = (),
) -> bool:
    """这个群是否应该发欢迎语（与「有没有新人」无关）。"""
    if not cfg.get("enabled"):
        return False
    chatroom_id = str(chatroom_id or "").strip()
    if not chatroom_id:
        return False
    # 黑名单永远排除，哪怕 mode=all —— 和消息准入的黑名单语义保持一致。
    if chatroom_id in set(blacklisted_groups or ()):
        return False

    mode = cfg.get("mode")
    if mode == MODE_ALL:
        return True
    if mode == MODE_BLOCKLIST:
        return chatroom_id not in set(cfg.get("groups") or ())
    if mode == MODE_WHITELIST:
        return chatroom_id in set(group_whitelist or ())
    # allowlist（含未知模式回落）
    return chatroom_id in set(cfg.get("groups") or ())


def is_quiet_hour(cfg: Mapping[str, Any], hour: int) -> bool:
    try:
        return int(hour) in (cfg.get("quietHours") or frozenset())
    except (TypeError, ValueError):
        return False


def render(template: str, names: Iterable[str], group_name: str = "") -> str:
    """套模板：多人昵称用「、」连接；空昵称写「新成员」，空群名写「本群」。"""
    joined = "、".join(str(n).strip() for n in names if str(n).strip())
    text = _NAME_RE.sub(joined or "新成员", str(template or DEFAULT_TEMPLATE))
    return _GROUP_RE.sub(str(group_name or "").strip() or "本群", text)


def build(
    cfg: Mapping[str, Any],
    chatroom_id: str,
    added: Iterable[tuple[str, str]],
    *,
    hour: int,
    group_name: str = "",
    group_whitelist: Iterable[str] = (),
    blacklisted_groups: Iterable[str] = (),
) -> tuple[str, list[str]] | None:
    """生成一条欢迎语 ``(text, ats)``；不该发时返回 ``None``。"""
    added = list(added or ())
    if not added:
        return None
    if not should_welcome(
        cfg,
        chatroom_id,
        group_whitelist=group_whitelist,
        blacklisted_groups=blacklisted_groups,
    ):
        return None
    if is_quiet_hour(cfg, hour):
        return None
    if len(added) > int(cfg.get("maxBatch") or DEFAULT_MAX_BATCH):
        return None

    names = [nick or wxid for wxid, nick in added]
    text = render(str(cfg.get("template") or DEFAULT_TEMPLATE), names, group_name)
    ats = [wxid for wxid, _ in added if wxid] if cfg.get("mention", True) else []
    return text, ats


def _baseline_nick(value: Any) -> str:
    """基线成员值兼容 ``{"nick": ...}`` 与旧格式纯字符串。"""
    if isinstance(value, Mapping):
        return str(value.get("nick") or "").strip()
    return str(value or "").strip()


def diff_members(
    baseline: Mapping[str, Any] | None,
    current: Mapping[str, str] | None,
) -> tuple[list[tuple[str, str]], list[str]]:
    """对比成员表：返回 ``(added[(wxid, 当前昵称)], removed[wxid])``。

    基线为空的语义由调用方处理（首次同步只建基线，不欢迎）——这里只做纯 diff。
    """
    old = baseline or {}
    new = current or {}
    added = [(str(wxid), str(nick or "")) for wxid, nick in new.items() if wxid and wxid not in old]
    removed = [str(wxid) for wxid in old if wxid not in new]
    return added, removed


def normalize_state(raw: Any) -> dict[str, Any]:
    """校验/规整 ``members.json``；损坏的结构一律回退为空状态，绝不抛异常。"""
    if not isinstance(raw, Mapping):
        return {"version": 1, "updated_at": 0, "chatrooms": {}}
    chatrooms_raw = raw.get("chatrooms")
    if not isinstance(chatrooms_raw, Mapping):
        chatrooms_raw = {}
    chatrooms: dict[str, Any] = {}
    for chatroom_id, group in chatrooms_raw.items():
        chatroom_id = str(chatroom_id or "").strip()
        if not chatroom_id or not isinstance(group, Mapping):
            continue
        members_raw = group.get("members")
        if not isinstance(members_raw, Mapping):
            members_raw = {}
        members: dict[str, Any] = {}
        for wxid, value in members_raw.items():
            wxid = str(wxid or "").strip()
            if not wxid:
                continue
            nick = _baseline_nick(value)
            first_seen = 0
            if isinstance(value, Mapping):
                try:
                    first_seen = int(value.get("first_seen") or 0)
                except (TypeError, ValueError):
                    first_seen = 0
            members[wxid] = {"nick": nick, "first_seen": first_seen}
        try:
            synced_at = int(group.get("synced_at") or 0)
        except (TypeError, ValueError):
            synced_at = 0
        chatrooms[chatroom_id] = {
            "name": str(group.get("name") or "").strip(),
            "synced_at": synced_at,
            "members": members,
        }
    try:
        updated_at = int(raw.get("updated_at") or 0)
    except (TypeError, ValueError):
        updated_at = 0
    return {"version": 1, "updated_at": updated_at, "chatrooms": chatrooms}


__all__ = [
    "MODE_ALLOWLIST",
    "MODE_BLOCKLIST",
    "MODE_ALL",
    "MODE_WHITELIST",
    "MODES",
    "DEFAULT_TEMPLATE",
    "DEFAULT_MAX_BATCH",
    "MEMBER_EVENT_THROTTLE_SECONDS",
    "resolve_cfg",
    "should_welcome",
    "is_quiet_hour",
    "render",
    "build",
    "diff_members",
    "normalize_state",
]
