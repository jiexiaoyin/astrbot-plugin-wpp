"""v0.7.0: 群资料预取 / 出站节流 / 复读保护 / 发送统计。"""

import asyncio
import importlib
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(PLUGIN_DIR.parent))

client_mod = importlib.import_module("astrbot-plugin-wpp.wpp_client")


class TestGroupProfile(unittest.IsolatedAsyncioTestCase):
    def _client(self):
        return client_mod.WppClient("http://127.0.0.1:18062", "token")

    async def test_prefetch_group_name_owner_and_members(self):
        client = self._client()
        info = {
            "Code": 0,
            "Data": {
                "NickName": "调试群",
                "ChatRoomOwner": "wxid_owner",
                "SmallHeadImgUrl": "https://example.com/g.png",
                "MemberCount": 2,
            },
        }
        members = {
            "Code": 0,
            "Data": {
                "NewChatroomData": {
                    "ChatRoomMember": [
                        {"UserName": "wxid_a", "NickName": "张三"},
                        {"UserName": "wxid_b", "NickName": {"string": "李四"}},
                    ]
                }
            },
        }
        with patch.object(
            client, "_post", AsyncMock(side_effect=[info, members])
        ) as post:
            profile = await client.get_group_profile("9@chatroom")
            self.assertEqual(profile["name"], "调试群")
            self.assertEqual(profile["owner"], "wxid_owner")
            self.assertEqual(profile["members"], {"wxid_a": "张三", "wxid_b": "李四"})
            self.assertEqual(profile["member_count"], 2)
            # 第二次走缓存, 不再请求 vendor
            self.assertEqual(await client.get_group_profile("9@chatroom"), profile)
            self.assertEqual(post.await_count, 2)

    async def test_prefetched_nickname_is_used_by_resolve_nickname(self):
        client = self._client()
        info = {"Code": 0, "Data": {"NickName": "群"}}
        members = {
            "Code": 0,
            "Data": {
                "NewChatroomData": {
                    "ChatRoomMember": [{"UserName": "wxid_a", "NickName": "张三"}]
                }
            },
        }
        with patch.object(client, "_post", AsyncMock(side_effect=[info, members])):
            await client.get_group_profile("9@chatroom")
        with patch.object(client, "_post", AsyncMock()) as post:
            self.assertEqual(await client.resolve_nickname("wxid_a", "9@chatroom"), "张三")
            post.assert_not_awaited()

    async def test_prefetch_failure_is_negative_cached(self):
        client = self._client()
        with patch.object(
            client, "_post", AsyncMock(side_effect=RuntimeError("boom"))
        ) as post:
            profile = await client.get_group_profile("9@chatroom")
            self.assertEqual(profile["name"], "")
            self.assertEqual(profile["members"], {})
            await client.get_group_profile("9@chatroom")
            self.assertEqual(post.await_count, 2)  # 群信息 + 成员, 第二次走负缓存

    async def test_partial_group_profile_uses_short_cache(self):
        """群信息成功但成员查询失败: 不能按完整资料缓存 30 分钟。"""
        client = self._client()
        info = {"Code": 0, "Data": {"NickName": "调试群", "MemberCount": 2}}
        with patch.object(
            client,
            "_post",
            AsyncMock(side_effect=[info, RuntimeError("members down")]),
        ):
            profile = await client.get_group_profile("9@chatroom")
        self.assertEqual(profile["name"], "调试群")
        self.assertEqual(profile["members"], {})
        _, _, ok = client._group_cache["9@chatroom"]
        self.assertFalse(ok)


