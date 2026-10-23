"""v0.6.0 主动回复候选群 / 媒体懒加载测试。

覆盖:
  1. 准入 ≠ 唤醒: proactive_groups 放行未 @ 消息, 但不产生唤醒标志
  2. 优先级: 黑名单 / none / 群白名单 / require_at_mention
  3. 配置解析: wpp_proactive_groups (一个实例一个账号)
  4. enrich_media 的 include/exclude 选择性下载
  5. on_waiting_llm_request 钩子懒加载一次, 并登记临时文件

在 AstrBot 容器内运行:
  docker exec astrbot python3 -m pytest /AstrBot/data/plugins/astrbot_plugin_wpp/tests -v
"""

import importlib
import os
import sys
import unittest
from pathlib import Path

from astrbot.api.message_components import File, Image, Plain
from astrbot.api.platform import AstrBotMessage

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(PLUGIN_DIR.parent))

policy = importlib.import_module("astrbot_plugin_wpp.wpp_policy")
media = importlib.import_module("astrbot_plugin_wpp.wpp_media")
adapter = importlib.import_module("astrbot_plugin_wpp.wpp_adapter")
plugin_main = importlib.import_module("astrbot_plugin_wpp.main")


def snap(**kwargs):
    base = dict(
        self_wxid="wxid_bot",
        self_nickname="小助手",
        group_reply="atbot",
    )
    base.update(kwargs)
    return policy.PolicySnapshot(**base)


class TestProactivePolicy(unittest.TestCase):
    def test_proactive_group_admits_without_at(self):
        p = snap(proactive_groups={"g1@chatroom"})
        d = policy.classify(p, "wxid_u", True, "g1@chatroom", "随便聊聊")
        self.assertTrue(d.allowed)
        self.assertTrue(d.proactive)
        self.assertFalse(d.at_me)
        self.assertEqual(d.reason, "proactive")

    def test_non_proactive_group_still_requires_at(self):
        p = snap(proactive_groups={"g1@chatroom"})
        self.assertFalse(policy.is_allowed(p, "wxid_u", True, "g2@chatroom", "随便聊聊"))

    def test_group_whitelist_is_automatically_proactive(self):
        """生产主动回复与群白名单共用: 进白名单即放行未 @ 消息。"""
        p = snap(allow_users={"g1@chatroom"})
        d = policy.classify(p, "wxid_u", True, "g1@chatroom", "随便聊聊")
        self.assertTrue(d.allowed)
        self.assertTrue(d.proactive)
        self.assertFalse(d.at_me)
        self.assertEqual(d.reason, "proactive")
        self.assertEqual(policy.effective_proactive_groups(p), {"g1@chatroom"})

    def test_private_whitelist_does_not_make_group_proactive(self):
        p = snap(allow_users={"wxid_u"})
        self.assertFalse(policy.is_allowed(p, "wxid_u", True, "g1@chatroom", "随便聊聊"))

    def test_explicit_proactive_does_not_bypass_group_whitelist(self):
        p = snap(allow_users={"g1@chatroom"}, proactive_groups={"g2@chatroom"})
        # 有群白名单时, 非白名单群仍被步骤 2 拦住; 显式列表不会绕过白名单。
        self.assertFalse(policy.is_allowed(p, "wxid_u", True, "g2@chatroom", "随便聊聊"))
        self.assertTrue(policy.is_allowed(p, "wxid_u", True, "g1@chatroom", "随便聊聊"))

    def test_at_me_in_proactive_group_is_normal_wake(self):
        p = snap(proactive_groups={"g1@chatroom"})
        d = policy.classify(p, "wxid_u", True, "g1@chatroom", "@小助手 你好")
        self.assertTrue(d.allowed)
        self.assertTrue(d.at_me)

    def test_proactive_overrides_require_at_mention(self):
        p = snap(proactive_groups={"g1@chatroom"}, require_at_mention=True)
        self.assertTrue(policy.is_allowed(p, "wxid_u", True, "g1@chatroom", "随便聊聊"))
        self.assertFalse(policy.is_allowed(p, "wxid_u", True, "g2@chatroom", "随便聊聊"))

    def test_group_none_is_hard_off(self):
        p = snap(proactive_groups={"g1@chatroom"}, group_reply="none")
        self.assertFalse(policy.is_allowed(p, "wxid_u", True, "g1@chatroom", "随便聊聊"))

    def test_blacklist_group_wins(self):
        p = snap(
            proactive_groups={"g1@chatroom"},
            blacklist_groups={"g1@chatroom"},
        )
        self.assertFalse(policy.is_allowed(p, "wxid_u", True, "g1@chatroom", "随便聊聊"))

    def test_group_whitelist_still_enforced(self):
        p = snap(
            proactive_groups={"g1@chatroom"},
            allow_users={"other@chatroom"},
        )
        self.assertFalse(policy.is_allowed(p, "wxid_u", True, "g1@chatroom", "随便聊聊"))

    def test_block_user_wins(self):
        p = snap(proactive_groups={"g1@chatroom"}, block_users={"wxid_bad"})
        self.assertFalse(policy.is_allowed(p, "wxid_bad", True, "g1@chatroom", "随便聊聊"))

    def test_all_mode_is_observe_not_forced_wake(self):
        p = snap(group_reply="all")
        d = policy.classify(p, "wxid_u", True, "g1@chatroom", "随便聊聊")
        self.assertTrue(d.allowed)
        self.assertFalse(d.at_me)
        self.assertFalse(d.proactive)
        self.assertEqual(d.reason, "group_all")

    def test_private_unaffected(self):
        p = snap(proactive_groups={"g1@chatroom"}, private_allow_all=True)
        d = policy.classify(p, "wxid_u", False, "", "你好")
        self.assertTrue(d.allowed)


