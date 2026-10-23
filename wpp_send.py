"""WPP 出站消息链发送 (event.send / send_by_session 共用)。

AstrBot 规范:
  - 平台适配器应遍历 MessageChain 的组件, 用组件的 `convert_to_file_path()` 解析媒体,
    而不是自己猜 `file` / `url` 的格式 (见 docs/zh/dev/plugin-platform-adapter.md)
  - 长文本按 markdown-aware 规则分片后逐条发送 (vendor SendTxt 超长会失败/截断)

支持组件: Plain / At / AtAll / Image / File / Record / Video / Reply / Share / Location。
Reply 引用回复走 appmsg XML (type=57); Share 走链接卡片; Location 走位置卡片。
"""

from __future__ import annotations

import asyncio
import io
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
# vendor /Msg/SendVoice 对 Base64 解码后的语音 payload 有硬上限, 实测阈值约
# 65,000B (64,974B 成功 / 65,180B 失败, 失败响应里 Offset=65000), 与格式无关。
# 取 64,500 留余量, 避免卡在 chunk 边界。
VOICE_MAX_BYTES = 64_500
# 微信语音单条上限 60 秒; 超过则自动分段发送
VOICE_MAX_MS = 60_000
# SILK 码率阶梯: 微信/厂商实测 6k~24kbps 均可正常播放, 按容量从高到低选,
# 让 ≤60s 的分段尽量以高码率发进 vendor 的 64,500B payload 上限
SILK_BIT_RATES = (24_000, 16_000, 12_000, 10_000, 9_000, 8_000, 6_000)


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
) -> dict[str, Any]:
    """按消息链顺序发送。单个组件失败只记日志, 不中断后续组件。

    同一会话的整条链会持有会话级锁 (WppClient.send_lock), 防止两条回复的
    分片交错; 真正的 HTTP 发送间隔由 WppClient._post 的节流器保证。
    返回 {"sent": 成功次数, "failed": 失败次数, "errors": [...]}。
    """
    lock_factory = getattr(api, "send_lock", None)
    lock = lock_factory(to_wxid) if callable(lock_factory) else None
    if lock is None:
        return await _send_chain_locked(api, to_wxid, chain, account_id)
    async with lock:
        return await _send_chain_locked(api, to_wxid, chain, account_id)


async def _send_chain_locked(
    api: WppClient,
    to_wxid: str,
    chain: list[Any],
    account_id: str,
) -> dict[str, Any]:
    stats: dict[str, Any] = {"sent": 0, "failed": 0, "errors": []}
    text_buf: list[str] = []
    ats: list[At] = []
    pending_reply: Reply | None = None

    async def call_text(text: str, at_ids: list[str] | None = None) -> bool:
        try:
            await api.send_text(to_wxid, text, ats=at_ids)
        except Exception as e:  # noqa: BLE001
            stats["failed"] += 1
            stats["errors"].append(f"send_text: {e}")
            logger.warning(f"[WPP:{account_id}] send_text failed: {e}")
            return False
        stats["sent"] += 1
        return True

    async def flush_text() -> None:
        nonlocal ats, pending_reply
        if not text_buf:
            ats = []
            pending_reply = None
            return
        text = "\n".join(t for t in text_buf if t)
        text_buf.clear()

        # 引用回复: Reply 后紧跟的文本优先走 type=57 appmsg。
        # 超过 500 字符时: 引用气泡放第一段 (@昵称 + head), 剩余内容
        # 作为普通文本续发, 保证既不丢引用/@, 也不截断长回复。
        if pending_reply is not None and text:
            mention_name = await _resolve_quote_mention_name(
                api, to_wxid, ats, pending_reply
            )
            prefix = _mention_prefix(to_wxid, ats, mention_name)
            budget = QUOTE_TEXT_LIMIT - len(prefix) - 1 if prefix else QUOTE_TEXT_LIMIT
            head, tail = split_quote_text(text, max(1, budget))
            # 群聊引用气泡没有 At 字段: 正文前加 "@昵称 " 提醒被引用成员。
            quote_text = _quote_text_with_mention(
                to_wxid, head, ats, mention_name=mention_name
            )
            xml = build_quote_reply_xml(
                quote_text,
                getattr(pending_reply, "id", ""),
                getattr(pending_reply, "sender_id", ""),
                display_name=mention_name
                or str(getattr(pending_reply, "sender_nickname", "") or ""),
                chat_user=to_wxid,
                original_content=str(
                    getattr(pending_reply, "message_str", "")
                    or getattr(pending_reply, "text", "")
                    or ""
                ),
                create_time=getattr(pending_reply, "time", 0) or 0,
            )
            try:
                await api.send_app_xml(to_wxid, xml)
            except Exception as e:  # noqa: BLE001
                # 引用发送失败时不要吞掉正文: 退回普通文本路径重发完整内容。
                stats["failed"] += 1
                stats["errors"].append(f"send_quote: {e}")
                logger.warning(
                    f"[WPP:{account_id}] send_quote failed, 回退普通文本: {e}"
                )
            else:
                stats["sent"] += 1
                pending_reply = None
                ats = []
                if tail:
                    for chunk in chunk_text(tail):
                        await call_text(chunk)
                return

        at_ids = [str(getattr(a, "qq", "")) for a in ats if str(getattr(a, "qq", ""))]
        first = True
        for chunk in chunk_text(text):
            await call_text(chunk, at_ids if first else None)
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
            stats = _merge_send_stats(
                stats, await _send_share(api, to_wxid, comp, account_id)
            )
        elif isinstance(comp, Location):
            await flush_text()
            stats = _merge_send_stats(
                stats, await _send_location(api, to_wxid, comp, account_id)
            )
        elif isinstance(comp, Image):
            await flush_text()
            stats = _merge_send_stats(
                stats, await _send_image(api, to_wxid, comp, account_id)
            )
        elif isinstance(comp, File):
            await flush_text()
            stats = _merge_send_stats(
                stats, await _send_file(api, to_wxid, comp, account_id)
            )
        elif isinstance(comp, Record):
            await flush_text()
            stats = _merge_send_stats(
                stats, await _send_record(api, to_wxid, comp, account_id)
            )
        elif isinstance(comp, Video):
            await flush_text()
            stats = _merge_send_stats(
                stats, await _send_video(api, to_wxid, comp, account_id)
            )
        else:
            logger.debug(f"[WPP:{account_id}] 未支持的出站组件: {type(comp).__name__}")

    await flush_text()
    return stats


