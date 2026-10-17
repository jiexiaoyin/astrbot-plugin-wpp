"""WPP 出站消息链发送 (event.send / send_by_session 共用)。

AstrBot 规范:
  - 平台适配器应遍历 MessageChain 的组件, 用组件的 `convert_to_file_path()` 解析媒体,
    而不是自己猜 `file` / `url` 的格式 (见 docs/zh/dev/plugin-platform-adapter.md)
  - 长文本按 markdown-aware 规则分片后逐条发送 (vendor SendTxt 超长会失败/截断)

支持组件: Plain / At / AtAll / Image / File / Record / Video / Reply / Share / Location。
Reply 引用回复走 appmsg XML (type=57); Share 走链接卡片; Location 走位置卡片。
"""

from __future__ import annotations

import os
import re
from typing import Any

from astrbot import logger
from astrbot.api.message_components import (
    At,
    File,
    Image,
    Location,
    Plain,
    Record,
    Reply,
    Share,
    Video,
)

from .wpp_client import WppClient

# 单条文本上限。wpp-openclaw 实测 6000 字符可用
TEXT_CHUNK_LIMIT = 6000
# 引用回复气泡文本上限 (wpp-openclaw buildQuoteReplyXml 取 500)
QUOTE_TEXT_LIMIT = 500
MAX_MEDIA_SEND_BYTES = 100 * 1024 * 1024


# --------------------------------------------------------------------------- 分片
def chunk_text(text: str, limit: int = TEXT_CHUNK_LIMIT) -> list[str]:
    """markdown-aware 文本分片。

    规则 (参考 wpp-openclaw chunkMarkdown):
      1. 优先按段落 (空行) 切
      2. 长段落按行切
      3. 单行超长 → 按码点硬切 (不切坏 emoji)
      4. ``` 代码块尽量保持完整 (超过 2x limit 才强制切)
    """
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    out: list[str] = []
    in_code = False
    blocks: list[str] = []
    buf = ""
    for line in text.split("\n"):
        if re.match(r"^\s*```", line):
            in_code = not in_code
        if not in_code and re.match(r"^\s*$", line):
            blocks.append(buf)
            buf = ""
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        blocks.append(buf)

    current = ""

    def _flush() -> None:
        nonlocal current
        if current:
            out.append(current)
            current = ""

    for para in (b.strip() for b in blocks):
        if not para:
            continue
        if len(para) <= limit:
            if current and len(current) + len(para) + 2 > limit:
                _flush()
            current = f"{current}\n\n{para}" if current else para
            continue
        _flush()
        out.extend(_chunk_long_paragraph(para, limit))

    _flush()
    return out


def _chunk_long_paragraph(text: str, limit: int) -> list[str]:
    out: list[str] = []
    current = ""
    in_code = False
    for line in text.split("\n"):
        if line.startswith("```"):
            in_code = not in_code
        if len(line) > limit:
            if current:
                out.append(current)
                current = ""
            out.extend(_hard_split(line, limit))
            continue
        sep = "\n" if current else ""
        would_exceed = len(current) + len(sep) + len(line) > limit
        # 代码块内放宽到 2x, 尽量不切断
        if in_code and current and len(current) + len(sep) + len(line) <= limit * 2:
            current = f"{current}{sep}{line}"
        elif would_exceed:
            if current:
                out.append(current)
            current = line
        else:
            current = f"{current}{sep}{line}"
    if current:
        out.append(current)
    return out


def _hard_split(line: str, limit: int) -> list[str]:
    """按 Unicode 码点切分, 避免切断 emoji/代理对。"""
    out: list[str] = []
    buf: list[str] = []
    size = 0
    for ch in line:
        clen = len(ch)
        if size + clen > limit:
            out.append("".join(buf))
            buf = []
            size = 0
        buf.append(ch)
        size += clen
    if buf:
        out.append("".join(buf))
    return out


