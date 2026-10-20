"""v0.3.0 核心纯函数 / 持久化测试。

覆盖:
  1. wpp_messages: vendor WS 事件过滤、消息解析、@/引用/图片元数据
  2. wpp_policy: 白名单 / 群回复模式 / require_at_mention
  3. wpp_send: markdown-aware 长文本分片
  4. wpp_storage: 去重环形缓冲落盘 + 旧数据目录迁移

在 AstrBot 容器内运行 (有 astrbot 依赖):
  docker exec astrbot python3 -m pytest /AstrBot/data/plugins/astrbot-plugin-wpp/tests -v
"""

import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(PLUGIN_DIR.parent))

msgs = importlib.import_module("astrbot-plugin-wpp.wpp_messages")
policy = importlib.import_module("astrbot-plugin-wpp.wpp_policy")
sender = importlib.import_module("astrbot-plugin-wpp.wpp_send")
storage = importlib.import_module("astrbot-plugin-wpp.wpp_storage")


def ws_payload(events, etype="sync_message"):
    return {"Data": {"type": etype, "data": {"messages": events}}}


class TestMessageEventGate(unittest.TestCase):
    def test_friend_circle_update_is_not_message(self):
        """朋友圈推送同样带 items, 不能被当成消息处理 (旧版日志刷屏根因)。"""
        payload = ws_payload([], etype="friend_circle_update")
        payload["Data"]["data"]["items"] = [{"fromUser": "wxid_x", "content": "hi"}]
        self.assertFalse(msgs.is_message_payload(payload))
        self.assertEqual(msgs.extract_all_msg_src(payload), [])

    def test_sync_message_is_message(self):
        payload = ws_payload([{"sender_id": "wxid_x", "content": "hi"}])
        self.assertTrue(msgs.is_message_payload(payload))
        self.assertEqual(len(msgs.extract_all_msg_src(payload)), 1)

    def test_legacy_webhook_addmsgs(self):
        payload = {
            "EventType": "sync_message",
            "Data": {"Data": {"AddMsgs": [{"fromUser": "wxid_x", "Content": "hi"}]}},
        }
        self.assertTrue(msgs.is_message_payload(payload))
        self.assertEqual(len(msgs.extract_all_msg_src(payload)), 1)

    def test_flat_payload(self):
        payload = {"fromUser": "wxid_x", "Content": "hi"}
        self.assertTrue(msgs.is_message_payload(payload))
        self.assertEqual(msgs.extract_all_msg_src(payload)[0]["fromUser"], "wxid_x")


class TestOtherMessageTypes(unittest.TestCase):
    def test_emoji_becomes_placeholder_text(self):
        src = {
            "sender_id": "wxid_f",
            "msg_id": "emoji1",
            "msg_type": 47,
            "content": "<msg><emoji md5=\"abc\" len=\"123\"/></msg>",
        }
        abm = msgs.to_astrbot_message(src, "wxid_bot", "小助手")
        self.assertIsNotNone(abm)
        self.assertTrue(abm.message_str)  # 至少是 [表情], 不让消息链为空

    def test_friend_request_is_silent(self):
        src = {"sender_id": "fmessage", "msg_id": "fr1", "content": "请求添加你为好友"}
        self.assertEqual(msgs.skip_reason(src, "wxid_bot"), "friend_request")
        self.assertIsNone(msgs.to_astrbot_message(src, "wxid_bot", "小助手"))

    def test_group_invite_sysmsg_is_silent(self):
        src = {
            "sender_id": "wxid_inviter",
            "chatroom_id": "123@chatroom",
            "msg_id": "gi1",
            "msg_type": 10000,
            "content": '<sysmsg type="groupinvite">邀请你加入群聊</sysmsg>',
        }
        self.assertEqual(msgs.skip_reason(src, "wxid_bot"), "system_msg")

    def test_reaction_sysmsg_is_silent(self):
        src = {
            "sender_id": "wxid_f",
            "msg_id": "re1",
            "content": '<sysmsg type="pat">拍了拍我</sysmsg>',
        }
        self.assertEqual(msgs.skip_reason(src, "wxid_bot"), "sysmsg")


