"""v0.6.3 WPP 群白名单 → Heartflow chat_whitelist 同步。"""

import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(PLUGIN_DIR.parent))

sync_mod = importlib.import_module("astrbot-plugin-wpp.wpp_heartflow")


class TestPureHelpers(unittest.TestCase):
    def test_collect_group_ids(self):
        accounts = [
            SimpleNamespace(
                allow_users={"wxid_a", "g1@chatroom"},
                proactive_groups={"g2@chatroom"},
            ),
            SimpleNamespace(
                allow_users={"g1@chatroom"},
                proactive_groups=set(),
            ),
        ]
        # 群白名单必须包括心流白名单: g2 只在 proactive_groups, 不在群白名单,
        # 适配器会在群白名单检查时拒绝它, 因此不能同步给 Heartflow。
        self.assertEqual(
            sync_mod.collect_group_ids(accounts),
            {"g1@chatroom"},
        )

    def test_collect_group_ids_without_group_whitelist_keeps_proactive(self):
        accounts = [
            SimpleNamespace(
                allow_users={"wxid_a"},
                proactive_groups={"g2@chatroom"},
            ),
        ]
        self.assertEqual(
            sync_mod.collect_group_ids(accounts),
            {"g2@chatroom"},
        )

    def test_custom_mode_is_subset_of_group_whitelist(self):
        accounts = [
            SimpleNamespace(
                allow_users={"g1@chatroom", "g2@chatroom"},
                proactive_groups=set(),
            ),
        ]
        groups, rejected = sync_mod.resolve_heartflow_groups(
            accounts,
            custom_groups=["g1@chatroom", "g3@chatroom"],
            mode="custom",
        )
        self.assertEqual(groups, {"g1@chatroom"})
        self.assertEqual(rejected, {"g3@chatroom"})

    def test_custom_mode_empty_whitelist_allows_custom_groups(self):
        accounts = [SimpleNamespace(allow_users={"wxid_a"}, proactive_groups=set())]
        groups, rejected = sync_mod.resolve_heartflow_groups(
            accounts, custom_groups=["g9@chatroom"], mode="custom"
        )
        self.assertEqual(groups, {"g9@chatroom"})
        self.assertEqual(rejected, set())

    def test_auto_mode_references_group_whitelist(self):
        accounts = [
            SimpleNamespace(
                allow_users={"wxid_a", "g1@chatroom"},
                proactive_groups={"g2@chatroom"},
            ),
        ]
        groups, rejected = sync_mod.resolve_heartflow_groups(accounts, mode="auto")
        self.assertEqual(groups, {"g1@chatroom"})
        self.assertEqual(rejected, {"g2@chatroom"})

    def test_build_umos_sorted_and_dedup(self):
        self.assertEqual(
            sync_mod.build_umos("wpp", ["b@chatroom", "a@chatroom", "a@chatroom"]),
            ["wpp:GroupMessage:a@chatroom", "wpp:GroupMessage:b@chatroom"],
        )

    def test_merge_keeps_other_platforms(self):
        existing = [
            "wpp:GroupMessage:old@chatroom",
            "qq:GroupMessage:123",
            "manual:GroupMessage:x",
        ]
        merged = sync_mod.merge_whitelist(
            existing, "wpp", ["wpp:GroupMessage:new@chatroom"]
        )
        self.assertEqual(
            merged,
            [
                "manual:GroupMessage:x",
                "qq:GroupMessage:123",
                "wpp:GroupMessage:new@chatroom",
            ],
        )

    def test_merge_removes_wpp_entries_when_source_empty(self):
        existing = ["wpp:GroupMessage:old@chatroom", "qq:GroupMessage:123"]
        self.assertEqual(
            sync_mod.merge_whitelist(existing, "wpp", []),
            ["qq:GroupMessage:123"],
        )


class TestSyncConfig(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "astrbot_plugin_Heartflow_config.json"
        self.path.write_bytes(
            b"\xef\xbb\xbf"
            + json.dumps(
                {
                    "enable_heartflow": True,
                    "whitelist_enabled": True,
                    "chat_whitelist": [
                        "wpp:GroupMessage:old@chatroom",
                        "qq:GroupMessage:123",
                    ],
                },
                ensure_ascii=False,
            ).encode("utf-8")
        )
        self.patch = patch.object(
            sync_mod, "heartflow_config_path", return_value=self.path
        )
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def _read(self):
        return json.loads(self.path.read_text(encoding="utf-8-sig"))

    async def test_sync_writes_merged_list_and_keeps_bom(self):
        sync_mod.configure(None)
        result = sync_mod.sync_heartflow_whitelist(
            "wpp", ["test-group@chatroom"], "test"
        )
        self.assertTrue(result["changed"])
        data = self._read()
        self.assertEqual(
            data["chat_whitelist"],
            ["qq:GroupMessage:123", "wpp:GroupMessage:test-group@chatroom"],
        )
        self.assertTrue(data["enable_heartflow"])
        self.assertTrue(self.path.read_bytes().startswith(b"\xef\xbb\xbf"))

    async def test_sync_is_idempotent(self):
        sync_mod.configure(None)
        sync_mod.sync_heartflow_whitelist("wpp", ["g@chatroom"], "first")
        result = sync_mod.sync_heartflow_whitelist("wpp", ["g@chatroom"], "second")
        self.assertFalse(result["changed"])

    async def test_updates_live_heartflow_instance(self):
        instance = SimpleNamespace(
            chat_whitelist=[], config={"enable_heartflow": True}
        )
        meta = SimpleNamespace(
            name="astrbot_plugin_Heartflow",
            module_path="astrbot_plugin_Heartflow.main",
            star_cls=instance,
        )
        context = SimpleNamespace(get_all_stars=lambda: [meta])
        sync_mod.configure(context)
        result = sync_mod.sync_heartflow_whitelist("wpp", ["g@chatroom"], "live")
        self.assertTrue(result["live"])
        expected = ["qq:GroupMessage:123", "wpp:GroupMessage:g@chatroom"]
        self.assertEqual(instance.chat_whitelist, expected)
        self.assertEqual(instance.config["chat_whitelist"], expected)

    async def test_missing_config_is_noop(self):
        self.path.unlink()
        sync_mod.configure(None)
        result = sync_mod.sync_heartflow_whitelist("wpp", ["g@chatroom"], "missing")
        self.assertFalse(result["changed"])
        self.assertFalse(result["live"])


if __name__ == "__main__":
    unittest.main()