# --------------------------------------------------------------------------- 发送
async def send_chain(
    api: WppClient,
    to_wxid: str,
    chain: list[Any],
    account_id: str = "",
) -> None:
    """按消息链顺序发送。单个组件失败只记日志, 不中断后续组件。"""
    text_buf: list[str] = []
    ats: list[At] = []
    pending_reply: Reply | None = None

    async def flush_text() -> None:
        nonlocal ats, pending_reply
        if not text_buf:
            ats = []
            pending_reply = None
            return
        text = "\n".join(t for t in text_buf if t)
        text_buf.clear()

        # 引用回复: Reply 后紧跟的短文本合并成 type=57 appmsg 一次发送
        if pending_reply is not None and text and len(text) <= QUOTE_TEXT_LIMIT:
            try:
                # 群聊引用回复没有 At 字段, 与 wpp-openclaw 一致: 正文前加
                # "@昵称 " 让被引用成员收到提醒 (核心 reply_with_mention
                # 会先往链里插 At, 这里把它合并进引用气泡)。
                quote_text = _quote_text_with_mention(to_wxid, text, ats)
                xml = build_quote_reply_xml(
                    quote_text,
                    getattr(pending_reply, "id", ""),
                    getattr(pending_reply, "sender_id", ""),
                )
                await api.send_app_xml(to_wxid, xml)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[WPP:{account_id}] send_quote failed: {e}")
            pending_reply = None
            ats = []
            return

        at_ids = [str(getattr(a, "qq", "")) for a in ats if str(getattr(a, "qq", ""))]
        first = True
        for chunk in chunk_text(text):
            try:
                await api.send_text(to_wxid, chunk, ats=at_ids if first else None)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[WPP:{account_id}] send_text failed: {e}")
            first = False
        ats = []
        pending_reply = None

    for comp in chain or []:
        if isinstance(comp, Plain):
            text_buf.append(comp.text or "")
        elif isinstance(comp, At):
            # AtAll 是 At 子类 (qq="all"), vendor At 字段接受 "all"
            qq = str(getattr(comp, "qq", "") or "")
            if qq:
                ats.append(comp)
        elif isinstance(comp, Reply):
            # 删除前一个 Reply, 只保留最后一个 (链中通常只有一个)
            pending_reply = comp
        elif isinstance(comp, Share):
            await flush_text()
            await _send_share(api, to_wxid, comp, account_id)
        elif isinstance(comp, Location):
            await flush_text()
            await _send_location(api, to_wxid, comp, account_id)
        elif isinstance(comp, Image):
            await flush_text()
            await _send_image(api, to_wxid, comp, account_id)
        elif isinstance(comp, File):
            await flush_text()
            await _send_file(api, to_wxid, comp, account_id)
        elif isinstance(comp, Record):
            await flush_text()
            await _send_record(api, to_wxid, comp, account_id)
        elif isinstance(comp, Video):
            await flush_text()
            await _send_video(api, to_wxid, comp, account_id)
        else:
            logger.debug(f"[WPP:{account_id}] 未支持的出站组件: {type(comp).__name__}")

    await flush_text()


def _quote_text_with_mention(to_wxid: str, text: str, ats: list[At]) -> str:
    """群聊引用回复: 把核心插入的 At 合并为正文的 "@昵称 " 前缀。

    WeChat 的引用气泡 (appmsg type=57) 走 /Msg/ShareLink, 没有 At 字段,
    因此与 wpp-openclaw 的 buildGroupMentionPrefix 一致, 用正文前缀提醒。
    私聊 / 无昵称时保持原文。
    """
    if not to_wxid.endswith("@chatroom") or not ats:
        return text
    at = ats[0]
    name = str(getattr(at, "name", "") or "").strip()
    qq = str(getattr(at, "qq", "") or "").strip()
    if not name or name == qq:
        return text
    prefix = f"@{name}"
    if text.startswith(prefix):
        return text
    return f"{prefix} {text}".strip()


