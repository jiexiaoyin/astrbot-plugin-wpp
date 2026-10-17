"""v0.7.0: Dashboard 策略查询/更新 (纯对象, 不启动 AstrBot)。"""

import importlib
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(PLUGIN_DIR.parent))

main_mod = importlib.import_module("astrbot-plugin-wpp.main")


def fake_account():
    acct = SimpleNamespace(
        account_id="wpp",
        allow_users={"u1", "123@chatroom"},
        block_users=set(),
        blacklist_groups=set(),
        proactive_groups={"123@chatroom"},
        group_reply="atbot",
        require_at_mention=False,
        lazy_media=True,
        save_policy=lambda: None,
        stats={"queue_size": 0, "dedup_size": 0, "send": {"sent": 1, "failed": 0}},
    )
    return acct


def fake_instance(acct):
    inst = SimpleNamespace(
        instance_id="wpp",
        heartflow_sync_enabled=True,
        accounts=[acct],
        sync_heartflow_whitelist=lambda reason="": None,
    )
    inst.meta = lambda: SimpleNamespace(name="wpp")
    return inst


class TestPolicyApi(unittest.TestCase):
    def _plugin(self, acct):
        plugin = object.__new__(main_mod.WppPlugin)
        plugin.context = SimpleNamespace(
            platform_manager=SimpleNamespace(platform_insts=[fake_instance(acct)])
        )
        return plugin

    def test_policy_view_sorted_and_no_top_level_data(self):
        plugin = self._plugin(fake_account())
        view = plugin._account_policy_view(fake_account())
        self.assertEqual(view["allow_users"], ["123@chatroom", "u1"])
        self.assertEqual(view["stats"]["send"]["sent"], 1)
        instances = plugin._policy_instances()
        self.assertEqual(instances[0]["id"], "wpp")
        self.assertNotIn("data", instances[0])

    def test_update_allow_users_and_group_reply(self):
        acct = fake_account()
        plugin = self._plugin(acct)
        payload = {
            "account_id": "wpp",
            "allow_users": ["u2", " 123@chatroom "],
            "group_reply": "all",
            "proactive_groups": ["123@chatroom", "456@chatroom"],
        }
        with patch.object(main_mod.request, "json", AsyncMock(return_value=payload)):
            resp = main_mod.asyncio.run(plugin.web_policy_update())
        self.assertTrue(resp)
        self.assertEqual(acct.allow_users, {"u2", "123@chatroom"})
        self.assertEqual(acct.group_reply, "all")
        self.assertEqual(acct.proactive_groups, {"123@chatroom", "456@chatroom"})

    def test_update_rejects_invalid_group_reply(self):
        acct = fake_account()
        plugin = self._plugin(acct)
        payload = {"account_id": "wpp", "group_reply": "invalid"}
        with patch.object(main_mod.request, "json", AsyncMock(return_value=payload)):
            main_mod.asyncio.run(plugin.web_policy_update())
        self.assertEqual(acct.group_reply, "atbot")


if __name__ == "__main__":
    unittest.main()
