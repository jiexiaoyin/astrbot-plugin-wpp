"""单实例单账号测试 (v0.10.0 账号模型)。

验证:
  1. _parse_account: 一个平台实例只解析一个 authcode/微信账号
  2. 旧 wpp_accounts 多账号配置不再生效（明确告警，提示拆实例）
  3. 同一 authcode 只能由一个平台实例轮询
  4. WppAccount 白名单/状态按实例 id 隔离到 accounts/<id>/

在 AstrBot 容器内运行 (有 astrbot 依赖):
  docker exec astrbot python3 -m pytest /AstrBot/data/plugins/astrbot-plugin-wpp/tests/test_single_account.py -v
"""

import asyncio
import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# 插件目录
PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(PLUGIN_DIR.parent))

mod = importlib.import_module("astrbot-plugin-wpp.wpp_adapter")
WppPlatformAdapter = mod.WppPlatformAdapter
_parse_account = mod._parse_account
authcode_fingerprint = mod.authcode_fingerprint
WppAccount = importlib.import_module("astrbot-plugin-wpp.wpp_account").WppAccount
storage = importlib.import_module("astrbot-plugin-wpp.wpp_storage")


class FakePlatform:
    """假平台 (供 WppAccount 引用, 不触网)。"""

    def __init__(self, instance_id="wpp"):
        self.instance_id = instance_id
        self.committed = []

    def meta(self):
        from astrbot.api.platform import PlatformMetadata

        return PlatformMetadata(name=mod.PLATFORM_NAME, description="t", id=self.instance_id)

    def commit_event(self, event):
        self.committed.append(event)


class TestParseAccount(unittest.TestCase):
    def test_single_account(self):
        cfg = {
            "id": "wpp-shop1",
            "wpp_base_url": "http://127.0.0.1:18062",
            "wpp_auth_token": "token_xieyin",
            "wpp_allow_users": "u1,u2",
            "wpp_block_users": "bad1,bad2",
            "wpp_group_reply": "atbot",
            "wpp_ws_url": "wss://x/ws/sync",
        }
        acct = _parse_account(cfg)
        self.assertEqual(acct["id"], "wpp-shop1")
        self.assertEqual(acct["authcode"], "token_xieyin")
        self.assertEqual(acct["allow_users"], {"u1", "u2"})
        self.assertEqual(acct["block_users"], {"bad1", "bad2"})

    def test_no_authcode_returns_none(self):
        self.assertIsNone(_parse_account({"id": "wpp", "wpp_base_url": "http://x"}))

    def test_legacy_multi_account_config_is_not_parsed(self):
        """旧 wpp_accounts 不再产生账号；没有 wpp_auth_token 就没有账号。"""
        cfg = {
            "id": "wpp",
            "wpp_accounts": json.dumps(
                [
                    {"id": "xieyin", "authcode": "tok1"},
                    {"id": "yirong", "authcode": "tok2"},
                ]
            ),
        }
        self.assertIsNone(_parse_account(cfg))

    def test_legacy_multi_account_ignored_when_single_token_present(self):
        """同时有 wpp_auth_token 和旧 wpp_accounts：只用当前实例的 token。"""
        cfg = {
            "id": "wpp-shop1",
            "wpp_auth_token": "tok_current",
            "wpp_accounts": json.dumps([{"id": "old", "authcode": "tok_old"}]),
        }
        acct = _parse_account(cfg)
        self.assertEqual(acct["id"], "wpp-shop1")
        self.assertEqual(acct["authcode"], "tok_current")


class TestAuthcodeLock(unittest.TestCase):
    def test_fingerprint_hides_authcode(self):
        fp = authcode_fingerprint("wxa_super_secret")
        self.assertEqual(len(fp), 12)
        self.assertNotIn("wxa_super_secret", fp)

    def test_same_authcode_cannot_run_twice(self):
        a = WppPlatformAdapter({"id": "wpp-a", "wpp_auth_token": "same_token"}, {}, asyncio.Queue())
        b = WppPlatformAdapter({"id": "wpp-b", "wpp_auth_token": "same_token"}, {}, asyncio.Queue())
        fp = authcode_fingerprint("same_token")
        WppPlatformAdapter._ACTIVE_AUTHCODES[fp] = a.instance_id
        try:
            with self.assertRaises(RuntimeError) as ctx:
                asyncio.run(b.run())
            self.assertIn("同一 authcode", str(ctx.exception))
        finally:
            WppPlatformAdapter._ACTIVE_AUTHCODES.pop(fp, None)

    def test_missing_authcode_fails_fast(self):
        adapter = WppPlatformAdapter({"id": "wpp-empty"}, {}, asyncio.Queue())
        self.assertEqual(adapter.accounts, [])
        with self.assertRaises(ValueError) as ctx:
            asyncio.run(adapter.run())
        self.assertIn("wpp_auth_token", str(ctx.exception))

    def test_one_adapter_exposes_one_account(self):
        adapter = WppPlatformAdapter(
            {"id": "wpp-one", "wpp_auth_token": "tok"}, {}, asyncio.Queue()
        )
        self.assertEqual(len(adapter.accounts), 1)
        self.assertEqual(adapter.accounts[0].account_id, "wpp-one")
        self.assertEqual(len(adapter.get_stats()["accounts"]), 1)

    def test_platform_metadata_name(self):
        """Dashboard 机器人列表/新增下拉直接显示技术名，这里保持中文。"""
        adapter = WppPlatformAdapter(
            {"id": "wpp-meta", "wpp_auth_token": "tok"}, {}, asyncio.Queue()
        )
        meta = adapter.meta()
        self.assertEqual(meta.name, mod.PLATFORM_NAME)
        self.assertEqual(meta.adapter_display_name, mod.PLATFORM_DISPLAY_NAME)
        self.assertEqual(meta.name, "个人微信（Pad协议）")
        self.assertEqual(meta.adapter_display_name, "个人微信（Pad协议）")

    def test_send_routing_uses_single_account(self):
        """所有会话回复都由本实例唯一的账号发送。"""
        from types import SimpleNamespace

        adapter = WppPlatformAdapter(
            {"id": "wpp-send", "wpp_auth_token": "tok"}, {}, asyncio.Queue()
        )
        session = SimpleNamespace(session_id="wxid_friend")
        self.assertIs(adapter._find_account_for_send(session), adapter.account)


