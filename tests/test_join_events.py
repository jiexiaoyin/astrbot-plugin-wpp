"""进群/退群系统通知识别 (v0.9.0 wpp_join_events)。"""

import importlib
import sys
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(PLUGIN_DIR.parent))

join_events = importlib.import_module("astrbot-plugin-wpp.wpp_join_events")

ROOM = "12345678901@chatroom"


def src(content="", *, msg_type=None, chatroom=ROOM, **extra):
    data = {"chatroom_id": chatroom, "content": content}
    if msg_type is not None:
        data["msg_type"] = msg_type
    data.update(extra)
    return data


class TestJoinEvents(unittest.TestCase):
    def test_placeholder_10000_means_members_changed(self):
        notice = join_events.parse_notice(
            src("收到一条微信系统通知", msg_type=10000)
        )
        self.assertIsNotNone(notice)
        self.assertEqual(notice.kind, "members_changed")
        self.assertEqual(notice.chatroom_id, ROOM)

    def test_string_system_type(self):
        notice = join_events.parse_notice(src("收到一条微信系统通知", kind="system"))
        self.assertIsNotNone(notice)
        self.assertEqual(notice.kind, "members_changed")

    def test_invite_text(self):
        notice = join_events.parse_notice(src('"张三"邀请"李四"加入了群聊'))
        self.assertIsNotNone(notice)
        self.assertEqual(notice.kind, "join")
        self.assertEqual(notice.member_name, "李四")
        self.assertEqual(notice.inviter, "张三")

    def test_invite_text_with_cjk_quotes(self):
        notice = join_events.parse_notice(src("「张三」邀请「李四」加入了群聊"))
        self.assertIsNotNone(notice)
        self.assertEqual(notice.member_name, "李四")

    def test_qrcode_text(self):
        notice = join_events.parse_notice(
            src('"李四"通过扫描"张三"分享的二维码加入群聊')
        )
        self.assertIsNotNone(notice)
        self.assertEqual(notice.kind, "join")
        self.assertEqual(notice.member_name, "李四")
        self.assertEqual(notice.inviter, "张三")

    def test_plain_join_text(self):
        notice = join_events.parse_notice(src('"李四"加入了群聊'))
        self.assertIsNotNone(notice)
        self.assertEqual(notice.kind, "join")
        self.assertEqual(notice.member_name, "李四")

    def test_kick_text(self):
        notice = join_events.parse_notice(src('"群主"将"李四"移出了群聊'))
        self.assertIsNotNone(notice)
        self.assertEqual(notice.kind, "leave")
        self.assertEqual(notice.member_name, "李四")
        self.assertEqual(notice.inviter, "群主")

    def test_leave_text(self):
        notice = join_events.parse_notice(src('"李四"退出了群聊'))
        self.assertIsNotNone(notice)
        self.assertEqual(notice.kind, "leave")
        self.assertEqual(notice.member_name, "李四")

    def test_self_join_without_chatroom(self):
        notice = join_events.parse_notice(
            src("[群聊邀请] 周州 邀请你加入群聊「某某群」", kind="group_invite", chatroom="")
        )
        self.assertIsNotNone(notice)
        self.assertEqual(notice.kind, "self_join")

    def test_bare_10002_is_revoke_not_member_event(self):
        self.assertIsNone(join_events.parse_notice(src("", msg_type=10002)))
        self.assertIsNone(join_events.parse_notice(src("你撤回了一条消息", msg_type=10002)))

    def test_10002_with_join_text_still_join(self):
        notice = join_events.parse_notice(src('"李四"加入了群聊', msg_type=10002))
        self.assertIsNotNone(notice)
        self.assertEqual(notice.kind, "join")

    def test_non_group_10000_ignored(self):
        self.assertIsNone(
            join_events.parse_notice(
                src("收到一条微信系统通知", msg_type=10000, chatroom="wxid_friend")
            )
        )

    def test_normal_text_ignored(self):
        self.assertIsNone(join_events.parse_notice(src("今天下午三点开会", msg_type=1)))

    def test_nested_raw_text_found(self):
        """vendor 常把原文放在嵌套字段, content 只有占位符。"""
        notice = join_events.parse_notice(
            {
                "chatroom_id": ROOM,
                "msg_type": 10000,
                "content": "收到一条微信系统通知",
                "raw": {"Payload": {"Content": '"李四"加入了群聊'}},
            }
        )
        self.assertIsNotNone(notice)
        self.assertEqual(notice.kind, "join")
        self.assertEqual(notice.member_name, "李四")


if __name__ == "__main__":
    unittest.main()