class TestToAstrBotMessage(unittest.TestCase):
    def test_private_text(self):
        src = {"sender_id": "wxid_friend", "content": "你好", "msg_id": "m1"}
        abm = msgs.to_astrbot_message(src, "wxid_bot", "小助手")
        self.assertIsNotNone(abm)
        self.assertEqual(abm.session_id, "wxid_friend")
        self.assertEqual(abm.message_str, "你好")
        self.assertEqual(abm.self_id, "wxid_bot")

    def test_group_profile_fills_name_owner_and_members(self):
        src = {
            "sender_id": "wxid_friend",
            "content": "大家好",
            "chatroom_id": "123@chatroom",
            "msg_id": "m-profile",
        }
        profile = {
            "name": "调试群",
            "owner": "wxid_owner",
            "avatar": "https://example.com/g.png",
            "members": {"wxid_friend": "张三", "wxid_owner": "群主"},
            "member_count": 2,
        }
        abm = msgs.to_astrbot_message(
            src, "wxid_bot", "小助手", group_profile=profile
        )
        self.assertIsNotNone(abm)
        self.assertEqual(abm.group.group_name, "调试群")
        self.assertEqual(abm.group.group_owner, "wxid_owner")
        self.assertEqual(abm.group.group_avatar, "https://example.com/g.png")
        self.assertEqual(abm.group.member_count, 2)
        self.assertEqual(
            {m.user_id: m.nickname for m in abm.group.members},
            {"wxid_friend": "张三", "wxid_owner": "群主"},
        )

    def test_sender_nickname_fallback_from_prefetch(self):
        src = {
            "sender_id": "wxid_friend",
            "content": "你好",
            "chatroom_id": "123@chatroom",
            "msg_id": "m-nick",
        }
        abm = msgs.to_astrbot_message(
            src, "wxid_bot", "小助手", sender_nickname="张三"
        )
        self.assertIsNotNone(abm)
        self.assertEqual(abm.sender.nickname, "张三")

    def test_group_text_at_bot(self):
        src = {
            "sender_id": "wxid_friend",
            "content": "@小助手 你好",
            "chatroom_id": "123@chatroom",
            "msg_id": "m2",
        }
        abm = msgs.to_astrbot_message(src, "wxid_bot", "小助手")
        self.assertIsNotNone(abm)
        self.assertEqual(abm.session_id, "123@chatroom")
        self.assertEqual(type(abm.message[0]).__name__, "At")
        self.assertEqual(abm.message[0].qq, "wxid_bot")

    def test_group_text_at_other_member_no_at_component(self):
        src = {
            "sender_id": "wxid_friend",
            "content": "@张三 你好",
            "chatroom_id": "123@chatroom",
            "msg_id": "m3",
        }
        abm = msgs.to_astrbot_message(src, "wxid_bot", "小助手")
        self.assertIsNotNone(abm)
        self.assertFalse(any(type(c).__name__ == "At" for c in abm.message))

    def test_outgoing_and_self_skipped(self):
        self.assertIsNone(msgs.to_astrbot_message(
            {"sender_id": "wxid_bot", "content": "x", "direction": "outgoing"},
            "wxid_bot", "小助手",
        ))
        self.assertIsNone(msgs.to_astrbot_message(
            {"sender_id": "wxid_bot", "content": "x"},
            "wxid_bot", "小助手",
        ))

    def test_revoke_skipped(self):
        self.assertIsNone(msgs.to_astrbot_message(
            {"sender_id": "wxid_f", "content": "", "msg_type": 10002}
        ))

    def test_group_image_meta_uses_chatroom(self):
        src = {
            "sender_id": "wxid_friend",
            "msg_type": 3,
            "chatroom_id": "123@chatroom",
            "local_id": "img_1",
            "image": {"data_len": 10, "cdn_download_contexts": [
                {"file_aes_key": "k", "file_no": "n"}
            ]},
        }
        abm = msgs.to_astrbot_message(src, "wxid_bot", "小助手")
        self.assertEqual(abm._wpp_image_meta["to_wxid"], "123@chatroom")
        self.assertEqual(abm._wpp_image_meta["cdn"], {"file_aes_key": "k", "file_no": "n"})

    def test_image_keeps_placeholder_plain_for_lazy_media(self):
        """未 @ 图片懒加载时消息链不能为空, 否则内置主动回复插件会跳过。"""
        src = {
            "sender_id": "wxid_friend",
            "msg_type": 3,
            "chatroom_id": "123@chatroom",
            "local_id": "img_2",
            "image": {"data_len": 10, "cdn_download_contexts": [
                {"file_aes_key": "k", "file_no": "n"}
            ]},
        }
        abm = msgs.to_astrbot_message(src, "wxid_bot", "小助手")
        self.assertEqual(len(abm.message), 1)
        self.assertEqual(type(abm.message[0]).__name__, "Plain")
        self.assertEqual(abm.message_str, "[图片]")

    def test_reply_context_creates_reply_component(self):
        src = {
            "sender_id": "wxid_friend",
            "content": "回复",
            "msg_id": "m4",
            "reply_context": {
                "quote_content": "被引用的话",
                "from_user_id": "wxid_other",
                "new_msg_id": "q1",
            },
        }
        abm = msgs.to_astrbot_message(src, "wxid_bot", "小助手")
        self.assertEqual(type(abm.message[0]).__name__, "Reply")
        self.assertEqual(abm.message[0].message_str, "被引用的话")

    def test_official_and_service_accounts_skipped(self):
        """公众号 / 微信团队 / 微信支付 / 服务通知等系统会话: 任何模式都不进 AstrBot。"""
        cases = {
            "gh_abc123": "official_account",
            "weixin": "system:weixin",
            "wxpay": "system:wxpay",
            "weixinpay": "system:weixinpay",
            "tenpay": "system:tenpay",
            "notifymessage": "system:notifymessage",
            "weixinsrc": "system:weixinsrc",
            "filehelper": "system:filehelper",
        }
        for sender, reason in cases.items():
            src = {"sender_id": sender, "content": "通知内容", "direction": "incoming"}
            self.assertEqual(msgs.skip_reason(src, "wxid_bot"), reason)
            self.assertIsNone(msgs.to_astrbot_message(src, "wxid_bot", "小助手"))

    def test_service_notice_with_empty_sender_uses_conversation(self):
        """服务通知的 sender 可能为空, 但 conversation_id 是 notifymessage。"""
        src = {
            "sender_id": "",
            "recipient_id": "wxid_bot",
            "conversation_id": "notifymessage",
            "content": "服务通知",
        }
        self.assertEqual(msgs.skip_reason(src, "wxid_bot"), "system:notifymessage")
        self.assertIsNone(msgs.to_astrbot_message(src, "wxid_bot", "小助手"))

    def test_system_and_status_messages_skipped(self):
        """群系统消息 / 状态推送 / 撤回 / sysmsg XML 一律静默。"""
        cases = [
            ({"sender_id": "wxid_f", "msg_type": 10000}, "system_msg"),
            ({"sender_id": "wxid_f", "msg_type": 10002}, "revoke"),
            ({"sender_id": "wxid_f", "msg_type": 51}, "status"),
            ({"sender_id": "wxid_f", "kind": "status"}, "kind:status"),
            ({"sender_id": "wxid_f", "content": '<sysmsg type="revokemsg">x</sysmsg>'}, "sysmsg"),
        ]
        for src, reason in cases:
            self.assertEqual(msgs.skip_reason(src, "wxid_bot"), reason)
            self.assertIsNone(msgs.to_astrbot_message(src, "wxid_bot", "小助手"))

    def test_payment_card_skipped_but_plain_red_packet_text_kept(self):
        """红包/转账卡片静默; 纯文本里的"红包"二字不能误杀 (接龙事故教训)。"""
        card = {
            "sender_id": "wxid_friend",
            "msg_type": 49,
            "content": "微信红包",
            "app": {"category": "payment_notice"},
        }
        self.assertTrue(msgs.is_payment_card(card))
        self.assertEqual(msgs.skip_reason(card, "wxid_bot"), "payment_card")

        xml_card = {
            "sender_id": "wxid_friend",
            "msg_type": 49,
            "content": "<msg><appmsg><type>2001</type></appmsg></msg>",
        }
        self.assertEqual(msgs.skip_reason(xml_card, "wxid_bot"), "payment_card")

        transfer = {"sender_id": "wxid_friend", "msg_type": 49, "content": "[转账]"}
        self.assertEqual(msgs.skip_reason(transfer, "wxid_bot"), "payment_card")

        plain = {"sender_id": "wxid_friend", "msg_type": 1, "content": "提升业绩，领取红包🧧"}
        self.assertFalse(msgs.is_payment_card(plain))
        self.assertIsNone(msgs.skip_reason(plain, "wxid_bot"))
        self.assertIsNotNone(msgs.to_astrbot_message(plain, "wxid_bot", "小助手"))

    def test_file_meta(self):
        src = {
            "sender_id": "wxid_friend",
            "msg_type": 6,
            "content": "a.xlsx",
            "file": {"download_context": {
                "attach_id": "at1", "user_name": "u", "data_len": 5
            }},
        }
        abm = msgs.to_astrbot_message(src, "wxid_bot", "")
        self.assertEqual(abm._wpp_file_meta["attach_id"], "at1")
        self.assertEqual(abm._wpp_file_meta["filename"], "a.xlsx")

    def test_message_id_prefers_new_msg_id_for_quote(self):
        """引用回复 refermsg.svrid 用 NewMsgId, 不能用本地 msg_id。"""
        src = {
            "sender_id": "wxid_friend",
            "msg_id": "local-1",
            "new_msg_id": "900123456789",
            "content": "你好",
        }
        abm = msgs.to_astrbot_message(src, "wxid_bot", "")
        self.assertEqual(abm.message_id, "900123456789")
        self.assertEqual(msgs.src_svr_id(src), "900123456789")

    def test_message_id_falls_back_to_msg_id(self):
        src = {"sender_id": "wxid_friend", "msg_id": "local-1", "content": "你好"}
        self.assertEqual(msgs.src_svr_id(src), "local-1")