class TestWppAccountIsolation(unittest.TestCase):
    def test_whitelist_path_uses_instance_id(self):
        """每个平台实例的运行时数据按实例 id 隔离。"""
        plat_a = FakePlatform("wpp-shop1")
        plat_b = FakePlatform("wpp-shop2")
        acct_a = WppAccount(plat_a, "wpp-shop1", "tokA", "http://x:1", "wss://x/ws")
        acct_b = WppAccount(plat_b, "wpp-shop2", "tokB", "http://x:1", "wss://x/ws")
        path_a = str(acct_a.store.config_path)
        path_b = str(acct_b.store.config_path)
        self.assertIn("accounts", path_a)
        self.assertIn("wpp-shop1", path_a)
        self.assertIn("wpp-shop2", path_b)
        self.assertNotEqual(path_a, path_b)

    def test_block_users_persisted(self):
        """block_users 跟随实例配置落盘, 重启/热更新后仍生效。"""
        with tempfile.TemporaryDirectory() as d:
            with patch.object(storage, "plugin_data_dir", return_value=Path(d)):
                acct = WppAccount(FakePlatform(), "wpp_shop", "tok", "http://x:1", "")
                acct.block_users.add("wxid_bad")
                acct.save_policy()
                reloaded = WppAccount(FakePlatform(), "wpp_shop", "tok", "http://x:1", "")
                self.assertIn("wxid_bad", reloaded.block_users)
                self.assertFalse(reloaded.policy_snapshot().block_users == set())

    def test_save_policy_preserves_join_welcome(self):
        """v0.9.0: /group add 等命令整份重写时不能抹掉 join_welcome 段。"""
        with tempfile.TemporaryDirectory() as d:
            with patch.object(storage, "plugin_data_dir", return_value=Path(d)):
                acct = WppAccount(FakePlatform(), "wpp_jw", "tok", "http://x:1", "")
                acct.store.save_config(
                    {
                        "allow_users": [],
                        "join_welcome": {"enabled": True, "mode": "whitelist"},
                    }
                )
                acct._load_policy_file()
                acct.block_users.add("wxid_bad")
                acct.save_policy()
                raw = json.loads(acct.store.config_path.read_text(encoding="utf-8"))
                self.assertTrue(raw["join_welcome"]["enabled"])
                self.assertEqual(raw["join_welcome"]["mode"], "whitelist")
                self.assertIn("wxid_bad", raw["block_users"])

    def test_removing_join_welcome_section_disables(self):
        """热更新时删掉 join_welcome 段 ⇒ 回到默认关闭, 不沿用旧配置。"""
        with tempfile.TemporaryDirectory() as d:
            with patch.object(storage, "plugin_data_dir", return_value=Path(d)):
                acct = WppAccount(FakePlatform(), "wpp_jw3", "tok", "http://x:1", "")
                acct.store.save_config({"join_welcome": {"enabled": True, "mode": "all"}})
                acct._load_policy_file()
                self.assertTrue(acct.join_welcome_cfg()["enabled"])
                acct.store.save_config({"allow_users": []})
                acct._load_policy_file()
                self.assertFalse(acct.join_welcome_cfg()["enabled"])

    def test_set_join_welcome_enabled_persisted(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.object(storage, "plugin_data_dir", return_value=Path(d)):
                acct = WppAccount(FakePlatform(), "wpp_jw2", "tok", "http://x:1", "")
                self.assertTrue(acct.set_join_welcome_enabled(True))
                raw = json.loads(acct.store.config_path.read_text(encoding="utf-8"))
                self.assertTrue(raw["join_welcome"]["enabled"])
                reloaded = WppAccount(FakePlatform(), "wpp_jw2", "tok", "http://x:1", "")
                self.assertTrue(reloaded.join_welcome_cfg()["enabled"])

    def test_platform_meta_id(self):
        """平台 meta().id 为实例 id。"""
        plat = FakePlatform(instance_id="wpp_custom")
        self.assertEqual(plat.meta().id, "wpp_custom")


if __name__ == "__main__":
    unittest.main()
