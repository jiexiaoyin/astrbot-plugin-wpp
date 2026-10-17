"""WPP 群白名单 → Heartflow `chat_whitelist` 单向同步。

同步源: 各账号 `allow_users` 中的群 ID + `proactive_groups`。
同步目标: `data/config/astrbot_plugin_Heartflow_config.json`。

只替换本平台实例的 `<platform_id>:GroupMessage:` 条目, 保留其他平台/手动条目;
写盘后同时更新已加载的 Heartflow 实例, 不需要重启 AstrBot。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable

from astrbot import logger
from astrbot.api.star import get_astrbot_data_path

HEARTFLOW_PLUGIN_NAME = "astrbot_plugin_Heartflow"
HEARTFLOW_CONFIG_FILENAME = "astrbot_plugin_Heartflow_config.json"

# 由 WppPlugin 在初始化时注入, 用于定位已加载的 Heartflow 实例
_context: Any = None


def configure(context: Any) -> None:
    global _context
    _context = context


def collect_group_ids(accounts: Iterable[Any]) -> set[str]:
    """汇总需要进 Heartflow 的群 ID: 群白名单 + 主动候选群。"""
    groups: set[str] = set()
    for acct in accounts or ():
        for user in getattr(acct, "allow_users", ()) or ():
            value = str(user).strip()
            if value.endswith("@chatroom"):
                groups.add(value)
        for group in getattr(acct, "proactive_groups", ()) or ():
            value = str(group).strip()
            if value:
                groups.add(value)
    return groups


def build_umos(platform_id: str, groups: Iterable[str]) -> list[str]:
    pid = str(platform_id or "wpp").strip() or "wpp"
    return [
        f"{pid}:GroupMessage:{group}"
        for group in sorted({str(g).strip() for g in groups if str(g).strip()})
    ]


def merge_whitelist(
    existing: Iterable[Any], platform_id: str, wpp_umos: Iterable[str]
) -> list[str]:
    """替换本平台的群条目, 保留其他平台/手写条目。"""
    prefix = f"{str(platform_id or 'wpp').strip() or 'wpp'}:GroupMessage:"
    kept = [
        str(item).strip()
        for item in (existing or [])
        if str(item).strip() and not str(item).startswith(prefix)
    ]
    return sorted(set(kept) | {str(u).strip() for u in wpp_umos if str(u).strip()})


def heartflow_config_path() -> Path:
    return Path(get_astrbot_data_path()) / "config" / HEARTFLOW_CONFIG_FILENAME


def _read_config(path: Path) -> tuple[dict, bool]:
    raw = path.read_bytes()
    return json.loads(raw.decode("utf-8-sig")), raw.startswith(b"\xef\xbb\xbf")


def _write_config(path: Path, data: dict, had_bom: bool) -> None:
    tmp = path.with_name(f"{path.name}.wpp-tmp")
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    tmp.write_text(text, encoding="utf-8-sig" if had_bom else "utf-8")
    os.replace(tmp, path)


def _update_live_instance(whitelist: list[str]) -> bool:
    """把新名单推给已加载的 Heartflow 实例, 使其无需重载插件即可生效。"""
    context = _context
    if context is None:
        return False
    try:
        stars = context.get_all_stars()
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[WPP] 读取插件列表失败, 无法更新 Heartflow 实例: {e}")
        return False
    for meta in stars or []:
        name = str(getattr(meta, "name", "") or "")
        module = str(getattr(meta, "module_path", "") or "")
        if HEARTFLOW_PLUGIN_NAME not in name and HEARTFLOW_PLUGIN_NAME not in module:
            continue
        instance = getattr(meta, "star_cls", None)
        if instance is None:
            continue
        try:
            instance.chat_whitelist = list(whitelist)
            config = getattr(instance, "config", None)
            if hasattr(config, "__setitem__"):
                config["chat_whitelist"] = list(whitelist)
            return True
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[WPP] 更新 Heartflow 实例白名单失败: {e}")
            return False
    return False


def sync_heartflow_whitelist(
    platform_id: str, groups: Iterable[str], reason: str = ""
) -> dict:
    """把 WPP 群白名单写入 Heartflow 配置并更新运行中实例。

    返回 {path, wpp_umos, changed, live}; Heartflow 未安装/未配置时返回
    changed=False 且不报错 (可选集成)。
    """
    umos = build_umos(platform_id, groups)
    path = heartflow_config_path()
    result = {"path": str(path), "wpp_umos": umos, "changed": False, "live": False}
    if not path.exists():
        return result

    try:
        data, had_bom = _read_config(path)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[WPP] 读取 Heartflow 配置失败, 跳过白名单同步: {e}")
        return result
    if not isinstance(data, dict):
        logger.warning("[WPP] Heartflow 配置不是 JSON 对象, 跳过白名单同步")
        return result

    existing = data.get("chat_whitelist") or []
    merged = merge_whitelist(existing, platform_id, umos)
    if merged != existing:
        data["chat_whitelist"] = merged
        try:
            _write_config(path, data, had_bom)
            result["changed"] = True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[WPP] 写入 Heartflow 配置失败: {e}")

    result["live"] = _update_live_instance(merged)
    if result["changed"] or result["live"]:
        logger.info(
            f"[WPP] Heartflow 白名单已同步 ({reason or 'manual'}): "
            f"{len(umos)} 个群, 写盘={'是' if result['changed'] else '否'}, "
            f"实时生效={'是' if result['live'] else '否'}"
        )
    return result