def _escape_xml(value: Any) -> str:
    return (
        str(value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def build_quote_reply_xml(
    content: str,
    reply_id: str | int | None,
    sender_id: str | int | None = "",
) -> str:
    """构造引用回复 appmsg (type=57)。

    与 wpp-openclaw buildQuoteReplyXml 一致: 客户端主气泡读 `<title>`,
    refermsg 极简化仅需 svrid (fromusr 可选, 兼容老客户端回查昵称)。
    """
    text = _escape_xml((content or "引用回复").strip()[:QUOTE_TEXT_LIMIT])
    svrid = str(reply_id or "").strip()
    fromusr = str(sender_id or "").strip()
    if fromusr in ("0", "None"):
        fromusr = ""
    refer = ""
    if svrid:
        refer = f"<refermsg><svrid>{_escape_xml(svrid)}</svrid>"
        if fromusr:
            refer += f"<fromusr>{_escape_xml(fromusr)}</fromusr>"
        refer += "</refermsg>"
    return f"<appmsg><title>{text}</title><des>{text}</des><type>57</type>{refer}</appmsg>"


def build_share_link_xml(
    title: str,
    desc: str = "",
    url: str = "",
    thumb_url: str = "",
) -> str:
    """构造链接卡片 appmsg (type=5)。"""
    thumb = f"<thumburl>{_escape_xml(thumb_url)}</thumburl>" if thumb_url else ""
    return (
        f"<appmsg><title>{_escape_xml(title)}</title>"
        f"<des>{_escape_xml(desc)}</des><type>5</type>"
        f"<url>{_escape_xml(url)}</url>{thumb}<appattach></appattach></appmsg>"
    )


async def _send_share(api: WppClient, to_wxid: str, comp: Share, account_id: str) -> None:
    try:
        xml = build_share_link_xml(
            getattr(comp, "title", "") or getattr(comp, "url", ""),
            getattr(comp, "content", "") or "",
            getattr(comp, "url", "") or "",
            getattr(comp, "image", "") or "",
        )
        await api.send_app_xml(to_wxid, xml)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[WPP:{account_id}] send_share failed: {e}")


async def _send_location(
    api: WppClient, to_wxid: str, comp: Location, account_id: str
) -> None:
    try:
        await api.send_location(
            to_wxid,
            float(getattr(comp, "lat", 0) or 0),
            float(getattr(comp, "lon", 0) or 0),
            str(getattr(comp, "content", "") or getattr(comp, "title", "") or ""),
            str(getattr(comp, "title", "") or ""),
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[WPP:{account_id}] send_location failed: {e}")


async def _send_image(api: WppClient, to_wxid: str, comp: Image, account_id: str) -> None:
    try:
        path = await comp.convert_to_file_path()
        await api.send_image(to_wxid, path)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[WPP:{account_id}] send_image failed: {e}")


async def _read_bytes(path: str) -> bytes | None:
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    if size > MAX_MEDIA_SEND_BYTES:
        logger.warning(f"[WPP] 媒体文件过大, 跳过发送: {size} bytes")
        return None
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError as e:
        logger.warning(f"[WPP] 读取待发送媒体失败: {e}")
        return None


async def _send_file(api: WppClient, to_wxid: str, comp: File, account_id: str) -> None:
    try:
        path = await comp.get_file()
        if not path:
            logger.warning(f"[WPP:{account_id}] File 组件没有可用路径/URL, 跳过")
            return
        data = await _read_bytes(path)
        if data is None:
            return
        await api.send_file(to_wxid, os.path.basename(path), data)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[WPP:{account_id}] send_file failed: {e}")


async def _send_video(api: WppClient, to_wxid: str, comp: Video, account_id: str) -> None:
    try:
        path = await comp.convert_to_file_path()
        data = await _read_bytes(path)
        if data is None:
            return
        await api.send_video(to_wxid, data)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[WPP:{account_id}] send_video failed: {e}")


async def _send_record(api: WppClient, to_wxid: str, comp: Record, account_id: str) -> None:
    try:
        path = await comp.convert_to_file_path()
        data = await _read_bytes(path)
        if data is None:
            return
        ext = os.path.splitext(path)[1].lower()
        type_map = {".amr": 0, ".speex": 1, ".mp3": 2, ".wav": 3, ".silk": 4}
        fmt = type_map.get(ext, 3)
        voice_time = _wav_duration_ms(path) if ext == ".wav" else 0
        await api.send_voice(to_wxid, data, fmt_type=fmt, voice_time_ms=voice_time)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[WPP:{account_id}] send_voice failed: {e}")


def _wav_duration_ms(path: str) -> int:
    try:
        import wave

        with wave.open(path, "rb") as w:
            rate = w.getframerate() or 1
            return int(w.getnframes() / rate * 1000)
    except Exception:  # noqa: BLE001
        return 0


__all__ = [
    "TEXT_CHUNK_LIMIT",
    "QUOTE_TEXT_LIMIT",
    "build_quote_reply_xml",
    "build_share_link_xml",
    "chunk_text",
    "send_chain",
]