class TestAccountConfigParsing(unittest.TestCase):
    def test_single_account_proactive_groups(self):
        cfg = {
            "wpp_base_url": "http://127.0.0.1:18062",
            "wpp_auth_token": "x",
            "wpp_proactive_groups": "g1@chatroom, g2@chatroom",
        }
        out = adapter._parse_account(cfg)
        self.assertEqual(out["proactive_groups"], {"g1@chatroom", "g2@chatroom"})

    def test_default_empty(self):
        cfg = {
            "wpp_base_url": "http://127.0.0.1:18062",
            "wpp_auth_token": "x",
        }
        out = adapter._parse_account(cfg)
        self.assertEqual(out["proactive_groups"], set())

    def test_no_authcode_returns_none(self):
        out = adapter._parse_account({"id": "wpp", "wpp_base_url": "http://x"})
        self.assertIsNone(out)


class _FakeApi:
    def __init__(self):
        self.image_calls = 0
        self.voice_calls = 0
        self.file_calls = 0

    async def download_image_cdn(self, key, no):
        self.image_calls += 1
        return b"\xff\xd8\xff" + b"\x00" * 16

    async def download_image(self, *args, **kwargs):
        self.image_calls += 1
        return b"\xff\xd8\xff" + b"\x00" * 16

    async def download_voice_binary(self, *args, **kwargs):
        self.voice_calls += 1
        return b"\x02#!SILK_V3" + b"\x00" * 16

    async def download_file_binary(self, *args, **kwargs):
        self.file_calls += 1
        return b"hello"


def image_msg() -> AstrBotMessage:
    msg = AstrBotMessage()
    msg.message = [Plain("[图片]")]
    msg.message_str = "[图片]"
    msg._wpp_image_meta = {"msg_id": "1", "to_wxid": "g@chatroom", "data_len": 3}
    return msg


def voice_msg() -> AstrBotMessage:
    msg = AstrBotMessage()
    msg.message = [Plain("[语音]")]
    msg.message_str = "[语音]"
    msg._wpp_voice_meta = {
        "msg_id": "v1",
        "new_msg_id": "",
        "client_msg_id": "",
        "master_buf_id": "0",
        "length": 100,
        "format": 4,
        "chat_room_name": "g@chatroom",
        "from_user_name": "u",
        "to_user_name": "wxid_bot",
    }
    return msg


class TestSelectiveMedia(unittest.IsolatedAsyncioTestCase):
    async def test_include_voice_skips_image(self):
        api = _FakeApi()
        msg = image_msg()
        paths = await media.enrich_media(api, msg, include={"voice"})
        self.assertEqual(paths, [])
        self.assertEqual(api.image_calls, 0)
        self.assertEqual(msg.message_str, "[图片]")

    async def test_exclude_voice_downloads_image(self):
        api = _FakeApi()
        msg = image_msg()
        await media.enrich_media(api, msg, exclude={"voice"})
        self.assertEqual(api.image_calls, 1)
        self.assertTrue(any(isinstance(c, Image) for c in msg.message))

    async def test_voice_include_then_exclude_does_not_redownload(self):
        api = _FakeApi()
        msg = voice_msg()
        paths = await media.enrich_media(api, msg, include={"voice"})
        self.assertEqual(api.voice_calls, 1)
        self.assertEqual(len(paths), 1)
        self.assertTrue(any(type(c).__name__ == "Record" for c in msg.message))
        await media.enrich_media(api, msg, exclude={"voice"})
        self.assertEqual(api.voice_calls, 1)
        os.unlink(paths[0])


class _FakeEvent:
    def __init__(self, msg: AstrBotMessage, platform: str = adapter.PLATFORM_NAME):
        self.message_obj = msg
        self.platform = platform
        self._extras = {}
        self.tracked = []

    def get_platform_name(self):
        return self.platform

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

    def set_extra(self, key, value):
        self._extras[key] = value

    def track_temporary_local_file(self, path):
        self.tracked.append(path)


class _FakeAccount:
    account_id = "test"

    def __init__(self):
        self._api = _FakeApi()


class TestLazyMediaHook(unittest.IsolatedAsyncioTestCase):
    async def test_hook_enriches_once_and_tracks_file(self):
        msg = AstrBotMessage()
        msg.message = [Plain("[文件] a.txt")]
        msg.message_str = "[文件] a.txt"
        msg._wpp_file_meta = {
            "attach_id": "at1",
            "user_name": "u",
            "data_len": 5,
            "filename": "a.txt",
        }
        event = _FakeEvent(msg)
        account = _FakeAccount()
        event.set_extra("_wpp_lazy_media", account)

        await plugin_main.WppPlugin.wpp_lazy_media(None, event)

        self.assertEqual(account._api.file_calls, 1)
        self.assertTrue(any(isinstance(c, File) for c in msg.message))
        self.assertEqual(len(event.tracked), 1)
        self.assertTrue(os.path.exists(event.tracked[0]))
        self.assertIsNone(event.get_extra("_wpp_lazy_media"))
        os.unlink(event.tracked[0])

        # 第二次调用不重复下载
        await plugin_main.WppPlugin.wpp_lazy_media(None, event)
        self.assertEqual(account._api.file_calls, 1)

    async def test_hook_ignores_other_platforms(self):
        msg = image_msg()
        event = _FakeEvent(msg, platform="aiocqhttp")
        account = _FakeAccount()
        event.set_extra("_wpp_lazy_media", account)

        await plugin_main.WppPlugin.wpp_lazy_media(None, event)

        self.assertEqual(account._api.image_calls, 0)
        self.assertIs(event.get_extra("_wpp_lazy_media"), account)


if __name__ == "__main__":
    unittest.main()
