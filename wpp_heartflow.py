"""WPP 群白名单 → Heartflow `chat_whitelist` 单向同步。

两种来源 (wpp_heartflow_whitelist_mode):
  - auto: 参考 WPP 群白名单 (allow_users 的 @chatroom + proactive_groups);
  - custom: 单独使用 wpp_heartflow_groups, 但必须是群白名单的子集。

不变量: 群白名单必须包括心流白名单。群白名单非空时, 不在白名单中的
目标群会被拒绝并跳过同步, 避免 Heartflow 白名单包含适配器不会放行的群。

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

from .wpp_constants import PLATFORM_NAME

HEARTFLOW_PLUGIN_NAME = "astrbot_plugin_Heartflow"
HEARTFLOW_CONFIG_FILENAME = "astrbot_plugin_Heartflow_config.json"

# 由 WppPlugin 在初始化时注入, 用于定位已加载的 Heartflow 实例
_context: Any = None


def configure(context: Any) -> None:
    global _context
    _context = context


HEARTFLOW_WHITELIST_MODES = ("auto", "custom")


def _normalize_groups(values: Iterable[Any]) -> set[str]:
    return {str(value).strip() for value in values or () if str(value).strip()}


def account_group_whitelist(accounts: Iterable[Any]) -> set[str]:
    """汇总各账号群白名单 (allow_users 中 @chatroom 结尾的项)。"""
    groups: set[str] = set()
    for acct in accounts or ():
        for user in getattr(acct, "allow_users", ()) or ():
            value = str(user).strip()
            if value.endswith("@chatroom"):
                groups.add(value)
    return groups


def account_proactive_groups(accounts: Iterable[Any]) -> set[str]:
    """汇总各账号显式主动候选群。"""
    groups: set[str] = set()
    for acct in accounts or ():
        groups.update(_normalize_groups(getattr(acct, "proactive_groups", ()) or ()))
    return groups


def resolve_heartflow_groups(
    accounts: Iterable[Any],
    custom_groups: Iterable[Any] = (),
    mode: str = "auto",
) -> tuple[set[str], set[str]]:
    """计算 Heartflow 白名单, 并返回 (可用群, 被群白名单拒绝的群)。

    mode:
      - auto: 参考 WPP 群白名单 (并包含显式 proactive_groups), 默认;
      - custom: 只用 custom_groups 单独设置, 但必须是群白名单的子集。

    不变量: **群白名单必须包括心流白名单**。群白名单非空时, 任何不在
    群白名单中的目标群都会被拒绝并跳过, 避免 Heartflow 收到适配器根本
    不会放行的群。群白名单为空 = 不限制群, 此时 custom/proactive 均可用。
    """
    whitelist = account_group_whitelist(accounts)
    custom = _normalize_groups(custom_groups)
    normalized = str(mode or "auto").strip().lower()
    if normalized in ("custom", "manual", "separate"):
        target = set(custom)
    elif normalized in ("auto", "reference", "whitelist", ""):
        target = whitelist | account_proactive_groups(accounts)
    else:
        logger.warning(f"[WPP] 未知 Heartflow 白名单模式 {mode!r}, 回退 auto (参考群白名单)")
        target = whitelist | account_proactive_groups(accounts)

    rejected: set[str] = set()
    if whitelist:
        rejected = target - whitelist
        target &= whitelist
    return target, rejected


def collect_group_ids(
    accounts: Iterable[Any],
    custom_groups: Iterable[Any] = (),
    mode: str = "auto",
) -> set[str]:
    """汇总需要进 Heartflow 的群 ID (兼容旧调用)。"""
    groups, _ = resolve_heartflow_groups(accounts, custom_groups, mode)
    return groups


def build_umos(platform_id: str, groups: Iterable[str]) -> list[str]:
    pid = str(platform_id or PLATFORM_NAME).strip() or PLATFORM_NAME
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
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.wpp-tmp")
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    try:
        # fsync 后再 replace: 断电时不能留下 0 字节的 Heartflow 配置
        with open(tmp, "w", encoding="utf-8-sig" if had_bom else "utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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


def sync_heartflow_whitelist(platform_id: str, groups: Iterable[str], reason: str = "") -> dict:
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
