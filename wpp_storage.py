"""WPP 账号运行时数据持久化。

AstrBot 插件规范: 运行时数据放 `data/plugin_data/<plugin_name>/`, 不要写进插件源码目录
(插件更新/重装会覆盖源码目录)。见 docs/zh/dev/star/guides/storage.md。

布局:
  data/plugin_data/astrbot-plugin-wpp/accounts/<account_id>/wpp_whitelist.json
  data/plugin_data/astrbot-plugin-wpp/accounts/<account_id>/dedup.json
  data/plugin_data/astrbot-plugin-wpp/accounts/<account_id>/members.json  (v0.9.0 群成员基线)

不在插件源码目录下保留任何读写兜底；AstrBot 插件更新/重装会覆盖源码目录。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from astrbot import logger

from .wpp_constants import PLATFORM_NAME, RUNTIME_ID_SLUG

PLUGIN_NAME = "astrbot-plugin-wpp"
_ACCOUNT_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def safe_account_id(account_id: str) -> str:
    """账号/实例 id 会拼进文件路径, 必须去掉路径分隔符/.. 等。

    平台技术名是中文「个人微信（Pad协议）」；所有中文字符会被正则净化成
    ``_``，导致多实例落到同一个 ``accounts/account``。这里先把平台名前缀
    映射到稳定的 ASCII ``wpp``，既避免冲突，也沿用 v0.12.x 的运行时目录和
    webhook token，不必迁移已有数据。
    """
    raw = str(account_id or "account")
    if raw == PLATFORM_NAME:
        raw = RUNTIME_ID_SLUG
    elif raw.startswith(PLATFORM_NAME):
        raw = RUNTIME_ID_SLUG + raw[len(PLATFORM_NAME) :]
    aid = _ACCOUNT_ID_RE.sub("_", raw).strip("._") or "account"
    return aid[:64]


def plugin_data_dir() -> Path:
    """插件数据目录 (AstrBot 规范的 data/plugin_data/<plugin_name>)。"""
    from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

    return Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME


class AccountStore:
    """单个微信账号的运行时配置/状态文件。"""

    def __init__(self, account_id: str) -> None:
        self.account_id = safe_account_id(account_id)
        self.dir = plugin_data_dir() / "accounts" / self.account_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.dir / "wpp_whitelist.json"
        self.dedup_path = self.dir / "dedup.json"
        self.members_path = self.dir / "members.json"

    # ------------------------------------------------------------------ 白名单/策略
    def load_config(self) -> dict:
        """读取账号配置; 文件缺失/损坏返回空配置。"""
        data = self._read_json(self.config_path)
        return data if isinstance(data, dict) else {}

    def save_config(self, data: dict) -> None:
        self._atomic_write(self.config_path, json.dumps(data, ensure_ascii=False, indent=2))

    def config_mtime(self) -> float:
        try:
            if self.config_path.exists():
                return self.config_path.stat().st_mtime
        except OSError:
            pass
        return 0.0

    # ------------------------------------------------------------------ 去重
    def load_dedup(self) -> list[str]:
        data = self._read_json(self.dedup_path)
        if isinstance(data, dict):
            ids = data.get("ids")
            if isinstance(ids, list):
                return [str(x) for x in ids if x]
        if isinstance(data, list):
            return [str(x) for x in data if x]
        return []

    def save_dedup(self, ids: list[str]) -> None:
        self._atomic_write(
            self.dedup_path,
            json.dumps({"ids": ids, "saved_at": int(time.time())}, ensure_ascii=False),
        )

    # ------------------------------------------------------------------ 群成员基线
    def load_members(self) -> dict:
        """读取群成员基线 (v0.9.0 进群欢迎)；缺失/损坏返回空状态。"""
        from .wpp_join_welcome import normalize_state

        return normalize_state(self._read_json(self.members_path))

    def save_members(self, state: dict) -> None:
        """原子写成员基线。失败向上抛 —— 调用方据此放弃发送欢迎语。"""
        self._atomic_write(
            self.members_path,
            json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        )

    # ------------------------------------------------------------------ 内部
    @staticmethod
    def _read_json(path: Path) -> Any:
        try:
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[WPP] 读取 {path} 失败: {e}")
        return None

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        """原子写 (临时文件 + os.replace), 避免半截文件。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


__all__ = [
    "PLUGIN_NAME",
    "plugin_data_dir",
    "safe_account_id",
    "AccountStore",
]
