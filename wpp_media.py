"""WPP 媒体消息富化: 下载图片/文件/视频并注入 AstrBot 消息链。

约定:
  - 下载到 AstrBot 临时目录, 文件名带随机后缀 (避免同名并发覆盖)
  - 返回临时文件路径列表, 由调用方 `event.track_temporary_local_file()` 登记,
    事件结束后 AstrBot 会自动清理
"""

from __future__ import annotations

import os
import uuid
from typing import Any

from astrbot import logger
from astrbot.api.message_components import File, Image, Plain, Record, Video
from astrbot.api.platform import AstrBotMessage
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

from .wpp_client import WppClient

# 单文件上限 (与 wpp_client 保持一致)
MAX_FILE_BYTES = 200 * 1024 * 1024


def _temp_path(prefix: str, ext: str) -> str:
    ext = ext if ext.startswith(".") else (f".{ext}" if ext else "")
    return os.path.join(get_astrbot_temp_path(), f"wpp_{prefix}_{uuid.uuid4().hex[:10]}{ext}")


def _safe_ext(filename: str) -> str:
    _base, ext = os.path.splitext(filename or "")
    ext = ext.lower()
    # 只保留常规扩展名, 防止把奇怪字符带进文件名
    if not ext or len(ext) > 10 or not all(c.isalnum() or c == "." for c in ext):
        return ""
    return ext


def _skill_hint(ext: str) -> str:
    if ext in (".xlsx", ".xlsm", ".xls", ".csv", ".tsv"):
        return " (这是Excel/表格文件, 请用 spreadsheets 技能读取, 不要用文本读取工具)"
    if ext == ".pdf":
        return " (这是PDF文件, 请用 pdf/document 技能读取)"
    if ext in (".docx", ".doc", ".pptx", ".ppt"):
        return " (这是Office文档, 请用 documents 技能读取)"
    return ""


def _non_media_prefix(msg: AstrBotMessage) -> list[Any]:
    """保留 Reply/At 等非媒体组件 (媒体组件和占位 Plain 会被重建)。"""
    return [
        c for c in (msg.message or []) if not isinstance(c, (Plain, Image, File, Video, Record))
    ]


