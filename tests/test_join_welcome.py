"""进群欢迎语纯函数 (v0.9.0 wpp_join_welcome)。"""

import importlib
import sys
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(PLUGIN_DIR.parent))

jw = importlib.import_module("astrbot_plugin_wpp.wpp_join_welcome")

ROOM = "10000000001@chatroom"
OTHER = "10000000002@chatroom"


def cfg(**kw):
    return jw.resolve_cfg(kw)


class TestResolveCfg(unittest.TestCase):
    def test_defaults(self):
        c = jw.resolve_cfg(None)
        self.assertFalse(c["enabled"])
        self.assertEqual(c["mode"], jw.MODE_ALLOWLIST)
        self.assertEqual(c["groups"], ())
        self.assertEqual(c["template"], jw.DEFAULT_TEMPLATE)
        self.assertTrue(c["mention"])
        self.assertEqual(c["quietHours"], frozenset())
        self.assertEqual(c["maxBatch"], jw.DEFAULT_MAX_BATCH)

    def test_unknown_mode_falls_back_to_allowlist(self):
        self.assertEqual(jw.resolve_cfg({"mode": "banana"})["mode"], jw.MODE_ALLOWLIST)

    def test_max_batch_bad_value(self):
        self.assertEqual(jw.resolve_cfg({"maxBatch": "x"})["maxBatch"], 3)
        self.assertEqual(jw.resolve_cfg({"maxBatch": 0})["maxBatch"], 1)

    def test_template_without_name_gets_placeholder(self):
        self.assertIn("{name}", jw.resolve_cfg({"template": "欢迎入群"})["template"])

    def test_groups_accepts_single_string(self):
        self.assertEqual(jw.resolve_cfg({"groups": ROOM})["groups"], (ROOM,))

    def test_quiet_hours_filters_invalid(self):
        self.assertEqual(
            jw.resolve_cfg({"quietHours": [0, 23, 24, -1, "x"]})["quietHours"],
            frozenset({0, 23}),
        )


class TestShouldWelcome(unittest.TestCase):
    def test_disabled_never(self):
        c = cfg(enabled=False, mode="all")
        self.assertFalse(jw.should_welcome(c, ROOM))

    def test_allowlist(self):
        c = cfg(enabled=True, mode="allowlist", groups=[ROOM])
        self.assertTrue(jw.should_welcome(c, ROOM))
        self.assertFalse(jw.should_welcome(c, OTHER))

    def test_blocklist(self):
        c = cfg(enabled=True, mode="blocklist", groups=[ROOM])
        self.assertTrue(jw.should_welcome(c, OTHER))
        self.assertFalse(jw.should_welcome(c, ROOM))

    def test_all(self):
        c = cfg(enabled=True, mode="all")
        self.assertTrue(jw.should_welcome(c, ROOM))

    def test_dynamic_whitelist(self):
        c = cfg(enabled=True, mode="whitelist")
        self.assertTrue(jw.should_welcome(c, ROOM, group_whitelist=[ROOM]))
        self.assertFalse(jw.should_welcome(c, OTHER, group_whitelist=[ROOM]))

    def test_blacklisted_group_never(self):
        c = cfg(enabled=True, mode="all")
        self.assertFalse(jw.should_welcome(c, ROOM, blacklisted_groups=[ROOM]))


class TestBuild(unittest.TestCase):
    def test_build_text_and_ats(self):
        c = cfg(enabled=True, mode="all")
        plan = jw.build(c, ROOM, [("wxid_a", "张三")], hour=10, group_name="调试群")
        self.assertEqual(plan, ("欢迎 @张三 加入调试群！", ["wxid_a"]))

    def test_multiple_names_joined(self):
        c = cfg(enabled=True, mode="all")
        plan = jw.build(
            c, ROOM, [("wxid_a", "张三"), ("wxid_b", "李四")], hour=10, group_name="调试群"
        )
        self.assertEqual(plan[0], "欢迎 @张三、李四 加入调试群！")
        self.assertEqual(plan[1], ["wxid_a", "wxid_b"])

    def test_group_name_fallback(self):
        c = cfg(enabled=True, mode="all")
        plan = jw.build(c, ROOM, [("wxid_a", "张三")], hour=10)
        self.assertIn("加入本群", plan[0])

    def test_quiet_hour(self):
        c = cfg(enabled=True, mode="all", quietHours=[23])
        self.assertIsNone(jw.build(c, ROOM, [("wxid_a", "张三")], hour=23, group_name="群"))
        self.assertIsNotNone(jw.build(c, ROOM, [("wxid_a", "张三")], hour=22, group_name="群"))

    def test_max_batch(self):
        c = cfg(enabled=True, mode="all", maxBatch=2)
        added = [("wxid_a", "a"), ("wxid_b", "b"), ("wxid_c", "c")]
        self.assertIsNone(jw.build(c, ROOM, added, hour=10, group_name="群"))

    def test_mention_off(self):
        c = cfg(enabled=True, mode="all", mention=False)
        plan = jw.build(c, ROOM, [("wxid_a", "张三")], hour=10, group_name="群")
        self.assertEqual(plan[1], [])

    def test_empty_added(self):
        c = cfg(enabled=True, mode="all")
        self.assertIsNone(jw.build(c, ROOM, [], hour=10))


class TestDiffAndState(unittest.TestCase):
    def test_diff_members(self):
        baseline = {"wxid_a": {"nick": "张三", "first_seen": 1}}
        current = {"wxid_a": "张三改", "wxid_b": "李四"}
        added, removed = jw.diff_members(baseline, current)
        self.assertEqual(added, [("wxid_b", "李四")])
        self.assertEqual(removed, [])

    def test_diff_members_removed(self):
        baseline = {"wxid_a": {"nick": "张三"}, "wxid_b": {"nick": "李四"}}
        added, removed = jw.diff_members(baseline, {"wxid_a": "张三"})
        self.assertEqual(added, [])
        self.assertEqual(removed, ["wxid_b"])

    def test_normalize_corrupt_state(self):
        for bad in (None, [], "x", {"chatrooms": "bad"}):
            self.assertEqual(jw.normalize_state(bad)["chatrooms"], {})

    def test_normalize_state_roundtrip(self):
        state = {
            "version": 1,
            "updated_at": 123,
            "chatrooms": {
                ROOM: {
                    "name": "调试群",
                    "synced_at": 99,
                    "members": {
                        "wxid_a": {"nick": "张三", "first_seen": 5},
                        "wxid_b": "李四",  # 旧格式纯字符串也要兼容
                    },
                }
            },
        }
        norm = jw.normalize_state(state)
        self.assertEqual(norm["updated_at"], 123)
        group = norm["chatrooms"][ROOM]
        self.assertEqual(group["name"], "调试群")
        self.assertEqual(group["members"]["wxid_a"], {"nick": "张三", "first_seen": 5})
        self.assertEqual(group["members"]["wxid_b"]["nick"], "李四")


if __name__ == "__main__":
    unittest.main()
