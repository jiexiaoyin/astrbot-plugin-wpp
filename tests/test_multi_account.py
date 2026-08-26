"""多账号架构测试 (v0.2.0 单实例多账号重构)。

验证核心多账号逻辑, 不依赖真实 astrbot 运行时:
  1. _parse_accounts: wpp_accounts JSON 多账号解析
  2. _parse_accounts: 单账号回退 (wpp_auth_token)
  3. WppAccount 白名单按账号隔离到 accounts/<id>/
  4. WppAccount meta().id 实例化 (平台级)

在 AstrBot 容器内运行 (有 astrbot 依赖):
  docker exec astrbot python3 -m pytest /AstrBot/data/plugins/astrbot-plugin-wpp/tests/test_multi_account.py -v
"""

import importlib
import json
import sys
import unittest
from pathlib import Path

# 插件目录
PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(PLUGIN_DIR.parent))

mod = importlib.import_module("astrbot-plugin-wpp.wpp_adapter")
WppPlatformAdapter = mod.WppPlatformAdapter
_parse_accounts = mod._parse_accounts
WppAccount = importlib.import_module("astrbot-plugin-wpp.wpp_account").WppAccount


class FakePlatform:
    """假平台 (供 WppAccount 引用, 不触网)。"""
    def __init__(self, instance_id="wpp"):
        self.instance_id = instance_id
        self.committed = []

    def meta(self):
        from astrbot.api.platform import PlatformMetadata
        return PlatformMetadata(name="wpp", description="t", id=self.instance_id)

    def commit_event(self, event):
        self.committed.append(event)


class TestParseAccounts(unittest.TestCase):
    def test_single_account_fallback(self):
        """单账号回退: wpp_auth_token + 顶层白名单。"""
        cfg = {
            "id": "wpp",
            "wpp_base_url": "http://127.0.0.1:18062",
            "wpp_auth_token": "token_xieyin",
            "wpp_allow_users": "u1,u2",
            "wpp_group_reply": "atbot",
            "wpp_ws_url": "wss://x/ws/sync",
            "wpp_accounts": "",
        }
        accounts = _parse_accounts(cfg)
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0]["id"], "wpp")
        self.assertEqual(accounts[0]["authcode"], "token_xieyin")
        self.assertEqual(accounts[0]["allow_users"], "u1,u2")

    def test_multi_account_parse(self):
        """多账号: wpp_accounts JSON 解析。"""
        cfg = {
            "id": "wpp",
            "wpp_base_url": "http://127.0.0.1:28062",
            "wpp_auth_token": "",
            "wpp_ws_url": "wss://x/ws/sync",
            "wpp_accounts": json.dumps([
                {"id": "xieyin", "authcode": "tok1", "allow_users": "a1", "group_reply": "all"},
                {"id": "yirong", "authcode": "tok2", "blacklist_groups": "g1"},
            ]),
        }
        accounts = _parse_accounts(cfg)
        self.assertEqual(len(accounts), 2)
        self.assertEqual(accounts[0]["id"], "xieyin")
        self.assertEqual(accounts[0]["authcode"], "tok1")
        self.assertEqual(accounts[0]["group_reply"], "all")
        self.assertEqual(accounts[1]["id"], "yirong")
        self.assertEqual(accounts[1]["authcode"], "tok2")
        # 未配置的字段用默认
        self.assertEqual(accounts[1]["group_reply"], "atbot")

    def test_multi_account_skips_no_authcode(self):
        """多账号: 无 authcode 的账号被跳过。"""
        cfg = {
            "wpp_accounts": json.dumps([
                {"id": "a", "authcode": "tok1"},
                {"id": "b", "authcode": ""},
            ]),
        }
        accounts = _parse_accounts(cfg)
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0]["id"], "a")

    def test_bad_json_falls_back(self):
        """wpp_accounts 坏 JSON → 回退单账号。"""
        cfg = {"wpp_auth_token": "tok", "wpp_accounts": "{{{bad"}
        accounts = _parse_accounts(cfg)
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0]["authcode"], "tok")


class TestWppAccountIsolation(unittest.TestCase):
    def test_whitelist_isolated_by_account(self):
        """白名单文件按账号隔离到 accounts/<id>/。"""
        plat = FakePlatform()
        acct_a = WppAccount(plat, "acct_a", "tokA", "http://x:1", "wss://x/ws")
        acct_b = WppAccount(plat, "acct_b", "tokB", "http://x:1", "wss://x/ws")
        path_a = acct_a._get_whitelist_file_path()
        path_b = acct_b._get_whitelist_file_path()
        self.assertIn("accounts", path_a)
        self.assertIn("acct_a", path_a)
        self.assertIn("acct_b", path_b)
        self.assertNotEqual(path_a, path_b)

    def test_accounts_independent_state(self):
        """两个账号白名单互相独立。"""
        plat = FakePlatform()
        acct_a = WppAccount(plat, "a", "tokA", "http://x:1", "wss://x/ws")
        acct_b = WppAccount(plat, "b", "tokB", "http://x:1", "wss://x/ws")
        acct_a.allow_users.add("friendA")
        acct_b.allow_users.add("friendB")
        self.assertIn("friendA", acct_a.allow_users)
        self.assertNotIn("friendA", acct_b.allow_users)
        self.assertIn("friendB", acct_b.allow_users)

    def test_platform_meta_id(self):
        """平台 meta().id 为实例 id。"""
        plat = FakePlatform(instance_id="wpp_custom")
        self.assertEqual(plat.meta().id, "wpp_custom")


if __name__ == "__main__":
    unittest.main()