class TestPolicy(unittest.TestCase):
    def test_group_blacklist_denied(self):
        p = policy.PolicySnapshot(blacklist_groups={"g@chatroom"}, group_reply="all")
        self.assertFalse(policy.is_allowed(p, "u", True, "g@chatroom", "hi"))

    def test_group_whitelist_denied(self):
        p = policy.PolicySnapshot(allow_users={"other@chatroom"}, group_reply="all")
        self.assertFalse(policy.is_allowed(p, "u", True, "g@chatroom", "hi"))

    def test_require_at_requires_bot_mention(self):
        p = policy.PolicySnapshot(
            group_reply="all", require_at_mention=True,
            self_wxid="wxid_bot", self_nickname="小助手",
        )
        # @ 了别人不算
        self.assertFalse(policy.is_allowed(p, "u", True, "g@chatroom", "@张三 hi", "wxid_bot"))
        # @ 机器人算
        self.assertTrue(policy.is_allowed(p, "u", True, "g@chatroom", "@小助手 hi", "wxid_bot"))

    def test_atbot_mode(self):
        p = policy.PolicySnapshot(
            group_reply="atbot", self_wxid="wxid_bot", self_nickname="小助手"
        )
        self.assertFalse(policy.is_allowed(p, "u", True, "g@chatroom", "hi"))
        self.assertTrue(policy.is_allowed(p, "u", True, "g@chatroom", "@小助手 hi"))

    def test_private_whitelist(self):
        self.assertTrue(policy.is_allowed(policy.PolicySnapshot(), "u", False, "", "hi"))
        p = policy.PolicySnapshot(allow_users={"u1"})
        self.assertTrue(policy.is_allowed(p, "u1", False, "", "hi"))
        self.assertFalse(policy.is_allowed(p, "u2", False, "", "hi"))

    def test_block_users_wins_in_all_pass_mode(self):
        """全部放行模式 (空 allow_users) 下, block_users 仍然拒绝。"""
        p = policy.PolicySnapshot(block_users={"wxid_bad"})
        self.assertFalse(policy.is_allowed(p, "wxid_bad", False, "", "hi"))
        self.assertTrue(policy.is_allowed(p, "wxid_ok", False, "", "hi"))

    def test_block_users_also_applies_in_group(self):
        p = policy.PolicySnapshot(block_users={"wxid_bad"}, group_reply="all")
        self.assertFalse(policy.is_allowed(p, "wxid_bad", True, "g@chatroom", "hi"))
        self.assertTrue(policy.is_allowed(p, "wxid_ok", True, "g@chatroom", "hi"))


