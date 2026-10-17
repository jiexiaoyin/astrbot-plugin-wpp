"""WPP 白名单 / 群回复策略 (纯函数)。

与旧的 `WppAccount._is_allowed` 相比:
  - `require_at_mention` 不再只判断 "内容里有没有 @", 而是必须 @ 机器人自己
    (旧实现里 @ 任何群友都能放行)
  - 策略以 `PolicySnapshot` 传入, 便于单测

判定顺序:
  0. 发送者在 block_users (私聊黑名单) → 拒绝 (私聊/群聊都生效)
  群聊:
  1. 黑名单群 → 拒绝
  2. 白名单里存在群 ID, 且本群不在白名单 → 拒绝
  3. group_reply == none → 拒绝
  4. require_at_mention 且未 @ 机器人 → 拒绝
  5. group_reply == all → 放行
  6. atbot: 内容 @ 了机器人 (或 vendor 的 recipient_id 就是机器人) → 放行
  私聊:
  7. block_users 命中 → 拒绝; 空白名单=全部放行; 否则只放行白名单
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .wpp_messages import mentioned, safe_str


@dataclass
class PolicySnapshot:
    allow_users: set[str] = field(default_factory=set)
    block_users: set[str] = field(default_factory=set)
    blacklist_groups: set[str] = field(default_factory=set)
    group_reply: str = "atbot"
    require_at_mention: bool = False
    self_wxid: str = ""
    self_nickname: str = ""


def is_allowed(
    policy: PolicySnapshot,
    from_wxid: str,
    is_group: bool,
    chatroom_id: str,
    content: str,
    recipient_id: str = "",
) -> bool:
    # 发送者黑名单: 私聊/群聊都生效 (群里屏蔽某个成员)
    if from_wxid and from_wxid in policy.block_users:
        return False

    if is_group:
        if chatroom_id and chatroom_id in policy.blacklist_groups:
            return False

        if policy.allow_users:
            group_whitelisted = chatroom_id in policy.allow_users
            has_group_in_whitelist = any(
                u.endswith("@chatroom") for u in policy.allow_users
            )
            if has_group_in_whitelist and not group_whitelisted:
                return False

        if policy.group_reply == "none":
            return False

        self_wxid = safe_str(policy.self_wxid)
        self_nickname = safe_str(policy.self_nickname)
        at_me = mentioned(content or "", self_wxid, self_nickname)

        if policy.require_at_mention and not at_me:
            return False

        if policy.group_reply == "all":
            return True

        # atbot: vendor 的 recipient_id 就是机器人 (再配合 @ 判断)
        if recipient_id and self_wxid and recipient_id == self_wxid and "@" in (content or ""):
            return True
        return at_me

    if not policy.allow_users:
        return True
    return bool(from_wxid) and from_wxid in policy.allow_users


__all__ = ["PolicySnapshot", "is_allowed"]