def _merge_send_stats(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    base["sent"] += int(extra.get("sent", 0))
    base["failed"] += int(extra.get("failed", 0))
    base["errors"].extend(extra.get("errors", []))
    return base


def split_quote_text(text: str, limit: int = QUOTE_TEXT_LIMIT) -> tuple[str, str]:
    """把长回复切成 (引用气泡 head, 续发 tail)。

    head 必须是 text 的前缀, tail 是无损剩余部分。优先在段落/换行/句末切,
    避免把一句话从中间截断; 找不到合适边界时按码点硬切。
    """
    if not text:
        return "", ""
    if len(text) <= limit:
        return text, ""
    window = text[:limit]
    cut = limit
    for sep in ("\n\n", "\n", "。", "！", "？", "；", "，", " "):
        idx = window.rfind(sep)
        if idx >= max(10, limit // 3):
            cut = idx + len(sep)
            break
    head = text[:cut].strip()
    if not head:
        head = text[:limit]
        cut = limit
    return head, text[cut:].strip()


# 昵称查询不能拖慢回复: 超时后直接退回 wxid
NICKNAME_RESOLVE_TIMEOUT = 5.0


async def _resolve_quote_mention_name(
    api: WppClient, to_wxid: str, ats: list[At], reply: Reply | None
) -> str:
    """群聊引用回复的 @ 名称: At.name → Reply.sender_nickname → vendor 查询 → wxid。

    入站消息常缺 sender 昵称, 适配器会用 wxid 兜底, 导致 At.name == At.qq;
    此时查群成员列表拿真实昵称。查不到就退回 wxid, 与 wpp-openclaw 的
    buildGroupMentionPrefix (`displayName || fromusr`) 行为一致。
    """
    if not to_wxid.endswith("@chatroom") or not ats:
        return ""
    at = ats[0]
    qq = str(getattr(at, "qq", "") or "").strip()
    if not qq or qq == "all":
        return ""
    for candidate in (
        str(getattr(at, "name", "") or "").strip(),
        str(getattr(reply, "sender_nickname", "") or "").strip(),
    ):
        if candidate and candidate != qq:
            return candidate

    resolver = getattr(api, "resolve_nickname", None)
    if callable(resolver):
        try:
            nick = await asyncio.wait_for(
                resolver(qq, to_wxid), timeout=NICKNAME_RESOLVE_TIMEOUT
            )
        except Exception as e:  # noqa: BLE001 - 昵称查询失败不影响发送
            logger.debug(f"[WPP] 引用回复昵称解析失败 {qq}: {e}")
            nick = ""
        nick = str(nick or "").strip()
        if nick:
            return nick
    return qq


def _mention_prefix(to_wxid: str, ats: list[At], mention_name: str = "") -> str:
    """群聊引用气泡需要的前缀 "@昵称"; 私聊/@全体返回空串。"""
    if not to_wxid.endswith("@chatroom") or not ats:
        return ""
    at = ats[0]
    qq = str(getattr(at, "qq", "") or "").strip()
    if not qq or qq == "all":
        return ""
    name = str(mention_name or "").strip() or str(getattr(at, "name", "") or "").strip()
    if not name:
        name = qq
    if not name or name == "all":
        return ""
    return f"@{name}"


def _quote_text_with_mention(
    to_wxid: str, text: str, ats: list[At], mention_name: str = ""
) -> str:
    """群聊引用回复: 把核心插入的 At 合并为正文的 "@昵称 " 前缀。

    WeChat 的引用气泡 (appmsg type=57) 走 /Msg/ShareLink, 没有 At 字段,
    因此与 wpp-openclaw 的 buildGroupMentionPrefix 一致, 用正文前缀提醒。
    私聊 / @全体 时不加前缀。
    """
    prefix = _mention_prefix(to_wxid, ats, mention_name)
    if not prefix:
        return text
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
    *,
    display_name: str = "",
    chat_user: str = "",
    original_content: str = "",
    create_time: int | float | str = 0,
) -> str:
    """构造引用回复 appmsg (type=57)。

    客户端主气泡读 `<title>`; refermsg 的 svrid 是必须的, fromusr 兼容老客户端
    回查昵称。v0.7.0 在可用时补充 chatusr/displayname/content/createtime
    (与 wpp-openclaw 完整 refermsg 结构一致), 缺失字段自动省略, 不影响
    最小 svrid 引用。
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
        chatusr = str(chat_user or "").strip()
        if chatusr:
            refer += f"<chatusr>{_escape_xml(chatusr)}</chatusr>"
        name = str(display_name or "").strip()
        if name:
            refer += f"<displayname>{_escape_xml(name)}</displayname>"
        original = str(original_content or "").strip()
        if original:
            refer += f"<content>{_escape_xml(original[:1000])}</content>"
        try:
            ts = int(float(create_time or 0))
        except (TypeError, ValueError):
            ts = 0
        if ts > 0:
            refer += f"<createtime>{ts}</createtime>"
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


def _one_result(ok: bool, error: str = "") -> dict[str, Any]:
    return {"sent": 1 if ok else 0, "failed": 0 if ok else 1, "errors": [error] if error else []}


async def _send_share(
    api: WppClient, to_wxid: str, comp: Share, account_id: str
) -> dict[str, Any]:
    try:
        xml = build_share_link_xml(
            getattr(comp, "title", "") or getattr(comp, "url", ""),
            getattr(comp, "content", "") or "",
            getattr(comp, "url", "") or "",
            getattr(comp, "image", "") or "",
        )
        await api.send_app_xml(to_wxid, xml)
        return _one_result(True)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[WPP:{account_id}] send_share failed: {e}")
        return _one_result(False, f"send_share: {e}")


async def _send_location(
    api: WppClient, to_wxid: str, comp: Location, account_id: str
) -> dict[str, Any]:
    try:
        await api.send_location(
            to_wxid,
            float(getattr(comp, "lat", 0) or 0),
            float(getattr(comp, "lon", 0) or 0),
            str(getattr(comp, "content", "") or getattr(comp, "title", "") or ""),
            str(getattr(comp, "title", "") or ""),
        )
        return _one_result(True)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[WPP:{account_id}] send_location failed: {e}")
        return _one_result(False, f"send_location: {e}")


async def _send_image(
    api: WppClient, to_wxid: str, comp: Image, account_id: str
) -> dict[str, Any]:
    try:
        path = await comp.convert_to_file_path()
        await api.send_image(to_wxid, path)
        return _one_result(True)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[WPP:{account_id}] send_image failed: {e}")
        return _one_result(False, f"send_image: {e}")


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


async def _send_file(
    api: WppClient, to_wxid: str, comp: File, account_id: str
) -> dict[str, Any]:
    try:
        path = await comp.get_file()
        if not path:
            logger.warning(f"[WPP:{account_id}] File 组件没有可用路径/URL, 跳过")
            return _one_result(False, "send_file: no path")
        data = await _read_bytes(path)
        if data is None:
            return _one_result(False, "send_file: unreadable")
        await api.send_file(to_wxid, os.path.basename(path), data)
        return _one_result(True)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[WPP:{account_id}] send_file failed: {e}")
        return _one_result(False, f"send_file: {e}")


async def _send_video(
    api: WppClient, to_wxid: str, comp: Video, account_id: str
) -> dict[str, Any]:
    try:
        path = await comp.convert_to_file_path()
        data = await _read_bytes(path)
        if data is None:
            return _one_result(False, "send_video: unreadable")
        await api.send_video(to_wxid, data)
        return _one_result(True)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[WPP:{account_id}] send_video failed: {e}")
        return _one_result(False, f"send_video: {e}")


async def _send_record(
    api: WppClient, to_wxid: str, comp: Record, account_id: str
) -> dict[str, Any]:
    # TTS 回复会带上原文 (Record.text)。语音发不出去时降级发文字，
    # 而不是让整条回复静默丢失；转发/用户语音没有原文，仍走文件或报错。
    fallback_text = str(getattr(comp, "text", "") or "").strip()

    async def _fallback(reason: str) -> dict[str, Any] | None:
        if not fallback_text:
            return None
        try:
            await api.send_text(to_wxid, fallback_text)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[WPP:{account_id}] {reason}; 文字降级也失败: {e}")
            return _one_result(False, f"send_voice fallback text: {e}")
        logger.info(f"[WPP:{account_id}] {reason}; 已降级发送文字")
        return _one_result(True)

    try:
        path = await comp.convert_to_file_path()
        data = await _read_bytes(path)
        if data is None:
            fb = await _fallback("语音文件不可读")
            return fb or _one_result(False, "send_voice: unreadable")
        ext = os.path.splitext(path)[1].lower()
        type_map = {".amr": 0, ".speex": 1, ".mp3": 2, ".wav": 3, ".silk": 4}
        fmt = type_map.get(ext, 3)
        # vendor 对 VoiceTime=0 直接返回 Code=-2，必须带真实时长 (mp3 也一样)
        voice_time = await _audio_duration_ms(path, ext)
        if voice_time <= 0:
            logger.warning(
                f"[WPP:{account_id}] 无法获取语音时长, vendor 可能拒发: {path}"
            )
        # vendor 还对语音 payload > 64,500B 直接返回 Code=-2。统一转微信原生
        # SILK, 单条 ≤60s; 码率按 24k→6k 阶梯选最高的能塞进 64,500B 的
        # (实测 6k/8k/9k 在微信端均可正常播放)。
        segments = await _encode_silk_segments(path)
        if segments:
            if len(segments) > 1:
                logger.info(
                    f"[WPP:{account_id}] 语音 {voice_time}ms 分 {len(segments)} 段发送"
                    f" (SILK, 单段 ≤{VOICE_MAX_MS // 1000}s)"
                )
            for silk, seg_ms in segments:
                await api.send_voice(
                    to_wxid, silk, fmt_type=4, voice_time_ms=seg_ms
                )
            return _one_result(True)
        if len(data) > VOICE_MAX_BYTES:
            fb = await _fallback(
                f"语音 {len(data)}B 超过 vendor 64,500B 限制且无法转 SILK"
            )
            if fb is not None:
                return fb
            logger.warning(
                f"[WPP:{account_id}] 语音 {len(data)}B 超过 vendor 64,500B 限制且无法转 "
                f"SILK, 降级为文件: {path}"
            )
            await api.send_file(to_wxid, os.path.basename(path), data)
            return _one_result(True)
        await api.send_voice(to_wxid, data, fmt_type=fmt, voice_time_ms=voice_time)
        return _one_result(True)
    except Exception as e:  # noqa: BLE001
        fb = await _fallback(f"send_voice failed: {e}")
        if fb is not None:
            return fb
        logger.warning(f"[WPP:{account_id}] send_voice failed: {e}")
        return _one_result(False, f"send_voice: {e}")


async def _encode_silk_segments(path: str) -> list[tuple[bytes, int]]:
    """按 ≤60s 切片并转微信原生 SILK (Tencent 标记)。

    每段从 24kbps 起按阶梯降码率, 选第一个 payload ≤64,500B 的;
    实测 6k/8k/9k 在微信端均可正常播放。若 6kbps 仍超限
    (极端长段/高熵音频), 按 4/5 收缩分段长度后重试。
    pysilk/ffmpeg 不可用或编码失败时返回空列表, 由调用方走原格式兜底。
    """
    try:
        import pysilk
    except ImportError:
        return []
    pcm = await _decode_pcm_24k(path)
    if not pcm:
        return []
    rate = 24000
    total_samples = len(pcm) // 2
    max_ms = VOICE_MAX_MS
    while max_ms >= 1000:
        max_samples = rate * max_ms // 1000
        segments: list[tuple[bytes, int]] = []
        pos = 0
        ok = True
        while pos < total_samples:
            end = min(pos + max_samples, total_samples)
            chunk = pcm[pos * 2 : end * 2]
            seg_ms = int((end - pos) * 1000 / rate)
            encoded = _silk_encode_fit(pysilk, chunk)
            if encoded is None:
                ok = False
                break
            segments.append((encoded, seg_ms))
            pos = end
        if ok:
            return segments
        logger.warning(
            f"[WPP] SILK 分段 {max_ms}ms 在 6kbps 下仍超 64,500B, 收缩分段后重试"
        )
        max_ms = max_ms * 4 // 5
    logger.warning("[WPP] SILK 分段编码失败, 放弃 SILK")
    return []


def _silk_encode_fit(pysilk: Any, pcm: bytes) -> bytes | None:
    """从高到低尝试码率, 返回第一个 ≤64,500B 的 SILK payload。"""
    for bit_rate in SILK_BIT_RATES:
        out = io.BytesIO()
        try:
            pysilk.encode(
                io.BytesIO(pcm),
                out,
                24000,
                bit_rate,
                24000,
                0,
                2,
                False,
                False,
                True,
            )
        except Exception:  # noqa: BLE001
            continue
        data = out.getvalue()
        if data and len(data) <= VOICE_MAX_BYTES:
            return data
    return None


async def _decode_pcm_24k(path: str) -> bytes:
    """用 ffmpeg 解码成 24kHz 单声道 s16le PCM。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-v",
            "error",
            "-i",
            path,
            "-f",
            "s16le",
            "-ac",
            "1",
            "-ar",
            "24000",
            "-",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        return stdout if proc.returncode == 0 else b""
    except Exception:  # noqa: BLE001
        return b""


async def _audio_duration_ms(path: str, ext: str) -> int:
    """拿语音时长 (毫秒)。ffprobe 优先, wav/mp3 有内置兜底。"""
    if ext == ".wav":
        ms = _wav_duration_ms(path)
        if ms > 0:
            return ms
    ms = await _ffprobe_duration_ms(path)
    if ms > 0:
        return ms
    if ext == ".mp3":
        return _mp3_duration_ms(path)
    return 0


async def _ffprobe_duration_ms(path: str) -> int:
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0 and stdout.strip():
            return int(float(stdout.decode().strip()) * 1000)
    except Exception:  # noqa: BLE001 - 没有 ffprobe 时走内置解析
        pass
    return 0


def _mp3_duration_ms(path: str) -> int:
    """纯 Python 解析 MPEG Layer III 帧时长 (ffprobe 不可用时的兜底)。"""
    try:
        with open(path, "rb") as f:
            data = f.read()
        pos = 0
        if len(data) >= 10 and data[:3] == b"ID3":
            size = (
                ((data[6] & 0x7F) << 21)
                | ((data[7] & 0x7F) << 14)
                | ((data[8] & 0x7F) << 7)
                | (data[9] & 0x7F)
            )
            pos = 10 + size
        bitrates = {
            1: (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
            2: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
        }
        sample_rates = {
            1: (44100, 48000, 32000),
            2: (22050, 24000, 16000),
            25: (11025, 12000, 8000),
        }
        total_ms = 0.0
        frames = 0
        n = len(data)
        while pos + 4 <= n:
            if data[pos] != 0xFF or (data[pos + 1] & 0xE0) != 0xE0:
                pos += 1
                continue
            version = {3: 1, 2: 2, 0: 25}.get((data[pos + 1] >> 3) & 0x03)
            layer = (data[pos + 1] >> 1) & 0x03
            br_index = (data[pos + 2] >> 4) & 0x0F
            sr_index = (data[pos + 2] >> 2) & 0x03
            if version is None or layer != 1 or not 0 < br_index < 15 or sr_index == 3:
                pos += 1
                continue
            bitrate = bitrates[1 if version == 1 else 2][br_index] * 1000
            sample_rate = sample_rates[version][sr_index]
            padding = (data[pos + 2] >> 1) & 0x01
            samples = 1152 if version == 1 else 576
            frame_size = (samples // 8) * bitrate // sample_rate + padding
            if frame_size <= 4:
                pos += 1
                continue
            total_ms += samples / sample_rate * 1000
            frames += 1
            pos += frame_size
        return int(total_ms) if frames else 0
    except Exception:  # noqa: BLE001
        return 0


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
    "split_quote_text",
]
