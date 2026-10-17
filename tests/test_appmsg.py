"""appmsg / XML 卡片解析测试 (v0.4.0)。

覆盖:
  1. 链接/小程序/位置/名片/合并转发/接龙 的结构化摘要
  2. 空 content 卡片不再整条丢弃
  3. 原始 XML 不再直接交给 LLM
  4. 文件卡片从 app.file.download_context 取附件
  5. 支付卡片判定加固, 接龙里的"红包"不误杀
  6. sysmsg/direction/字符串消息类型/@ 列表兼容
  7. 媒体富化保留 At 组件

在 AstrBot 容器内运行:
  docker exec astrbot python3 -m pytest /AstrBot/data/plugins/astrbot-plugin-wpp/tests -v
"""

import importlib
import os
import sys
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(PLUGIN_DIR.parent))

msgs = importlib.import_module("astrbot-plugin-wpp.wpp_messages")
appmsg = importlib.import_module("astrbot-plugin-wpp.wpp_appmsg")
policy = importlib.import_module("astrbot-plugin-wpp.wpp_policy")
media = importlib.import_module("astrbot-plugin-wpp.wpp_media")


LINK_XML = (
    "<msg><appmsg><title>商品链接</title><des>限时活动</des>"
    "<url>https://example.com/p/1</url><type>5</type></appmsg></msg>"
)
LOCATION_XML = '<msg><location x="31.234" y="121.456" label="门店A" poiname="门店A"/></msg>'
CONTACT_XML = (
    "<msg><appmsg><title>张三</title><type>42</type>"
    '<msg username="wxid_zhangsan" nickname="张三" alias="zs001"/></appmsg></msg>'
)
FORWARD_XML = (
    "<msg><appmsg><title>聊天记录</title><type>19</type>"
    "<recorditem><![CDATA[<recordinfo><datalist><dataitem>"
    "<datatitle>A</datatitle><datadesc>第一句话</datadesc>"
    "<sourcename>张三</sourcename></dataitem></datalist></recordinfo>]]>"
    "</recorditem></appmsg></msg>"
)
QUOTE_XML = (
    "<msg><appmsg><title>收到</title><type>57</type><refermsg>"
    "<type>1</type><svrid>q1</svrid><fromusr>wxid_other</fromusr>"
    "<displayname>李四</displayname><content>被引用的话</content>"
    "<createtime>1700000000000</createtime></refermsg></appmsg></msg>"
)


class TestStructuredCards(unittest.TestCase):
    def test_link_card_is_structured_not_raw_xml(self):
        src = {"sender_id": "wxid_f", "msg_type": 49, "content": LINK_XML}
        abm = msgs.to_astrbot_message(src, "wxid_bot", "小助手")
        self.assertIsNotNone(abm)
        self.assertIn("商品链接", abm.message_str)
        self.assertIn("https://example.com/p/1", abm.message_str)
        self.assertNotIn("<appmsg", abm.message_str)

    def test_mini_program_structured_app_block(self):
        src = {
            "sender_id": "wxid_f",
            "msg_type": 49,
            "content": "快来领取补贴",
            "app": {
                "category": "mini_program",
                "title": "国家补贴",
                "description": "家电数码补贴",
                "mini_program": {
                    "app_id": "wx123",
                    "page_path": "pages/index",
                    "username": "gh_abc@app",
                    "source_display_name": "国补小程序",
                },
            },
        }
        abm = msgs.to_astrbot_message(src, "wxid_bot", "小助手")
        self.assertIn("[小程序]", abm.message_str)
        self.assertIn("pages/index", abm.message_str)
        self.assertIn("wx123", abm.message_str)

    def test_empty_content_card_not_dropped(self):
        src = {
            "sender_id": "wxid_f",
            "msg_type": 49,
            "content": "",
            "app": {"category": "link", "title": "标题", "url": "https://example.com"},
        }
        abm = msgs.to_astrbot_message(src, "wxid_bot", "小助手")
        self.assertIsNotNone(abm)
        self.assertIn("[链接] 标题", abm.message_str)

    def test_location_card(self):
        src = {"sender_id": "wxid_f", "msg_type": 48, "content": LOCATION_XML}
        abm = msgs.to_astrbot_message(src, "wxid_bot", "小助手")
        self.assertIn("门店A", abm.message_str)
        self.assertIn("31.234,121.456", abm.message_str)

    def test_contact_card(self):
        src = {"sender_id": "wxid_f", "msg_type": 49, "content": CONTACT_XML}
        abm = msgs.to_astrbot_message(src, "wxid_bot", "小助手")
        self.assertIn("[名片] 张三", abm.message_str)
        self.assertIn("wxid_zhangsan", abm.message_str)

    def test_merged_forward_items(self):
        src = {"sender_id": "wxid_f", "msg_type": 49, "content": FORWARD_XML}
        abm = msgs.to_astrbot_message(src, "wxid_bot", "小助手")
        self.assertIn("[合并转发]", abm.message_str)
        self.assertIn("张三", abm.message_str)
        self.assertIn("第一句话", abm.message_str)

    def test_relay_card(self):
        src = {
            "sender_id": "wxid_f",
            "msg_type": 49,
            "content": "#接龙 拼单",
            "app": {
                "category": "app_message",
                "title": "#接龙 拼单",
                "description": "1. 张三 2. 李四",
            },
        }
        abm = msgs.to_astrbot_message(src, "wxid_bot", "小助手")
        self.assertIn("[接龙]", abm.message_str)
        self.assertIn("张三", abm.message_str)

    def test_quote_xml_creates_reply_component(self):
        src = {"sender_id": "wxid_f", "msg_type": 49, "content": QUOTE_XML}
        abm = msgs.to_astrbot_message(src, "wxid_bot", "小助手")
        reply = abm.message[0]
        self.assertEqual(type(reply).__name__, "Reply")
        self.assertEqual(reply.message_str, "被引用的话")
        self.assertEqual(reply.sender_nickname, "李四")
        self.assertEqual(reply.time, 1700000000)

    def test_unknown_xml_not_passed_through(self):
        src = {
            "sender_id": "wxid_f",
            "msg_type": 9999,
            "content": "<msg><foo><bar>secret</bar></foo></msg>",
        }
        abm = msgs.to_astrbot_message(src, "wxid_bot", "小助手")
        self.assertNotIn("<msg", abm.message_str)


