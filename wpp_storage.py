"""WPP 账号运行时数据持久化。

AstrBot 插件规范: 运行时数据放 `data/plugin_data/<plugin_name>/`, 不要写进插件源码目录
(插件更新/重装会覆盖源码目录)。见 docs/zh/dev/star/guides/storage.md。

布局:
  data/plugin_data/astrbot-plugin-wpp/accounts/<account_id>/wpp_whitelist.json
  data/plugin_data/astrbot-plugin-wpp/accounts/<account_id>/dedup.json

兼容: 旧版本把白名单写在插件目录 `accounts/<account_id>/`, 读取时自动回退并迁移到新目录。
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

PLUGIN_NAME = "astrbot-plugin-wpp"
_ACCOUNT_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def safe_account_id(account_id: str) -> str:
    """账号/实例 id 会拼进文件路径, 必须去掉路径分隔符/.. 等。"""
    aid = _ACCOUNT_ID_RE.sub("_", str(account_id or "account")).strip("._") or "account"
    return aid[:64]


def plugin_data_dir() -> Path:
    """插件数据目录 (兼容旧版 AstrBot / 测试环境)。"""
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

        return Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME
    except Exception:  # noqa: BLE001
        return Path(__file__).resolve().parent / "data" / "plugin_data"


def legacy_accounts_dir() -> Path:
    """旧版数据目录 (插件源码目录下的 accounts/)。"""
    return Path(__file__).resolve().parent / "accounts"


class AccountStore:
    """单个微信账号的运行时配置/状态文件。"""

    def __init__(self, account_id: str) -> None:
        self.account_id = safe_account_id(account_id)
        self.dir = plugin_data_dir() / "accounts" / self.account_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.dir / "wpp_whitelist.json"
        self.dedup_path = self.dir / "dedup.json"
        self._legacy_dir = legacy_accounts_dir() / self.account_id

    # ------------------------------------------------------------------ 白名单/策略
    def load_config(self) -> dict:
        """读取账号配置; 新目录没有时回退旧目录 (并迁移)。"""
        data = self._read_json(self.config_path)
        if data is None:
            legacy = self._read_json(self._legacy_dir / "wpp_whitelist.json")
            if isinstance(legacy, dict):
                logger.info(
                    f"[WPP:{self.account_id}] 迁移旧白名单文件 → {self.config_path}"
                )
                self.save_config(legacy)
                return legacy
        return data if isinstance(data, dict) else {}

    def save_config(self, data: dict) -> None:
        self._atomic_write(self.config_path, json.dumps(data, ensure_ascii=False, indent=2))

    def config_mtime(self) -> float:
        for p in (self.config_path, self._legacy_dir / "wpp_whitelist.json"):
            try:
                if p.exists():
                    return p.stat().st_mtime
            except OSError:
                continue
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
    "legacy_accounts_dir",
    "safe_account_id",
    "AccountStore",
]
