"""v0.6.2 出站昵称解析: 群成员列表 → 通讯录兜底 → 缓存。"""

import importlib
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(PLUGIN_DIR.parent))

client_mod = importlib.import_module("astrbot_plugin_wpp.wpp_client")


class TestResolveNickname(unittest.IsolatedAsyncioTestCase):
    def _client(self):
        return client_mod.WppClient("http://127.0.0.1:18062", "token")

    async def test_group_member_nickname(self):
        client = self._client()
        resp = {
            "Code": 0,
            "Data": {
                "NewChatroomData": {
                    "ChatRoomMember": [
                        {"UserName": "wxid_other", "NickName": "别人"},
                        {"UserName": "wxid_a", "NickName": "张三"},
                    ]
                }
            },
        }
        with patch.object(client, "_post", AsyncMock(return_value=resp)) as post:
            self.assertEqual(await client.resolve_nickname("wxid_a", "9@chatroom"), "张三")
            post.assert_awaited_once_with(
                "/Group/GetChatRoomMemberDetail", {"QID": "9@chatroom"}, max_retries=0
            )

    async def test_group_miss_falls_back_to_contact(self):
        client = self._client()
        group_resp = {"Code": 0, "Data": {"NewChatroomData": {"ChatRoomMember": []}}}
        contact_resp = {
            "Code": 0,
            "Data": {"ContactList": [{"NickName": {"string": "李四"}}]},
        }
        with patch.object(
            client, "_post", AsyncMock(side_effect=[group_resp, contact_resp])
        ) as post:
            self.assertEqual(await client.resolve_nickname("wxid_b", "9@chatroom"), "李四")
            self.assertEqual(post.await_count, 2)
            post.assert_awaited_with(
                "/Friend/GetContractDetail", {"userName": "wxid_b"}, max_retries=0
            )

    async def test_private_chat_uses_contact_only(self):
        client = self._client()
        contact_resp = {"Code": 0, "Data": {"ContactList": [{"Remark": "老主顾"}]}}
        with patch.object(client, "_post", AsyncMock(return_value=contact_resp)) as post:
            self.assertEqual(await client.resolve_nickname("wxid_c"), "老主顾")
            post.assert_awaited_once_with(
                "/Friend/GetContractDetail", {"userName": "wxid_c"}, max_retries=0
            )

    async def test_success_is_cached(self):
        client = self._client()
        resp = {
            "Code": 0,
            "Data": {
                "NewChatroomData": {"ChatRoomMember": [{"UserName": "wxid_a", "NickName": "张三"}]}
            },
        }
        with patch.object(client, "_post", AsyncMock(return_value=resp)) as post:
            self.assertEqual(await client.resolve_nickname("wxid_a", "9@chatroom"), "张三")
            self.assertEqual(await client.resolve_nickname("wxid_a", "9@chatroom"), "张三")
            self.assertEqual(post.await_count, 1)

    async def test_failure_returns_empty_and_is_negative_cached(self):
        client = self._client()
        with patch.object(client, "_post", AsyncMock(side_effect=RuntimeError("boom"))) as post:
            self.assertEqual(await client.resolve_nickname("wxid_x", "9@chatroom"), "")
            self.assertEqual(await client.resolve_nickname("wxid_x", "9@chatroom"), "")
            self.assertEqual(post.await_count, 2)  # 群查询 + 通讯录兜底, 第二次走负缓存

    async def test_at_all_is_not_resolved(self):
        client = self._client()
        with patch.object(client, "_post", AsyncMock()) as post:
            self.assertEqual(await client.resolve_nickname("all", "9@chatroom"), "")
            post.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