class TestChunkText(unittest.TestCase):
    def test_short_text_single_chunk(self):
        self.assertEqual(sender.chunk_text("hello"), ["hello"])

    def test_long_text_split_and_lossless(self):
        text = ("段落一。" * 100 + "\n\n") * 20
        chunks = sender.chunk_text(text, limit=200)
        self.assertTrue(len(chunks) > 1)
        self.assertTrue(all(len(c) <= 200 for c in chunks))
        # 去掉分片处补的空行后内容不丢
        self.assertEqual(
            "".join(c.replace("\n\n", "") for c in chunks).replace("\n", ""),
            text.replace("\n\n", "").replace("\n", ""),
        )

    def test_hard_split_preserves_emoji(self):
        text = "😀" * 50
        chunks = sender.chunk_text(text, limit=10)
        self.assertEqual("".join(chunks), text)

    def test_code_block_not_split_when_small(self):
        text = "前文\n\n```python\nprint(1)\nprint(2)\n```\n\n后文"
        self.assertEqual(sender.chunk_text(text, limit=100), [text])


class TestStorage(unittest.TestCase):
    def test_dedup_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.object(storage, "plugin_data_dir", return_value=Path(d)):
                store = storage.AccountStore("acct_test")
                store.save_dedup(["a", "b", "c"])
                self.assertEqual(store.load_dedup(), ["a", "b", "c"])

    def test_config_atomic_write(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.object(storage, "plugin_data_dir", return_value=Path(d)):
                store = storage.AccountStore("acct_test")
                store.save_config({"allow_users": ["u1"], "group_reply": "all"})
                raw = json.loads(store.config_path.read_text(encoding="utf-8"))
                self.assertEqual(raw["group_reply"], "all")

    def test_legacy_config_migrated(self):
        with tempfile.TemporaryDirectory() as d:
            legacy = Path(d) / "legacy"
            legacy_acct = legacy / "acct_old"
            legacy_acct.mkdir(parents=True)
            (legacy_acct / "wpp_whitelist.json").write_text(
                json.dumps({"allow_users": ["u1"], "group_reply": "none"}),
                encoding="utf-8",
            )
            with patch.object(storage, "plugin_data_dir", return_value=Path(d) / "new"), \
                 patch.object(storage, "legacy_accounts_dir", return_value=legacy):
                store = storage.AccountStore("acct_old")
                cfg = store.load_config()
                self.assertEqual(cfg["allow_users"], ["u1"])
                self.assertTrue(store.config_path.exists())

    def test_members_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.object(storage, "plugin_data_dir", return_value=Path(d)):
                store = storage.AccountStore("acct_members")
                state = {
                    "version": 1,
                    "updated_at": 10,
                    "chatrooms": {
                        "1@chatroom": {
                            "name": "群",
                            "synced_at": 5,
                            "members": {"wxid_a": {"nick": "张三", "first_seen": 5}},
                        }
                    },
                }
                store.save_members(state)
                loaded = store.load_members()
                self.assertEqual(
                    loaded["chatrooms"]["1@chatroom"]["members"]["wxid_a"]["nick"], "张三"
                )

    def test_members_corrupt_file_falls_back(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.object(storage, "plugin_data_dir", return_value=Path(d)):
                store = storage.AccountStore("acct_bad")
                store.members_path.write_text("{not json", encoding="utf-8")
                self.assertEqual(store.load_members()["chatrooms"], {})

    def test_safe_account_id(self):
        self.assertEqual(storage.safe_account_id("../evil/x"), "evil_x")
        self.assertEqual(storage.safe_account_id(""), "account")


if __name__ == "__main__":
    unittest.main()
