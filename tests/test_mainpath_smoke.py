"""真实主路径冒烟: WppAccount._process_src → AstrBot 事件提交。

这是纯函数单测之外的兜底 (feedback: 单测全绿 ≠ 能跑):
  - 用 stub AccountStore / WppClient 替换网络与落盘
  - 真正走 WppAccount._process_src、wpp_messages、wpp_media、事件提交
  - 覆盖系统消息静默、白名单、群 @、主动候选群、媒体懒加载、昵称/群资料预取

在原 /tmp 冒烟脚本基础上加入 v0.7.0 的群资料/昵称预取断言。
"""

import asyncio
import importlib
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(PLUGIN_DIR.parent))

account_mod = importlib.import_module("astrbot-plugin-wpp.wpp_account")
plugin_main = importlib.import_module("astrbot-plugin-wpp.main")


class StubStore:
    def __init__(self, *a, **k):
        pass

    def load_dedup(self):
        return []

    def config_mtime(self):
        return 0

    def load_config(self):
        return {}

    def save_dedup(self, *a, **k):
        pass

    def save_config(self, *a, **k):
        pass


GROUP_PROFILE = {
    "name": "调试群",
    "owner": "wxid_owner",
    "avatar": "https://example.com/g.png",
    "members": {"wxid_f": "张三", "wxid_owner": "群主", "wxid_bot": "小助手"},
    "member_count": 3,
}


class FakeClient:
    def __init__(self, *a, **k):
        self.image_calls = 0
        self.voice_calls = 0
        self.group_profile_calls = 0
        self.nickname_calls = 0

    async def download_image_cdn(self, *a, **k):
        self.image_calls += 1
        return b"\xff\xd8\xff" + b"\x00" * 32

    async def download_image(self, *a, **k):
        self.image_calls += 1
        return b"\xff\xd8\xff" + b"\x00" * 32

    async def download_voice_binary(self, *a, **k):
        self.voice_calls += 1
        return b"\x02#!SILK_V3" + b"\x00" * 32

    async def get_group_profile(self, chatroom_id):
        self.group_profile_calls += 1
        return dict(GROUP_PROFILE)

    async def resolve_nickname(self, wxid, chatroom_id=""):
        self.nickname_calls += 1
        return {"wxid_private": "私聊好友"}.get(wxid, "")


class Ev:
    def __init__(self, msg):
        self.msg = msg
        self.message_obj = msg
        self._extras = {}
        self.tracked = []

    def track_temporary_local_file(self, path):
        self.tracked.append(path)

    def get_platform_name(self):
        return "wpp"

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

    def set_extra(self, key, value):
        self._extras[key] = value


class Plat:
    def __init__(self):
        self.events = []

    def bind_session(self, sid, acc):
        pass

    def create_event(self, msg):
        return Ev(msg)

    def commit_event(self, ev):
        self.events.append(ev)


LINK_XML = (
    "<msg><appmsg><title>商品链接</title><des>限时活动</des>"
    "<url>https://example.com/p/1</url><type>5</type></appmsg></msg>"
)
PAY_XML = "<msg><appmsg><type>2000</type><title>微信红包</title></appmsg></msg>"
SYS_XML = '<?xml version="1.0"?><sysmsg type="revokemsg">x</sysmsg>'


