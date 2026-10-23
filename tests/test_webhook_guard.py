"""webhook 兜底通道的鉴权边界 (fail-closed / HMAC-SHA256 / filehelper 方向)。"""

import hashlib
import hmac
import importlib
import sys
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(PLUGIN_DIR.parent))

adapter_mod = importlib.import_module("astrbot_plugin_wpp.wpp_adapter")
msgs = importlib.import_module("astrbot_plugin_wpp.wpp_messages")

BODY = '{"Data":{"Content":"hi"}}'
SECRET = "s3cret"


def _sig(algo: str) -> str:
    return hmac.new(SECRET.encode(), BODY.encode(), getattr(hashlib, algo)).hexdigest()


class TestWebhookSignature(unittest.TestCase):
    def test_sha256_accepted_with_and_without_prefix(self):
        sig = _sig("sha256")
        verify = adapter_mod.WppPlatformAdapter._verify_webhook_signature
        self.assertTrue(verify(BODY, sig, SECRET))
        self.assertTrue(verify(BODY, f"sha256={sig}", SECRET))
        # 大小写/空白不敏感
        self.assertTrue(verify(BODY, f" SHA256={sig.upper()} ", SECRET))

    def test_other_algorithms_rejected(self):
        """md5/sha1 降级签名一律拒绝。"""
        verify = adapter_mod.WppPlatformAdapter._verify_webhook_signature
        self.assertFalse(verify(BODY, f"md5={_sig('md5')}", SECRET))
        self.assertFalse(verify(BODY, f"sha1={_sig('sha1')}", SECRET))

    def test_wrong_or_truncated_signature_rejected(self):
        verify = adapter_mod.WppPlatformAdapter._verify_webhook_signature
        self.assertFalse(verify(BODY, "", SECRET))
        self.assertFalse(verify(BODY, "deadbeef", SECRET))
        self.assertFalse(verify(BODY, _sig("sha256")[:-1], SECRET))
        self.assertFalse(verify(BODY, _sig("sha256"), "wrong-secret"))


class TestFilehelperDirection(unittest.TestCase):
    def test_recipient_or_conversation_matches(self):
        self.assertTrue(msgs.src_is_filehelper({"recipient_id": "filehelper"}))
        self.assertTrue(msgs.src_is_filehelper({"conversation_id": "filehelper"}))

    def test_sender_only_does_not_match(self):
        """文件助手发来的消息不是本机命令。"""
        self.assertFalse(msgs.src_is_filehelper({"sender_id": "filehelper"}))
        self.assertFalse(
            msgs.src_is_filehelper(
                {"sender_id": "filehelper", "recipient_id": "wxid_bot", "conversation_id": "wxid_bot"}
            )
        )


if __name__ == "__main__":
    unittest.main()
