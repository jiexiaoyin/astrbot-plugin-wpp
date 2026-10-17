"""WPP 白名单 / 群回复策略 (纯函数)。

核心原则: **准入 ≠ 唤醒**。
本模块只回答"这条消息是否进入 AstrBot", 不设置任何唤醒标志, 也不决定
"机器人要不要回复"。未 @ 放行的消息由 AstrBot 核心 + 决策插件
(Heartflow / 内置 active_reply 等) 决定是否主动接话。

判定顺序:
  0. 发送者在 block_users (私聊黑名单) → 拒绝 (私聊/群聊都生效)
  群聊:
  1. 黑名单群 → 拒绝
  2. 白名单里存在群 ID, 且本群不在白名单 → 拒绝
  3. group_reply == none → 拒绝 (硬开关, proactive_groups 也不能绕过)
  4. 已 @ 机器人 → 放行 (正常唤醒)
  5. 群在白名单或 proactive_groups → 放行 (未 @ 也放行, 覆盖 require_at_mention)
  6. require_at_mention 且未 @ → 拒绝
  7. group_reply == all → 放行 (观察模式, 不唤醒)
  8. 其他 → 拒绝
  私聊:
  9. 空白名单 = 全部放行; 否则只放行白名单

**主动回复与群白名单共用同一份列表**: `allow_users` 里的 `@chatroom`
条目天然就是主动候选群, 不需要再复制到 `proactive_groups`; 后者仅用于
未配置群白名单时的显式补充 (或保留历史配置)。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .wpp_messages import mentioned, safe_str


@dataclass
class PolicySnapshot:
    allow_users: set[str] = field(default_factory=set)
    block_users: set[str] = field(default_factory=set)
    blacklist_groups: set[str] = field(default_factory=set)
    proactive_groups: set[str] = field(default_factory=set)
    group_reply: str = "atbot"
    require_at_mention: bool = False
    self_wxid: str = ""
    self_nickname: str = ""


@dataclass(frozen=True)
class Admission:
    """准入结果。

    allowed:   是否进入 AstrBot 管道
    at_me:     是否 @ 了机器人 (群聊正常唤醒)
    proactive: 是否因 proactive_groups 放行 (未 @ 的主动候选群)
    reason:    判定原因 (日志/排查用)
    """

    allowed: bool
    at_me: bool = False
    proactive: bool = False
    reason: str = ""


def group_whitelist(policy: PolicySnapshot) -> set[str]:
    """返回白名单中的群 ID (allow_users 里以 @chatroom 结尾的项)。"""
    return {
        str(group).strip()
        for group in policy.allow_users
        if str(group).strip().endswith("@chatroom")
    }


def effective_proactive_groups(policy: PolicySnapshot) -> set[str]:
    """主动候选群 = 显式 proactive_groups ∪ 群白名单。

    生产主动回复与白名单共用同一份群列表: 群进入白名单即视为 opt-in
    未 @ 观察, 无需再复制到 proactive_groups。显式列表继续保留, 便于
    未配置群白名单时单独 opt-in, 也不破坏历史配置。
    """
    groups = {
        str(group).strip()
        for group in policy.proactive_groups
        if str(group).strip()
    }
    groups.update(group_whitelist(policy))
    return groups


def classify(
    policy: PolicySnapshot,
    from_wxid: str,
    is_group: bool,
    chatroom_id: str,
    content: str,
    recipient_id: str = "",
    at_list: list[str] | None = None,
) -> Admission:
    """判定消息是否进入 AstrBot, 并返回 @/主动候选信息。"""
    # 发送者黑名单: 私聊/群聊都生效 (群里屏蔽某个成员)
    if from_wxid and from_wxid in policy.block_users:
        return Admission(False, reason="block_user")

    if is_group:
        if chatroom_id and chatroom_id in policy.blacklist_groups:
            return Admission(False, reason="blacklist_group")

        if policy.allow_users:
            group_whitelisted = chatroom_id in policy.allow_users
            has_group_in_whitelist = any(
                u.endswith("@chatroom") for u in policy.allow_users
            )
            if has_group_in_whitelist and not group_whitelisted:
                return Admission(False, reason="group_whitelist")

        if policy.group_reply == "none":
            return Admission(False, reason="group_none")

        at_me = mentioned(
            content or "",
            safe_str(policy.self_wxid),
            safe_str(policy.self_nickname),
            at_list,
        )
        proactive = bool(chatroom_id) and chatroom_id in effective_proactive_groups(
            policy
        )

        if at_me:
            return Admission(True, at_me=True, proactive=proactive, reason="at_me")
        if proactive:
            # 主动候选群: 未 @ 也放行, 但适配器不设置唤醒标志。
            return Admission(True, proactive=True, reason="proactive")
        if policy.require_at_mention:
            return Admission(False, reason="require_at")
        if policy.group_reply == "all":
            return Admission(True, reason="group_all")
        return Admission(False, reason="no_at")

    if not policy.allow_users:
        return Admission(True, reason="private_all")
    allowed = bool(from_wxid) and from_wxid in policy.allow_users
    return Admission(allowed, reason="private_allow" if allowed else "private_whitelist")


def is_allowed(
    policy: PolicySnapshot,
    from_wxid: str,
    is_group: bool,
    chatroom_id: str,
    content: str,
    recipient_id: str = "",
    at_list: list[str] | None = None,
) -> bool:
    """向后兼容的布尔接口。"""
    return classify(
        policy,
        from_wxid,
        is_group,
        chatroom_id,
        content,
        recipient_id,
        at_list,
    ).allowed


__all__ = [
    "Admission",
    "PolicySnapshot",
    "classify",
    "effective_proactive_groups",
    "group_whitelist",
    "is_allowed",
]
