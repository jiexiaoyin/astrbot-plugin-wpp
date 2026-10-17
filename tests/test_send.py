"""出站消息链测试 (v0.5.0): AtAll / 引用回复 / 链接卡片 / 位置。"""

import importlib
import sys
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(PLUGIN_DIR.parent))

sender = importlib.import_module("astrbot-plugin-wpp.wpp_send")
event_mod = importlib.import_module("astrbot-plugin-wpp.wpp_event")

from astrbot.api.message_components import At, AtAll, Location, Plain, Reply, Share


class FakeApi:
    def __init__(self):
        self.calls = []

    async def send_text(self, to_wxid, content, ats=None):
        self.calls.append(("text", to_wxid, content, ats))

    async def send_app_xml(self, to_wxid, xml, app_type=5):
        self.calls.append(("app", to_wxid, xml, app_type))

    async def send_location(self, to_wxid, lat, lon, label="", poi_name=""):
        self.calls.append(("loc", to_wxid, lat, lon, label, poi_name))


async def run_chain(chain, to_wxid="wxid_to"):
    api = FakeApi()
    await sender.send_chain(api, to_wxid, chain)
    return api.calls


class TestOutboundChain(unittest.IsolatedAsyncioTestCase):
    async def test_at_all(self):
        calls = await run_chain([AtAll(), Plain("大家好")])
        self.assertEqual(calls, [("text", "wxid_to", "大家好", ["all"])])

    async def test_at_member(self):
        calls = await run_chain([At(qq="wxid_a"), Plain("你好")])
        self.assertEqual(calls, [("text", "wxid_to", "你好", ["wxid_a"])])

    async def test_quote_reply(self):
        calls = await run_chain(
            [Reply(id="123", sender_id="wxid_a"), Plain("收到")]
        )
        self.assertEqual(len(calls), 1)
        kind, to_wxid, xml, app_type = calls[0]
        self.assertEqual((kind, to_wxid, app_type), ("app", "wxid_to", 5))
        self.assertIn("<type>57</type>", xml)
        self.assertIn("<svrid>123</svrid>", xml)
        self.assertIn("<fromusr>wxid_a</fromusr>", xml)
        self.assertIn("<title>收到</title>", xml)

    async def test_group_quote_with_mention_prefix(self):
        """核心 reply_with_mention 插入的 At 合并进引用气泡。"""
        calls = await run_chain(
            [
                Reply(id="new123", sender_id="wxid_a"),
                At(qq="wxid_a", name="张三"),
                Plain("收到"),
            ],
            to_wxid="123@chatroom",
        )
        self.assertEqual(len(calls), 1)
        kind, to_wxid, xml, app_type = calls[0]
        self.assertEqual((kind, to_wxid, app_type), ("app", "123@chatroom", 5))
        self.assertIn("<title>@张三 收到</title>", xml)
        self.assertIn("<svrid>new123</svrid>", xml)
        self.assertIn("<fromusr>wxid_a</fromusr>", xml)

    async def test_private_quote_does_not_add_mention_prefix(self):
        calls = await run_chain(
            [Reply(id="9"), At(qq="wxid_a", name="张三"), Plain("收到")]
        )
        self.assertEqual(len(calls), 1)
        self.assertIn("<title>收到</title>", calls[0][2])

    async def test_quote_reply_long_text_falls_back_to_text(self):
        calls = await run_chain([Reply(id="123"), Plain("长" * 600)])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "text")

    async def test_share_link(self):
        calls = await run_chain(
            [Share(url="https://e.com/a?x=1&y=2", title="A&B", content="说明", image="")]
        )
        self.assertEqual(len(calls), 1)
        kind, _to, xml, app_type = calls[0]
        self.assertEqual((kind, app_type), ("app", 5))
        self.assertIn("<type>5</type>", xml)
        self.assertIn("<title>A&amp;B</title>", xml)
        self.assertIn("<url>https://e.com/a?x=1&amp;y=2</url>", xml)

    async def test_location(self):
        calls = await run_chain(
            [Location(lat=31.234, lon=121.456, title="门店A", content="某某路 1 号")]
        )
        self.assertEqual(calls, [("loc", "wxid_to", 31.234, 121.456, "某某路 1 号", "门店A")])

    async def test_quote_without_id_has_no_refermsg(self):
        xml = sender.build_quote_reply_xml("hi", "", "")
        self.assertNotIn("<refermsg>", xml)
        self.assertIn("<type>57</type>", xml)


class TestFillReplySender(unittest.TestCase):
    def _event(self, message_id="new123", user_id="wxid_a", nickname="张三"):
        from types import SimpleNamespace

        ev = object.__new__(event_mod.WppMessageEvent)
        ev.message_obj = SimpleNamespace(
            message_id=message_id,
            sender=SimpleNamespace(user_id=user_id, nickname=nickname),
        )
        return ev

    def test_fills_sender_for_current_message(self):
        chain = [Reply(id="new123")]
        self._event()._fill_reply_sender(chain)
        self.assertEqual(chain[0].sender_id, "wxid_a")
        self.assertEqual(chain[0].sender_nickname, "张三")

    def test_does_not_touch_other_message(self):
        chain = [Reply(id="other", sender_id="wxid_x")]
        self._event()._fill_reply_sender(chain)
        self.assertEqual(chain[0].sender_id, "wxid_x")


if __name__ == "__main__":
    unittest.main()