class TestFileCard(unittest.TestCase):
    def test_file_attach_from_app_file(self):
        src = {
            "sender_id": "wxid_f",
            "msg_type": 49,
            "kind": "app",
            "content": "",
            "app": {
                "category": "file",
                "title": "报表.xlsx",
                "file": {
                    "download_context": {
                        "attach_id": "at1",
                        "user_name": "u",
                        "data_len": 5,
                    }
                },
            },
        }
        abm = msgs.to_astrbot_message(src, "wxid_bot", "小助手")
        self.assertEqual(abm._wpp_file_meta["filename"], "报表.xlsx")
        self.assertEqual(abm._wpp_file_meta["attach_id"], "at1")
        self.assertEqual(abm.message_str, "[文件] 报表.xlsx")

    def test_file_filename_does_not_fall_back_to_raw_xml(self):
        src = {
            "sender_id": "wxid_f",
            "msg_type": 49,
            "kind": "app",
            "content": "<msg><appmsg><title>a.xlsx</title><type>6</type></appmsg></msg>",
            "app": {"category": "file"},
        }
        abm = msgs.to_astrbot_message(src, "wxid_bot", "小助手")
        self.assertEqual(abm._wpp_file_meta["filename"], "a.xlsx")
        self.assertNotIn("<appmsg", abm.message_str)


class TestPaymentHardening(unittest.TestCase):
    def test_description_only_transfer(self):
        src = {
            "sender_id": "wxid_f",
            "msg_type": 49,
            "content": "",
            "app": {"description": "微信转账"},
        }
        self.assertTrue(msgs.is_payment_card(src))
        self.assertEqual(msgs.skip_reason(src, "wxid_bot"), "payment_card")

    def test_relay_with_redpacket_text_is_not_payment(self):
        src = {
            "sender_id": "wxid_f",
            "msg_type": 49,
            "content": "#接龙 领红包",
            "app": {
                "category": "app_message",
                "title": "#接龙 领红包",
                "description": "1. 张三 2. 李四",
            },
        }
        self.assertFalse(msgs.is_payment_card(src))
        self.assertIsNotNone(msgs.to_astrbot_message(src, "wxid_bot", "小助手"))

    def test_payment_xml_type(self):
        src = {
            "sender_id": "wxid_f",
            "msg_type": 49,
            "content": "<msg><appmsg><type>2002</type></appmsg></msg>",
        }
        self.assertTrue(msgs.is_payment_card(src))

    def test_link_card_with_redpacket_title_is_kept(self):
        src = {
            "sender_id": "wxid_f",
            "msg_type": 49,
            "content": "",
            "app": {
                "category": "link",
                "title": "红包活动",
                "description": "红包活动",
                "url": "https://example.com/hb",
            },
        }
        self.assertFalse(msgs.is_payment_card(src))


