"""v0.7.0: Dashboard 策略查询/更新 (纯对象, 不启动 AstrBot)。"""

import importlib
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(PLUGIN_DIR.parent))

main_mod = importlib.import_module("astrbot-plugin-wpp.main")
constants = importlib.import_module("astrbot-plugin-wpp.wpp_constants")


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
        stats={
            "status": {
                "online": True,
                "wxid": "wxid_bot",
                "nickname": "小助理",
                "message": "在线",
            },
            "queue_size": 0,
            "dedup_size": 0,
            "send": {"sent": 1, "failed": 0},
        },
    )
    return acct


def fake_instance(acct):
    inst = SimpleNamespace(
        instance_id="wpp",
        heartflow_sync_enabled=True,
        heartflow_whitelist_mode="custom",
        heartflow_groups={"123@chatroom"},
        accounts=[acct],
        sync_heartflow_whitelist=lambda reason="": None,
        _refresh_account_status=AsyncMock(),
    )
    inst.meta = lambda: SimpleNamespace(name=constants.PLATFORM_NAME)
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
        self.assertEqual(view["auto_proactive_groups"], ["123@chatroom"])
        self.assertEqual(view["effective_proactive_groups"], ["123@chatroom"])
        self.assertEqual(view["stats"]["send"]["sent"], 1)
        instances = plugin._policy_instances()
        self.assertEqual(instances[0]["id"], "wpp")
        self.assertEqual(instances[0]["heartflow_mode"], "custom")
        self.assertEqual(instances[0]["heartflow_target_groups"], ["123@chatroom"])
        self.assertEqual(instances[0]["heartflow_rejected_groups"], [])
        self.assertNotIn("data", instances[0])

    def test_policy_query_exposes_version_and_status(self):
        plugin = self._plugin(fake_account())
        resp = main_mod.asyncio.run(plugin.web_policy_query())
        self.assertTrue(resp)
        body = json.loads(resp.body)
        self.assertNotIn("data", body)
        self.assertNotEqual(body["version"], "dev")
        status = body["instances"][0]["accounts"][0]["stats"]["status"]
        self.assertTrue(status["online"])
        self.assertEqual(status["wxid"], "wxid_bot")

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

    def test_update_accepts_newline_and_semicolon_lists(self):
        acct = fake_account()
        plugin = self._plugin(acct)
        payload = {
            "account_id": "wpp",
            "allow_users": "u1\n123@chatroom;456@chatroom",
        }
        with patch.object(main_mod.request, "json", AsyncMock(return_value=payload)):
            main_mod.asyncio.run(plugin.web_policy_update())
        self.assertEqual(acct.allow_users, {"u1", "123@chatroom", "456@chatroom"})

    def test_update_rejects_invalid_group_reply(self):
        acct = fake_account()
        plugin = self._plugin(acct)
        payload = {"account_id": "wpp", "group_reply": "invalid"}
        with patch.object(main_mod.request, "json", AsyncMock(return_value=payload)):
            main_mod.asyncio.run(plugin.web_policy_update())
        self.assertEqual(acct.group_reply, "atbot")

    def test_update_rejects_non_group_ids_in_group_lists(self):
        acct = fake_account()
        plugin = self._plugin(acct)
        cases = (
            ("blacklist_groups", ["wxid_not_a_group"], set()),
            ("proactive_groups", ["123@chatroom", "not-a-group"], {"123@chatroom"}),
            ("block_users", ["123@chatroom"], set()),
        )
        for field, value, expected in cases:
            with self.subTest(field=field):
                payload = {"account_id": "wpp", field: value}
                with patch.object(main_mod.request, "json", AsyncMock(return_value=payload)):
                    resp = main_mod.asyncio.run(plugin.web_policy_update())
                body = json.loads(resp.body)
                self.assertEqual(body["status"], "error")
                self.assertIn(field, body["message"])
                self.assertEqual(getattr(acct, field), expected)

    def test_update_is_atomic_on_validation_error(self):
        acct = fake_account()
        plugin = self._plugin(acct)
        payload = {
            "account_id": "wpp",
            "allow_users": ["u2"],
            "blacklist_groups": ["not-a-group"],
        }
        with patch.object(main_mod.request, "json", AsyncMock(return_value=payload)):
            resp = main_mod.asyncio.run(plugin.web_policy_update())
        self.assertEqual(json.loads(resp.body)["status"], "error")
        self.assertEqual(acct.allow_users, {"u1", "123@chatroom"})

    def test_status_refresh_calls_adapter_and_returns_instances(self):
        acct = fake_account()
        inst = fake_instance(acct)
        plugin = object.__new__(main_mod.WppPlugin)
        plugin.context = SimpleNamespace(platform_manager=SimpleNamespace(platform_insts=[inst]))
        with patch.object(main_mod.request, "json", AsyncMock(return_value={})):
            resp = main_mod.asyncio.run(plugin.web_status_refresh())
        self.assertTrue(resp)
        inst._refresh_account_status.assert_awaited_once_with(acct)
        body = json.loads(resp.body)
        self.assertNotIn("data", body)
        self.assertEqual(body["refreshed"][0]["account_id"], "wpp")
        self.assertEqual(body["instances"][0]["id"], "wpp")
        self.assertTrue(body["ok"])
        self.assertEqual(body["failed"], [])

    def test_status_refresh_reports_failures(self):
        acct = fake_account()
        inst = fake_instance(acct)
        inst._refresh_account_status = AsyncMock(side_effect=RuntimeError("offline"))
        plugin = object.__new__(main_mod.WppPlugin)
        plugin.context = SimpleNamespace(platform_manager=SimpleNamespace(platform_insts=[inst]))
        with patch.object(main_mod.request, "json", AsyncMock(return_value={})):
            resp = main_mod.asyncio.run(plugin.web_status_refresh())
        body = json.loads(resp.body)
        self.assertFalse(body["ok"])
        self.assertEqual(body["failed"][0]["account_id"], "wpp")
        self.assertEqual(body["failed"][0]["error"], "刷新失败")
        self.assertNotIn("offline", json.dumps(body, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