class TestGroupSnapshot(unittest.IsolatedAsyncioTestCase):
    """v0.9.0: 进群欢迎用的无缓存成员快照。"""

    def _client(self):
        return client_mod.WppClient("http://127.0.0.1:18062", "token")

    @staticmethod
    def _info():
        return {"Code": 0, "Data": {"NickName": "调试群"}}

    @staticmethod
    def _members(items):
        return {"Code": 0, "Data": {"NewChatroomData": {"ChatRoomMember": items}}}

    async def test_snapshot_filters_non_humans_and_skips_cache(self):
        client = self._client()
        members = self._members([
            {"UserName": "wxid_a", "NickName": "张三"},
            {"UserName": "123@chatroom", "NickName": "别的群"},
            {"UserName": "gh_news", "NickName": "公众号"},
            {"UserName": "weixin", "NickName": "微信团队"},
        ])
        with patch.object(
            client, "_post", AsyncMock(side_effect=[self._info(), members])
        ) as post:
            snap = await client.fetch_group_snapshot("9@chatroom")
        self.assertEqual(snap["name"], "调试群")
        self.assertEqual(snap["members"], {"wxid_a": "张三"})
        self.assertEqual(snap["member_count"], 1)
        # 不写 30 分钟群资料缓存
        self.assertEqual(client._group_cache, {})
        # 第二次仍强拉, 不复用缓存
        with patch.object(
            client, "_post", AsyncMock(side_effect=[self._info(), members])
        ) as post2:
            await client.fetch_group_snapshot("9@chatroom")
        self.assertEqual(post2.await_count, 2)
        self.assertEqual(post.await_count, 2)

    async def test_empty_members_is_failure(self):
        client = self._client()
        with patch.object(
            client, "_post", AsyncMock(side_effect=[self._info(), self._members([])])
        ):
            self.assertIsNone(await client.fetch_group_snapshot("9@chatroom"))

    async def test_vendor_failure_returns_none(self):
        client = self._client()
        with patch.object(
            client, "_post", AsyncMock(side_effect=[RuntimeError("info"), RuntimeError("down")])
        ):
            self.assertIsNone(await client.fetch_group_snapshot("9@chatroom"))

    async def test_non_chatroom_makes_no_call(self):
        client = self._client()
        with patch.object(client, "_post", AsyncMock()) as post:
            self.assertIsNone(await client.fetch_group_snapshot("wxid_friend"))
        post.assert_not_awaited()


class TestOutboundGuard(unittest.IsolatedAsyncioTestCase):
    async def test_send_interval_serializes_same_session(self):
        client = client_mod.WppClient(
            "http://127.0.0.1:18062", "token", send_interval=0.05
        )
        calls = []

        async def fake_post(endpoint, body, *a, **k):
            calls.append(time.monotonic())
            return {"Code": 0}

        with patch.object(client, "_post_impl", fake_post):
            await asyncio.gather(
                client.send_text("wxid_a", "一"),
                client.send_text("wxid_a", "二"),
            )
        self.assertEqual(len(calls), 2)
        self.assertGreaterEqual(calls[1] - calls[0], 0.04)
        self.assertEqual(client.send_stats["sent"], 2)
        self.assertEqual(client.send_stats["failed"], 0)

    async def test_send_failure_recorded(self):
        client = client_mod.WppClient("http://127.0.0.1:18062", "token")
        with patch.object(
            client, "_post_impl", AsyncMock(side_effect=RuntimeError("down"))
        ):
            with self.assertRaises(RuntimeError):
                await client.send_text("wxid_a", "你好")
        self.assertEqual(client.send_stats["failed"], 1)
        self.assertIn("down", client.send_stats["last_error"])

    async def test_repeat_guard_allows_limit_then_skips(self):
        client = client_mod.WppClient(
            "http://127.0.0.1:18062",
            "token",
            repeat_guard_window=300,
            repeat_guard_limit=2,
        )
        with patch.object(client, "_post_impl", AsyncMock(return_value={"Code": 0})) as post:
            await client.send_text("wxid_a", "收到")
            await client.send_text("wxid_a", "收到")
            third = await client.send_text("wxid_a", "收到")
        self.assertEqual(post.await_count, 2)
        self.assertTrue(third.get("Skipped"))

    async def test_bypass_repeat_guard_for_welcome(self):
        """欢迎语不受复读保护: 三次同文都要真发, 且 At 正确带入。"""
        client = client_mod.WppClient(
            "http://127.0.0.1:18062",
            "token",
            repeat_guard_window=300,
            repeat_guard_limit=1,
        )
        bodies = []

        async def fake_post(endpoint, body, *a, **k):
            bodies.append(body)
            return {"Code": 0}

        with patch.object(client, "_post_impl", fake_post):
            for _ in range(3):
                await client.send_text(
                    "9@chatroom", "欢迎 @张三 加入本群！", ["wxid_a"],
                    bypass_repeat_guard=True,
                )
        self.assertEqual(len(bodies), 3)
        self.assertEqual(bodies[0]["At"], "wxid_a")

    async def test_repeat_guard_scoped_by_session(self):
        client = client_mod.WppClient(
            "http://127.0.0.1:18062",
            "token",
            repeat_guard_window=300,
            repeat_guard_limit=1,
        )
        with patch.object(client, "_post_impl", AsyncMock(return_value={"Code": 0})):
            await client.send_text("wxid_a", "收到")
            await client.send_text("wxid_b", "收到")
        self.assertEqual(client.send_stats["sent"], 2)


if __name__ == "__main__":
    unittest.main()
