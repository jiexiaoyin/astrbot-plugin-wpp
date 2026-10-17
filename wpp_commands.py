"""filehelper 管理命令 (在「文件传输助手」里发指令管理当前账号的白名单/群策略)。

命令注册表化: help 自动遍历注册表, 新命令只需加一行。
所有命令只影响**本账号** (WppAccount 持有的 allow_users / block_users /
blacklist_groups / group_reply)。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from astrbot import logger

from .wpp_messages import split_args

if TYPE_CHECKING:
    from .wpp_account import WppAccount


class FilehelperCommands:
    def __init__(self, account: "WppAccount") -> None:
        self.account = account
        self._commands: list[tuple[str, str, Any]] = [
            ("/user add|del <wxid> [wxid...]", "私聊白名单增删 (批量)", self._user),
            ("/user list", "查看私聊白名单", self._user),
            ("/block add|del|list <wxid> [wxid...]", "私聊黑名单 (全部放行模式下排除)", self._block),
            ("/group add|del <群ID> [群ID...]", "群白名单增删 (批量)", self._group),
            ("/group list", "查看群白名单", self._group),
            ("/blacklist add|del|list <群ID> [群ID...]", "黑名单群管理 (批量)", self._blacklist),
            ("/proactive add|del|list <群ID> [群ID...]", "主动回复候选群 (未@也放行, 不强制回复)", self._proactive),
            ("/mode atbot|none|all", "群回复模式", self._mode),
            ("/at on|off", "群聊是否必须@才回复", self._at),
            ("/help", "显示本帮助", self._help),
        ]

    @property
    def registry(self) -> list[tuple[str, str, Any]]:
        return self._commands

    async def handle(self, content: str, reply) -> None:
        parts = content.strip().split()
        cmd = (parts[0] if parts else "").lower()
        args = parts[1:]
        for usage, _desc, handler in self._commands:
            if cmd == usage.split()[0].lower():
                try:
                    await handler(args, reply)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[WPP:{self.account.account_id} FILEHELPER] 命令 {cmd} 失败: {e}")
                    await reply(f"命令 {cmd} 执行失败: {e}")
                return
        await reply(f"未知命令: {cmd}\n用 /help 查看全部命令。")

    # ------------------------------------------------------------------ 命令实现
    async def _user(self, args: list[str], reply) -> None:
        acct = self.account
        action = (args[0] if args else "").strip().lower()
        targets = split_args(args[1:])
        if action == "list" or (action == "" and not targets):
            users = [u for u in sorted(acct.allow_users) if not u.endswith("@chatroom")]
            await reply(
                f"📋 私聊白名单 ({len(users)}):\n"
                + ("\n".join(f"- {u}" for u in users) if users else "(空)")
            )
            return
        if action == "add" and targets:
            added = [t for t in targets if t not in acct.allow_users]
            acct.allow_users.update(targets)
            acct.save_policy()
            await reply(
                f"✅ 已授权私聊白名单: {', '.join(added) or '(均已存在)'}\n"
                f"当前 ({len(acct.allow_users)}): {', '.join(sorted(acct.allow_users))}"
            )
            return
        if action == "del" and targets:
            removed = [t for t in targets if t in acct.allow_users]
            missing = [t for t in targets if t not in acct.allow_users]
            for t in removed:
                acct.allow_users.discard(t)
            acct.save_policy()
            msg = f"✅ 已移除私聊白名单: {', '.join(removed) or '(无)'}"
            if missing:
                msg += f"\n⚠️ 不在白名单: {', '.join(missing)}"
            msg += f"\n当前 ({len(acct.allow_users)}): {', '.join(sorted(acct.allow_users)) or '(空)'}"
            await reply(msg)
            return
        await reply("用法: /user add <wxid> [wxid...] | /user del <wxid> [wxid...] | /user list")

    async def _block(self, args: list[str], reply) -> None:
        """私聊黑名单: 全部放行模式下排除指定 wxid (群聊里也屏蔽该成员)。"""
        acct = self.account
        action = (args[0] if args else "").strip().lower()
        targets = split_args(args[1:])
        if action == "list" or (action == "" and not targets):
            users = sorted(acct.block_users)
            await reply(
                f"🚫 私聊黑名单 ({len(users)}):\n"
                + ("\n".join(f"- {u}" for u in users) if users else "(空)")
            )
            return
        if action == "add" and targets:
            added = [t for t in targets if t not in acct.block_users]
            removed_from_wl = []
            for t in targets:
                acct.block_users.add(t)
                if t in acct.allow_users:
                    acct.allow_users.discard(t)
                    removed_from_wl.append(t)
            acct.save_policy()
            note = f"\n(已自动从白名单移除: {', '.join(removed_from_wl)})" if removed_from_wl else ""
            await reply(
                f"✅ 已加入私聊黑名单: {', '.join(added) or '(均已存在)'}{note}\n"
                f"当前 ({len(acct.block_users)}): {', '.join(sorted(acct.block_users))}"
            )
            return
        if action == "del" and targets:
            removed = [t for t in targets if t in acct.block_users]
            missing = [t for t in targets if t not in acct.block_users]
            for t in removed:
                acct.block_users.discard(t)
            acct.save_policy()
            msg = f"✅ 已移出私聊黑名单: {', '.join(removed) or '(无)'}"
            if missing:
                msg += f"\n⚠️ 不在黑名单: {', '.join(missing)}"
            msg += f"\n当前 ({len(acct.block_users)}): {', '.join(sorted(acct.block_users)) or '(空)'}"
            await reply(msg)
            return
        await reply("用法: /block add <wxid> [wxid...] | /block del <wxid> [wxid...] | /block list")

    async def _group(self, args: list[str], reply) -> None:
        acct = self.account
        action = (args[0] if args else "").strip().lower()
        targets = split_args(args[1:])
        if action == "list" or (action == "" and not targets):
            groups = [u for u in sorted(acct.allow_users) if u.endswith("@chatroom")]
            await reply(
                f"📋 群白名单 ({len(groups)}):\n"
                + ("\n".join(f"- {u}" for u in groups) if groups else "(空)")
                + "\n说明: 白名单群自动作为主动回复候选 (未@也放行, 不强制回复)"
            )
            return
        if action == "add" and targets:
            added = [t for t in targets if t not in acct.allow_users]
            for t in targets:
                acct.allow_users.add(t)
                acct.blacklist_groups.discard(t)
            acct.save_policy()
            groups = sorted(u for u in acct.allow_users if u.endswith("@chatroom"))
            await reply(
                f"✅ 已授权群白名单: {', '.join(added) or '(均已存在)'}\n"
                f"当前群 ({len(groups)}): {', '.join(groups) or '(空)'}\n"
                "白名单群已自动纳入主动回复候选 (未@也放行, 由决策插件决定是否回复)"
            )
            return
        if action == "del" and targets:
            removed = [t for t in targets if t in acct.allow_users]
            missing = [t for t in targets if t not in acct.allow_users]
            for t in removed:
                acct.allow_users.discard(t)
            acct.save_policy()
            groups = sorted(u for u in acct.allow_users if u.endswith("@chatroom"))
            msg = f"✅ 已移除群白名单: {', '.join(removed) or '(无)'}"
            if missing:
                msg += f"\n⚠️ 不在白名单: {', '.join(missing)}"
            msg += f"\n当前群 ({len(groups)}): {', '.join(groups) or '(空)'}"
            await reply(msg)
            return
        await reply("用法: /group add <群ID> [群ID...] | /group del <群ID> [群ID...] | /group list")

    async def _blacklist(self, args: list[str], reply) -> None:
        acct = self.account
        action = (args[0] if args else "").strip().lower()
        targets = split_args(args[1:])
        if action == "add" and targets:
            removed_from_wl = []
            for t in targets:
                acct.blacklist_groups.add(t)
                if t in acct.allow_users:
                    acct.allow_users.discard(t)
                    removed_from_wl.append(t)
            acct.save_policy()
            note = f"\n(已自动从白名单移除: {', '.join(removed_from_wl)})" if removed_from_wl else ""
            await reply(
                f"✅ 已加入黑名单群: {', '.join(targets)}{note}\n"
                f"当前黑名单 ({len(acct.blacklist_groups)}): {', '.join(sorted(acct.blacklist_groups)) or '(空)'}"
            )
        elif action == "del" and targets:
            removed = [t for t in targets if t in acct.blacklist_groups]
            missing = [t for t in targets if t not in acct.blacklist_groups]
            for t in removed:
                acct.blacklist_groups.discard(t)
            acct.save_policy()
            msg = f"✅ 已移出黑名单群: {', '.join(removed) or '(无)'}"
            if missing:
                msg += f"\n⚠️ 不在黑名单: {', '.join(missing)}"
            msg += f"\n当前黑名单群 ({len(acct.blacklist_groups)}): {', '.join(sorted(acct.blacklist_groups)) or '(空)'}"
            await reply(msg)
        elif action == "list":
            await reply(
                f"当前黑名单群 ({len(acct.blacklist_groups)}):\n"
                + ("\n".join(f"- {g}" for g in sorted(acct.blacklist_groups)) if acct.blacklist_groups else "(空)")
            )
        else:
            await reply("用法: /blacklist add <群ID> [群ID...] | del <群ID> [群ID...] | list")

    async def _proactive(self, args: list[str], reply) -> None:
        """主动回复候选群: 未 @ 消息也放行, 但由决策插件决定是否回复。"""
        acct = self.account
        action = (args[0] if args else "").strip().lower()
        targets = split_args(args[1:])

        def _umo(group_id: str) -> str:
            pid = str(getattr(acct.platform, "instance_id", "") or "wpp")
            return f"{pid}:GroupMessage:{group_id}"

        auto_groups = sorted(
            str(u) for u in acct.allow_users if str(u).endswith("@chatroom")
        )
        if action == "list" or (action == "" and not targets):
            explicit = sorted(acct.proactive_groups)
            effective = sorted(set(explicit) | set(auto_groups))
            lines = [f"👀 主动回复候选群 ({len(effective)}):"]
            if auto_groups:
                lines.append(f"白名单自动纳入 ({len(auto_groups)}):")
                for g in auto_groups:
                    lines.append(f"- {g}")
                    lines.append(f"  UMO: {_umo(g)}")
            if explicit:
                lines.append(f"显式候选 ({len(explicit)}):")
                for g in explicit:
                    lines.append(f"- {g}")
                    lines.append(f"  UMO: {_umo(g)}")
            if not effective:
                lines.append("(空)")
            lines.append(
                "说明: 只放行未@消息, 不强制回复; 由 Heartflow/active_reply 决定。"
                "群白名单自动共用, 无需重复 /proactive add"
            )
            await reply("\n".join(lines))
            return

        if action == "add" and targets:
            added = [
                t
                for t in targets
                if t not in acct.proactive_groups and t not in auto_groups
            ]
            already = [
                t
                for t in targets
                if t in acct.proactive_groups or t in auto_groups
            ]
            acct.proactive_groups.update(
                t for t in targets if t not in auto_groups
            )
            acct.save_policy()
            msg = f"✅ 已加入主动回复候选群: {', '.join(added) or '(无新增)'}"
            if already:
                msg += f"\nℹ️ 已在候选中 (含白名单自动纳入): {', '.join(already)}"
            blocked = (
                [t for t in targets if t not in auto_groups]
                if auto_groups
                else []
            )
            if blocked:
                msg += (
                    f"\n⚠️ 群白名单已启用, 以下群未进白名单, 显式加入也不会放行: "
                    f"{', '.join(blocked)}; 请先 /group add"
                )
            msg += (
                f"\n当前显式列表 ({len(acct.proactive_groups)}): "
                f"{', '.join(sorted(acct.proactive_groups)) or '(空)'}"
                "\n注意: 只放行未@消息, 不会强制回复; 请同时启用 Heartflow 等决策插件"
            )
            await reply(msg)
            return

        if action == "del" and targets:
            removed = [t for t in targets if t in acct.proactive_groups]
            missing = [t for t in targets if t not in acct.proactive_groups]
            for t in removed:
                acct.proactive_groups.discard(t)
            acct.save_policy()
            msg = f"✅ 已移出显式主动候选: {', '.join(removed) or '(无)'}"
            if missing:
                msg += f"\n⚠️ 不在显式名单: {', '.join(missing)}"
            still_auto = [t for t in targets if t in auto_groups]
            if still_auto:
                msg += (
                    f"\nℹ️ 仍在群白名单中, 因此仍会自动作为主动候选: "
                    f"{', '.join(still_auto)}; 如需完全关闭请 /group del"
                )
            msg += (
                f"\n当前显式列表 ({len(acct.proactive_groups)}): "
                f"{', '.join(sorted(acct.proactive_groups)) or '(空)'}"
            )
            await reply(msg)
            return

        await reply(
            "用法: /proactive add <群ID> [群ID...] | "
            "/proactive del <群ID> [群ID...] | /proactive list"
        )

    async def _mode(self, args: list[str], reply) -> None:
        acct = self.account
        mode = (args[0] if args else "").strip().lower()
        if mode not in ("atbot", "none", "all"):
            await reply(
                "用法: /mode atbot|none|all\n"
                "atbot=只放行@机器人的群消息 / none=忽略群消息 / "
                "all=放行所有群消息(观察模式, 不强制回复)\n"
                f"当前: {acct.group_reply}"
            )
            return
        acct.group_reply = mode
        acct.save_policy()
        await reply(
            f"✅ 群消息准入模式已设为: {mode}\n"
            "(atbot=只放行@ / none=忽略群 / all=放行所有群消息, 不强制回复)"
        )

    async def _at(self, args: list[str], reply) -> None:
        acct = self.account
        mode = (args[0] if args else "").strip().lower()
        if mode in ("on", "1", "true"):
            acct.require_at_mention = True
            acct.save_policy()
            await reply("✅ 群聊已设为必须 @ 才回复")
        elif mode in ("off", "0", "false"):
            acct.require_at_mention = False
            acct.save_policy()
            await reply("✅ 群聊已解除必须 @ (主动候选群内的未@消息仍由决策插件判断)")
        else:
            await reply(
                f"用法: /at on|off\n当前: {'on (必须@)' if acct.require_at_mention else 'off (不必@)'}"
            )

    async def _help(self, args: list[str], reply) -> None:
        lines = [f"【WPP 命令 (账号 {self.account.account_id})】"]
        for usage, desc, _handler in self._commands:
            lines.append(f"{usage:<32}  {desc}")
        await reply("\n".join(lines))


__all__ = ["FilehelperCommands"]
