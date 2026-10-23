"""出站消息链测试 (v0.5.0): AtAll / 引用回复 / 链接卡片 / 位置。"""

import importlib
import sys
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(PLUGIN_DIR.parent))

sender = importlib.import_module("astrbot_plugin_wpp.wpp_send")
event_mod = importlib.import_module("astrbot_plugin_wpp.wpp_event")

from astrbot.api.message_components import At, AtAll, Location, Plain, Reply, Share


class FakeApi:
    def __init__(self, nickname=""):
        self.calls = []
        self.nick_calls = []
        self._nickname = nickname

    async def send_text(self, to_wxid, content, ats=None):
        self.calls.append(("text", to_wxid, content, ats))

    async def send_app_xml(self, to_wxid, xml, app_type=5):
        self.calls.append(("app", to_wxid, xml, app_type))

    async def send_location(self, to_wxid, lat, lon, label="", poi_name=""):
        self.calls.append(("loc", to_wxid, lat, lon, label, poi_name))

    async def resolve_nickname(self, wxid, chatroom_id=""):
        self.nick_calls.append((wxid, chatroom_id))
        return self._nickname


async def run_chain(chain, to_wxid="wxid_to", nickname=""):
    api = FakeApi(nickname)
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
        calls = await run_chain([Reply(id="123", sender_id="wxid_a"), Plain("收到")])
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

    async def test_group_quote_resolves_wxid_to_group_nickname(self):
        """入站缺昵称时 At.name == wxid, 应查群成员列表换成真实昵称。"""
        calls = await run_chain(
            [
                Reply(id="new123", sender_id="wxid_a"),
                At(qq="wxid_a", name="wxid_a"),
                Plain("收到"),
            ],
            to_wxid="123@chatroom",
            nickname="张三",
        )
        self.assertEqual(len(calls), 1)
        self.assertIn("<title>@张三 收到</title>", calls[0][2])

    async def test_group_quote_falls_back_to_wxid_when_nickname_unknown(self):
        """查不到昵称时退回 @wxid, 与 wpp-openclaw displayName || fromusr 一致。"""
        calls = await run_chain(
            [
                Reply(id="new123", sender_id="wxid_a"),
                At(qq="wxid_a", name="wxid_a"),
                Plain("收到"),
            ],
            to_wxid="123@chatroom",
        )
        self.assertEqual(len(calls), 1)
        self.assertIn("<title>@wxid_a 收到</title>", calls[0][2])

    async def test_group_quote_at_all_has_no_mention_prefix(self):
        calls = await run_chain(
            [Reply(id="new123"), AtAll(), Plain("收到")],
            to_wxid="123@chatroom",
        )
        self.assertEqual(len(calls), 1)
        self.assertIn("<title>收到</title>", calls[0][2])

    async def test_private_quote_does_not_add_mention_prefix(self):
        calls = await run_chain([Reply(id="9"), At(qq="wxid_a", name="张三"), Plain("收到")])
        self.assertEqual(len(calls), 1)
        self.assertIn("<title>收到</title>", calls[0][2])

    async def test_quote_reply_long_text_quotes_head_then_sends_tail(self):
        """v0.7.0: 长回复不再放弃引用, 第一段进引用气泡, 剩余续发。"""
        calls = await run_chain([Reply(id="123"), Plain("长" * 600)])
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0], "app")
        self.assertIn("<title>" + "长" * 500 + "</title>", calls[0][2])
        self.assertEqual(calls[1], ("text", "wxid_to", "长" * 100, None))

    async def test_long_group_quote_keeps_mention_in_bubble(self):
        calls = await run_chain(
            [
                Reply(id="new123", sender_id="wxid_a"),
                At(qq="wxid_a", name="张三"),
                Plain("长" * 600),
            ],
            to_wxid="123@chatroom",
        )
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0], "app")
        title = calls[0][2].split("<title>", 1)[1].split("</title>", 1)[0]
        self.assertTrue(title.startswith("@张三 "))
        self.assertLessEqual(len(title), 500)
        self.assertEqual(calls[1][0], "text")
        # head 预留 "@张三 " 的 4 个字符 → 496 + 104 = 600, 内容无损
        self.assertEqual(calls[1][2], "长" * 104)
        self.assertEqual(title[4:] + calls[1][2], "长" * 600)

    async def test_quote_send_failure_falls_back_to_full_text(self):
        class FailingApi(FakeApi):
            async def send_app_xml(self, to_wxid, xml, app_type=5):
                raise RuntimeError("vendor down")

        api = FailingApi()
        await sender.send_chain(
            api, "123@chatroom", [Reply(id="1"), At(qq="wxid_a", name="张三"), Plain("收到")]
        )
        self.assertEqual(len(api.calls), 1)
        self.assertEqual(api.calls[0], ("text", "123@chatroom", "收到", ["wxid_a"]))

    def test_split_quote_text_prefers_sentence_boundary(self):
        text = "第一句。" + "长" * 600
        head, tail = sender.split_quote_text(text, 100)
        self.assertTrue(head.startswith("第一句。"))
        self.assertLessEqual(len(head), 100)
        self.assertEqual(head + tail, text)

    def test_split_quote_text_hard_cut_without_boundary(self):
        head, tail = sender.split_quote_text("长" * 600, 500)
        self.assertEqual(head, "长" * 500)
        self.assertEqual(tail, "长" * 100)

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

    async def test_quote_refermsg_enriched_fields(self):
        xml = sender.build_quote_reply_xml(
            "收到",
            "123",
            "wxid_a",
            display_name="张三",
            chat_user="9@chatroom",
            original_content="你好 <&>",
            create_time=1700000000,
        )
        self.assertIn("<chatusr>9@chatroom</chatusr>", xml)
        self.assertIn("<displayname>张三</displayname>", xml)
        self.assertIn("<content>你好 &lt;&amp;&gt;</content>", xml)
        self.assertIn("<createtime>1700000000</createtime>", xml)


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


class TestSendFailurePropagation(unittest.IsolatedAsyncioTestCase):
    """全部组件发送失败必须抛错, 不能让核心误以为已回复。"""

    def _event(self, api):
        from types import SimpleNamespace

        ev = object.__new__(event_mod.WppMessageEvent)
        ev._api = api
        ev.account_id = "stub"
        ev.session = SimpleNamespace(session_id="wxid_f")
        ev.message_obj = SimpleNamespace(
            message_id="m1",
            sender=SimpleNamespace(user_id="wxid_f", nickname="张三"),
        )
        ev.platform_meta = SimpleNamespace(name="wpp")
        return ev

    async def test_all_failed_raises(self):
        from astrbot.api.event import MessageChain

        class Api:
            async def send_text(self, *a, **k):
                raise RuntimeError("vendor down")

        with self.assertRaises(RuntimeError) as ctx:
            await self._event(Api()).send(MessageChain([Plain("你好")]))
        self.assertIn("发送全部失败", str(ctx.exception))

    async def test_success_does_not_raise(self):
        from astrbot.api.event import MessageChain

        class Api:
            async def send_text(self, *a, **k):
                return {"Code": 0}

        await self._event(Api()).send(MessageChain([Plain("你好")]))


if __name__ == "__main__":
    unittest.main()