class TestMainPathSmoke(unittest.TestCase):
    def setUp(self):
        self._store_patch = patch.object(account_mod, "AccountStore", StubStore)
        self._client_patch = patch.object(account_mod, "WppClient", FakeClient)
        self._store_patch.start()
        self._client_patch.start()
        self.addCleanup(self._store_patch.stop)
        self.addCleanup(self._client_patch.stop)

    def new_acc(
        self,
        group_reply="atbot",
        allow_users=None,
        proactive_groups=None,
        require_at_mention=False,
    ):
        acc = account_mod.WppAccount(
            Plat(),
            "stub",
            "authcode",
            "http://127.0.0.1:1",
            "",
            allow_users=allow_users,
            group_reply=group_reply,
            proactive_groups=proactive_groups,
            require_at_mention=require_at_mention,
        )
        acc._self_wxid = "wxid_bot"
        acc._self_nickname = "小助手"
        return acc

    @staticmethod
    def run_src(acc, src):
        asyncio.run(acc._process_src(src))
        return acc.platform.events

    def test_main_path_smoke(self):
        cases = []

        def check(name, passed):
            cases.append((name, bool(passed)))

        a = self.new_acc()
        evs = self.run_src(
            a, {"sender_id": "wxid_f", "msg_id": "m1", "msg_type": 49, "content": LINK_XML}
        )
        check(
            "link card",
            len(evs) == 1
            and "商品链接" in evs[-1].msg.message_str
            and "<appmsg" not in evs[-1].msg.message_str,
        )

        a = self.new_acc()
        evs = self.run_src(a, {"sender_id": "gh_news", "msg_id": "m2", "content": "公众号推文"})
        check("gh_ silent", len(evs) == 0)

        a = self.new_acc()
        evs = self.run_src(
            a, {"sender_id": "wxid_f", "msg_id": "m3", "msg_type": 49, "content": PAY_XML}
        )
        check("payment silent", len(evs) == 0)

        a = self.new_acc()
        evs = self.run_src(
            a, {"sender_id": "wxid_f", "msg_id": "m4", "msg_type": 49, "content": SYS_XML}
        )
        check("sysmsg silent", len(evs) == 0)

        a = self.new_acc()
        evs = self.run_src(
            a,
            {
                "sender_id": "wxid_f",
                "msg_id": "m5",
                "msg_type": 49,
                "content": "",
                "app": {"category": "link", "title": "空正文卡片", "url": "https://e.com"},
            },
        )
        check("empty card kept", len(evs) == 1 and "空正文卡片" in evs[-1].msg.message_str)

        a = self.new_acc(group_reply="atbot")
        evs = self.run_src(
            a,
            {
                "sender_id": "wxid_f",
                "chatroom_id": "123@chatroom",
                "msg_id": "m6",
                "content": "@张三 你好",
                "at_user_list": ["wxid_bot"],
            },
        )
        check("group at-list", len(evs) == 1)

        a = self.new_acc(group_reply="atbot")
        evs = self.run_src(
            a,
            {
                "sender_id": "wxid_f",
                "chatroom_id": "123@chatroom",
                "msg_id": "m7",
                "content": "@张三 你好",
                "at_user_list": ["wxid_other"],
            },
        )
        check("group not at bot", len(evs) == 0)

        a = self.new_acc(group_reply="all")
        evs = self.run_src(
            a,
            {
                "sender_id": "wxid_f",
                "chatroom_id": "123@chatroom",
                "msg_id": "m8",
                "msg_type": 49,
                "content": "",
                "app": {
                    "category": "mini_program",
                    "title": "国补",
                    "mini_program": {"app_id": "wx1", "page_path": "p"},
                },
            },
        )
        check(
            "group all + mini program",
            len(evs) == 1 and "[小程序]" in evs[-1].msg.message_str,
        )

        a = self.new_acc()
        evs = self.run_src(
            a,
            {
                "sender_id": "wxid_f",
                "msg_id": "m10",
                "msg_type": 34,
                "voice": {
                    "download_context": {
                        "msg_id": 9,
                        "length": 100,
                        "from_user_name": "wxid_f",
                        "to_user_name": "wxid_bot",
                    }
                },
            },
        )
        check(
            "voice -> Record",
            len(evs) == 1 and type(evs[-1].msg.message[-1]).__name__ == "Record",
        )

        a = self.new_acc(allow_users={"wxid_f"})
        evs = self.run_src(a, {"sender_id": "wxid_stranger", "msg_id": "m9", "content": "hi"})
        check("whitelist blocks stranger", len(evs) == 0)

        # 生产主动回复与群白名单共用: 群白名单直接放行未 @ 消息
        a = self.new_acc(allow_users={"123@chatroom"})
        evs = self.run_src(
            a,
            {
                "sender_id": "wxid_f",
                "chatroom_id": "123@chatroom",
                "msg_id": "w1",
                "content": "群里随便聊聊",
            },
        )
        check(
            "whitelist group auto proactive",
            len(evs) == 1 and evs[-1].get_extra("_wpp_lazy_media") is a,
        )

        # ---- v0.6.0 主动候选群 + 媒体懒加载 ----
        a = self.new_acc(proactive_groups={"123@chatroom"})
        evs = self.run_src(
            a,
            {
                "sender_id": "wxid_f",
                "chatroom_id": "123@chatroom",
                "msg_id": "p1",
                "content": "大家聊什么呢",
            },
        )
        check("proactive non-at admitted", len(evs) == 1)
        check(
            "proactive non-at marked lazy",
            len(evs) == 1 and evs[-1].get_extra("_wpp_lazy_media") is a,
        )

        a = self.new_acc(proactive_groups={"999@chatroom"})
        evs = self.run_src(
            a,
            {
                "sender_id": "wxid_f",
                "chatroom_id": "123@chatroom",
                "msg_id": "p2",
                "content": "大家聊什么呢",
            },
        )
        check("non-proactive non-at dropped", len(evs) == 0)

        a = self.new_acc(group_reply="none", proactive_groups={"123@chatroom"})
        evs = self.run_src(
            a,
            {
                "sender_id": "wxid_f",
                "chatroom_id": "123@chatroom",
                "msg_id": "p2b",
                "content": "大家聊什么呢",
            },
        )
        check("group none overrides proactive", len(evs) == 0)

        a = self.new_acc(proactive_groups={"123@chatroom"}, require_at_mention=True)
        evs = self.run_src(
            a,
            {
                "sender_id": "wxid_f",
                "chatroom_id": "123@chatroom",
                "msg_id": "p2c",
                "content": "大家聊什么呢",
            },
        )
        check("proactive overrides require_at", len(evs) == 1)

        a = self.new_acc(proactive_groups={"123@chatroom"})
        evs = self.run_src(
            a,
            {
                "sender_id": "wxid_f",
                "chatroom_id": "123@chatroom",
                "msg_id": "p2d",
                "content": "@小助手 你好",
            },
        )
        check(
            "proactive at is eager",
            len(evs) == 1 and evs[-1].get_extra("_wpp_lazy_media") is None,
        )

        a = self.new_acc(proactive_groups={"123@chatroom"})
        evs = self.run_src(
            a,
            {
                "sender_id": "wxid_f",
                "chatroom_id": "123@chatroom",
                "msg_id": "p3",
                "msg_type": 3,
                "local_id": "img_lazy",
                "image": {
                    "data_len": 10,
                    "cdn_download_contexts": [{"file_aes_key": "k", "file_no": "n"}],
                },
            },
        )
        check(
            "proactive image not downloaded yet",
            len(evs) == 1 and a._api.image_calls == 0,
        )
        asyncio.run(plugin_main.WppPlugin.wpp_lazy_media(None, evs[-1]))
        check(
            "lazy hook downloads image",
            a._api.image_calls == 1
            and type(evs[-1].msg.message[-1]).__name__ == "Image"
            and evs[-1].get_extra("_wpp_lazy_media") is None,
        )

        a = self.new_acc(proactive_groups={"123@chatroom"})
        evs = self.run_src(
            a,
            {
                "sender_id": "wxid_f",
                "chatroom_id": "123@chatroom",
                "msg_id": "p4",
                "msg_type": 34,
                "voice": {
                    "download_context": {
                        "msg_id": 9,
                        "length": 100,
                        "from_user_name": "wxid_f",
                        "to_user_name": "wxid_bot",
                    }
                },
            },
        )
        check(
            "proactive voice eager Record",
            len(evs) == 1
            and a._api.voice_calls == 1
            and type(evs[-1].msg.message[-1]).__name__ == "Record",
        )
        asyncio.run(plugin_main.WppPlugin.wpp_lazy_media(None, evs[-1]))
        check("lazy hook skips voice", a._api.voice_calls == 1)

        # ---- v0.7.0 入站群资料 / 昵称预取 ----
        a = self.new_acc(proactive_groups={"123@chatroom"})
        evs = self.run_src(
            a,
            {
                "sender_id": "wxid_f",
                "chatroom_id": "123@chatroom",
                "msg_id": "p5",
                "content": "大家好",
            },
        )
        check(
            "group profile prefetched",
            len(evs) == 1
            and a._api.group_profile_calls == 1
            and evs[-1].msg.group.group_name == "调试群"
            and evs[-1].msg.sender.nickname == "张三"
            and evs[-1].msg.group.member_count == 3,
        )

        a = self.new_acc()
        evs = self.run_src(
            a,
            {"sender_id": "wxid_private", "msg_id": "p6", "content": "你好"},
        )
        check(
            "private nickname prefetched",
            len(evs) == 1
            and a._api.nickname_calls == 1
            and evs[-1].msg.sender.nickname == "私聊好友",
        )

        failed = [name for name, ok in cases if not ok]
        self.assertEqual(
            failed,
            [],
            f"{len(cases) - len(failed)}/{len(cases)} passed; failed={failed}",
        )


if __name__ == "__main__":
    unittest.main()