async def enrich_media(
    api: WppClient,
    msg: AstrBotMessage,
    *,
    include: set[str] | None = None,
    exclude: set[str] | None = None,
) -> list[str]:
    """就地补充消息链中的媒体组件, 返回需要登记清理的临时文件路径。

    include / exclude 用于选择性下载 (类型: image / file / voice / video):
      - 未 @ 的主动候选群消息: include={"voice"} 即时下载语音 (保证 STT),
        其余媒体延迟到真正调用 LLM 时再下载。
      - 懒加载钩子: exclude={"voice"} 避免重复下载语音。
    """
    temp_files: list[str] = []

    def _want(kind: str) -> bool:
        return (include is None or kind in include) and (exclude is None or kind not in exclude)

    image_meta = getattr(msg, "_wpp_image_meta", None)
    if image_meta and _want("image"):
        path = await _download_image(api, image_meta)
        prefix = _non_media_prefix(msg)
        if path:
            try:
                with open(path, "rb") as f:
                    msg.message = prefix + [Image.fromBytes(f.read())]
                msg.message_str = "[图片]"
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[WPP] 读取下载图片失败: {e}")
                msg.message = prefix + [Plain("[图片下载失败]")]
                msg.message_str = "[图片下载失败]"
            finally:
                _unlink(path)
        else:
            msg.message = prefix + [Plain("[图片下载失败]")]
            msg.message_str = "[图片下载失败]"

    file_meta = getattr(msg, "_wpp_file_meta", None)
    if file_meta and file_meta.get("attach_id") and _want("file"):
        try:
            data = await api.download_file_binary(
                file_meta.get("attach_id", ""),
                file_meta.get("user_name", ""),
                int(file_meta.get("data_len", 0) or 0),
            )
            if data:
                fname = str(file_meta.get("filename") or "file")
                path = _temp_path("file", _safe_ext(fname))
                with open(path, "wb") as f:
                    f.write(data)
                temp_files.append(path)
                hint = _skill_hint(_safe_ext(fname))
                msg.message_str = f"[文件] {fname}"
                msg.message = _non_media_prefix(msg) + [
                    Plain(f"[文件] {fname}{hint}"),
                    File(name=fname, file=path),
                ]
            else:
                logger.warning(f"[WPP] 文件下载失败: {file_meta.get('filename')}")
                msg.message_str = f"[文件] {file_meta.get('filename', '')} (下载失败)"
                msg.message = _non_media_prefix(msg) + [
                    Plain(f"[文件] {file_meta.get('filename', '')} (下载失败)")
                ]
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[WPP] 文件下载异常: {e}")
            msg.message_str = "[文件] (下载失败)"
            msg.message = _non_media_prefix(msg) + [Plain("[文件] (下载失败)")]

    voice_meta = getattr(msg, "_wpp_voice_meta", None)
    if voice_meta and voice_meta.get("msg_id") and _want("voice"):
        try:
            data = await api.download_voice_binary(
                str(voice_meta.get("msg_id", "")),
                new_msg_id=str(voice_meta.get("new_msg_id", "")),
                client_msg_id=str(voice_meta.get("client_msg_id", "")),
                master_buf_id=str(voice_meta.get("master_buf_id", "0") or "0"),
                length=int(voice_meta.get("length", 0) or 0),
                voice_format=int(voice_meta.get("format", 4) or 4),
                chat_room_name=str(voice_meta.get("chat_room_name", "")),
                from_user_name=str(voice_meta.get("from_user_name", "")),
                to_user_name=str(voice_meta.get("to_user_name", "")),
            )
            if data:
                path = _temp_path("voice", ".silk")
                with open(path, "wb") as f:
                    f.write(data)
                temp_files.append(path)
                msg.message_str = "[语音]"
                msg.message = _non_media_prefix(msg) + [Record(file=path)]
            else:
                logger.warning("[WPP] 语音下载失败")
                msg.message_str = "[语音下载失败]"
                msg.message = _non_media_prefix(msg) + [Plain("[语音] (下载失败)")]
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[WPP] 语音下载异常: {e}")
            msg.message_str = "[语音下载失败]"
            msg.message = _non_media_prefix(msg) + [Plain("[语音] (下载失败)")]

    video_meta = getattr(msg, "_wpp_video_meta", None)
    if video_meta and video_meta.get("msg_id") and _want("video"):
        try:
            data = await api.download_video(
                video_meta.get("msg_id", ""),
                video_meta.get("to_wxid", ""),
                int(video_meta.get("data_len", 0) or 0),
                int(video_meta.get("compress_type", 0) or 0),
            )
            if data:
                path = _temp_path("video", ".mp4")
                with open(path, "wb") as f:
                    f.write(data)
                temp_files.append(path)
                msg.message_str = "[视频]"
                msg.message = _non_media_prefix(msg) + [
                    Plain(f"[视频] {len(data)} bytes"),
                    Video.fromFileSystem(path),
                ]
            else:
                logger.warning("[WPP] 视频下载失败")
                msg.message_str = "[视频下载失败]"
                msg.message = _non_media_prefix(msg) + [Plain("[视频] (下载失败)")]
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[WPP] 视频下载异常: {e}")
            msg.message_str = "[视频下载失败]"
            msg.message = _non_media_prefix(msg) + [Plain("[视频] (下载失败)")]

    return temp_files


async def _download_image(api: WppClient, meta: dict) -> str | None:
    """下载图片到临时文件, 返回路径 (失败 None)。"""
    data: bytes | None = None
    cdn = meta.get("cdn")
    if cdn:
        data = await api.download_image_cdn(cdn.get("file_aes_key", ""), cdn.get("file_no", ""))
    if not data:
        data = await api.download_image(
            meta.get("msg_id", ""),
            meta.get("to_wxid", ""),
            int(meta.get("data_len", 0) or 0),
        )
    if not data:
        return None
    if len(data) > MAX_FILE_BYTES:
        logger.warning(f"[WPP] 图片过大, 跳过: {len(data)} bytes")
        return None
    path = _temp_path("image", ".jpg")
    with open(path, "wb") as f:
        f.write(data)
    return path


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


__all__ = ["enrich_media", "MAX_FILE_BYTES"]
