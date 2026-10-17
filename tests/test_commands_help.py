"""filehelper /help 输出: 微信纯文本 (无 Markdown) + 策略摘要 + 单命令详情。"""

import importlib
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(PLUGIN_DIR.parent))

commands_mod = importlib.import_module("astrbot-plugin-wpp.wpp_commands")


def make_account(**overrides):
    data = dict(
        account_id="test",
        allow_users={"wxid_a", "g1@chatroom", "g2@chatroom"},
        block_users={"wxid_b"},
        blacklist_groups={"g9@chatroom"},
        proactive_groups={"g2@chatroom", "g3@chatroom"},
        group_reply="atbot",
        require_at_mention=False,
    )
    data.update(overrides)
    return SimpleNamespace(**data)


class TestHelp(unittest.IsolatedAsyncioTestCase):
    async def _help(self, content, account=None):
        replies = []

        async def reply(text):
            replies.append(text)

        account = account or make_account()
        await commands_mod.FilehelperCommands(account).handle(content, reply)
        return replies[-1]

    async def test_help_shows_version_and_policy_summary(self):
        text = await self._help("/help")
        self.assertIn(f"版本 {commands_mod.plugin_version()}", text)
        self.assertIn("私聊白名单：1 人", text)
        self.assertIn("私聊黑名单：1 人", text)
        self.assertIn("群白名单：2 个（自动主动候选）", text)
        self.assertIn("主动候选：显式 2 个，生效 2 个", text)
        # 群白名单非空时, 白名单外的显式候选不会放行。
        self.assertIn("群模式：atbot", text)
        self.assertIn("必须@：关闭", text)

    async def test_help_is_wechat_plain_text(self):
        text = await self._help("/help")
        self.assertNotIn("**", text)
        self.assertNotIn("`", text)
        self.assertNotIn("|", text)

    async def test_help_none_mode_has_no_effective_proactive(self):
        text = await self._help("/help", make_account(group_reply="none"))
        self.assertIn("群模式：none", text)
        self.assertIn("生效 0 个", text)

    async def test_help_blacklisted_group_not_effective(self):
        text = await self._help(
            "/help", make_account(blacklist_groups={"g1@chatroom"})
        )
        self.assertIn("生效 1 个", text)

    async def test_help_documents_new_versions_semantics(self):
        text = await self._help("/help")
        self.assertIn("/group add/del", text)
        self.assertIn("自动作为主动候选", text)
        self.assertIn("/proactive add", text)
        self.assertIn("未配置群白名单时，可用 /proactive add 补充", text)
        self.assertIn("只放行消息，不强制回复", text)
        self.assertIn("auto / custom", text)
        self.assertIn("群白名单必须包含心流白名单", text)
        self.assertIn("/wpp_policy", text)

    async def test_help_single_command(self):
        text = await self._help("/help group")
        self.assertTrue(text.startswith("【/group"))
        self.assertIn("/group add/del 群ID", text)
        self.assertIn("/group list", text)

    async def test_help_single_command_accepts_bare_name(self):
        text = await self._help("/help proactive")
        self.assertTrue(text.startswith("【/proactive"))
        self.assertIn("显式补充主动候选", text)

    async def test_help_unknown_command(self):
        text = await self._help("/help nope")
        self.assertIn("未找到命令: /nope", text)
        self.assertIn("/help", text)

    def test_plugin_version_reads_metadata(self):
        self.assertNotEqual(commands_mod.plugin_version(), "dev")


if __name__ == "__main__":
    unittest.main()