class TestCompat(unittest.TestCase):
    def test_sysmsg_with_declaration_and_case(self):
        for content in (
            '﻿<?xml version="1.0"?><sysmsg type="revokemsg">x</sysmsg>',
            '<?xml version="1.0"?><SysMsg type="revokemsg">x</SysMsg>',
            "<!-- c --><sysmsg>x</sysmsg>",
        ):
            src = {"sender_id": "wxid_f", "content": content}
            self.assertEqual(msgs.skip_reason(src, "wxid_bot"), "sysmsg")

    def test_direction_variants(self):
        for direction in ("incoming", "in", "recv", "received", "1"):
            self.assertTrue(msgs.src_is_incoming({"direction": direction}))
        for direction in ("outgoing", "out", "0"):
            self.assertFalse(msgs.src_is_incoming({"direction": direction}))

    def test_string_msg_type(self):
        self.assertEqual(msgs.src_msg_type({"type": "image"}), msgs.MSG_TYPE_IMAGE)
        self.assertEqual(msgs.src_msg_type({"type": "voice"}), msgs.MSG_TYPE_VOICE)
        self.assertEqual(msgs.src_msg_type({"type": "app"}), msgs.MSG_TYPE_APP)

    def test_group_id_not_taken_from_sender_id(self):
        src = {"sender_id": "123@chatroom", "content": "hi"}
        self.assertEqual(msgs.src_chatroom(src), "")

    def test_structured_at_list_mentions_bot(self):
        src = {
            "sender_id": "wxid_f",
            "content": "@张三 hi",
            "chatroom_id": "123@chatroom",
            "at_user_list": ["wxid_bot"],
        }
        abm = msgs.to_astrbot_message(src, "wxid_bot", "小助手")
        self.assertTrue(any(type(c).__name__ == "At" for c in abm.message))

        p = policy.PolicySnapshot(
            group_reply="atbot", self_wxid="wxid_bot", self_nickname="小助手"
        )
        self.assertTrue(
            policy.is_allowed(p, "wxid_f", True, "123@chatroom", "@张三 hi", "", ["wxid_bot"])
        )

    def test_voice_without_transcript_keeps_download_context(self):
        src = {
            "sender_id": "wxid_f",
            "msg_id": "v1",
            "msg_type": 34,
            "content": "<voicemsg/>",
            "voice": {
                "download_context": {
                    "msg_id": 123,
                    "new_msg_id": "n1",
                    "client_msg_id": "c1",
                    "master_buf_id": "m1",
                    "format": 4,
                    "length": 1200,
                    "from_user_name": "wxid_f",
                    "to_user_name": "wxid_bot",
                }
            },
        }
        abm = msgs.to_astrbot_message(src, "wxid_bot", "小助手")
        meta = abm._wpp_voice_meta
        self.assertEqual(meta["msg_id"], "123")
        self.assertEqual(meta["new_msg_id"], "n1")
        self.assertEqual(meta["length"], 1200)
        self.assertEqual(meta["format"], 4)

    def test_voice_with_transcript_skips_download(self):
        src = {
            "sender_id": "wxid_f",
            "msg_id": "v2",
            "msg_type": 34,
            "voice": {"transcript": "你好", "download_context": {"msg_id": 1}},
        }
        abm = msgs.to_astrbot_message(src, "wxid_bot", "小助手")
        self.assertEqual(abm.message_str, "你好")
        self.assertFalse(hasattr(abm, "_wpp_voice_meta"))

    def test_media_prefix_preserves_at(self):
        from astrbot.api.message_components import At, Plain
        from astrbot.api.platform import AstrBotMessage

        abm = AstrBotMessage()
        abm.message = [At(qq="wxid_bot"), Plain("[图片]")]
        prefix = media._non_media_prefix(abm)
        self.assertEqual(len(prefix), 1)
        self.assertEqual(type(prefix[0]).__name__, "At")


class _FakeVoiceApi:
    def __init__(self):
        self.calls = None

    async def download_voice_binary(self, *args, **kwargs):
        self.calls = kwargs
        return b"\x02#!SILK_V3" + b"\x00" * 32


class TestVoiceEnrich(unittest.IsolatedAsyncioTestCase):
    async def test_voice_download_becomes_record(self):
        src = {
            "sender_id": "wxid_f",
            "msg_id": "v3",
            "msg_type": 34,
            "voice": {
                "download_context": {
                    "msg_id": 456,
                    "new_msg_id": "n2",
                    "length": 900,
                    "from_user_name": "wxid_f",
                    "to_user_name": "wxid_bot",
                }
            },
        }
        abm = msgs.to_astrbot_message(src, "wxid_bot", "小助手")
        api = _FakeVoiceApi()
        paths = await media.enrich_media(api, abm)
        self.assertEqual(len(paths), 1)
        self.assertTrue(paths[0].endswith(".silk"))
        self.assertTrue(os.path.exists(paths[0]))
        self.assertEqual(type(abm.message[0]).__name__, "Record")
        self.assertEqual(api.calls["voice_format"], 4)
        self.assertEqual(api.calls["to_user_name"], "wxid_bot")
        os.unlink(paths[0])


if __name__ == "__main__":
    unittest.main()
