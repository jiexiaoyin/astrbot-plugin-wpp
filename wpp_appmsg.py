"""微信 appmsg / XML 卡片解析 (纯函数, 无网络/无文件 IO)。

背景:
  vendor 的 `msgType=49` 承载了链接、文件、位置、小程序、名片、引用、合并转发、
  接龙、红包等大量“卡片消息”。旧实现只把 `content` 原样塞成 `Plain`，导致:
    - 原始 XML 进入 LLM (token 爆炸 + prompt injection 面)
    - 只有 app 元数据、content 为空的卡片被整条丢弃
    - 文件卡片字段读不全，下载不到附件

本模块的目标:
  1. 优先读 vendor 的结构化 `app` 块 (v1 schema)
  2. 结构缺失时回退解析 `<appmsg>` XML
  3. 统一输出可读摘要，绝不把原始 XML 直接交给 LLM
  4. 解析结果同时供 Reply/At/位置/名片等组件使用

所有函数都是纯函数，方便在容器内直接 pytest。
"""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from typing import Any

MAX_XML_BYTES = 256 * 1024
MAX_SUMMARY_CHARS = 4096
MAX_ITEMS = 20
MAX_ITEM_CHARS = 200

_XML_DECL_RE = re.compile(r"^<\?xml[^>]*\?>", re.I)
_COMMENT_RE = re.compile(r"^<!--.*?-->", re.S)
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_AMP_RE = re.compile(r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_TAG_RE = re.compile(r"<[^>]+>")


def safe_str(v: Any) -> str:
    return "" if v is None else str(v).strip()


def _to_int(v: Any) -> int | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return None


def _local(tag: str) -> str:
    """去掉 ElementTree 的 `{namespace}` 前缀。"""
    return tag.rsplit("}", 1)[-1].lower()


def app_payload(src: dict) -> dict:
    """取 vendor 的结构化 app 块 (兼容 app/appMsg/appmsg)。"""
    if not isinstance(src, dict):
        return {}
    for key in ("app", "appMsg", "appmsg"):
        obj = src.get(key)
        if isinstance(obj, dict):
            return obj
    return {}


def app_category(src: dict) -> str:
    return safe_str(app_payload(src).get("category")).lower()


def appmsg_type(src: dict) -> int | None:
    app = app_payload(src)
    for key in ("type", "appmsg_type", "appmsgType"):
        t = _to_int(app.get(key))
        if t is not None:
            return t
    root = parse_xml(app_xml(src))
    if root is not None:
        return _to_int(find_text(root, "type"))
    return None


def app_xml(src: dict) -> str:
    """取卡片 XML 原文 (content / raw_xml / app.xml)，并做长度与转义处理。"""
    if not isinstance(src, dict):
        return ""
    app = app_payload(src)
    for key in ("raw_xml", "xml", "rawXml"):
        val = src.get(key)
        if isinstance(val, str) and val.strip():
            return _prepare_xml(val)
        val = app.get(key)
        if isinstance(val, str) and val.strip():
            return _prepare_xml(val)
    val = src.get("content") or src.get("Content")
    if isinstance(val, str) and val.strip():
        return _prepare_xml(val)
    return ""


def _prepare_xml(text: str) -> str:
    text = _CTRL_RE.sub("", text or "")
    if len(text) > MAX_XML_BYTES:
        text = text[:MAX_XML_BYTES]
    # HTML 转义的 XML (`&lt;msg&gt;...`) 先还原
    if "&lt;" in text[:2000] and "<" not in text[:2000]:
        text = html.unescape(text)
    # 未转义的裸 & 会让 ET 解析失败；保留合法实体
    text = _AMP_RE.sub("&amp;", text)
    return text.strip()


def parse_xml(xml: str) -> ET.Element | None:
    """宽松解析 XML: 去非法字符、找第一个根标签、失败返回 None。"""
    if not xml:
        return None
    text = _prepare_xml(xml)
    candidates = [text]
    # 去掉 XML 声明前后的杂文本
    for tag in ("<msg", "<appmsg", "<sysmsg", "<recorditem", "<location"):
        idx = text.find(tag)
        if idx > 0:
            candidates.append(text[idx:])
            break
    for cand in candidates:
        try:
            return ET.fromstring(cand)
        except ET.ParseError:
            continue
    return None


def _iter_elements(root: ET.Element, name: str):
    target = name.lower()
    for elem in root.iter():
        if _local(elem.tag) == target:
            yield elem


def find_text(root: ET.Element | None, *names: str) -> str:
    if root is None:
        return ""
    for name in names:
        for elem in _iter_elements(root, name):
            text = "".join(elem.itertext()).strip()
            if text:
                return text
    return ""


def find_attr(root: ET.Element | None, *names: str) -> str:
    if root is None:
        return ""
    wanted = {n.lower() for n in names}
    for elem in root.iter():
        for key, val in elem.attrib.items():
            if _local(key) in wanted and safe_str(val):
                return safe_str(val)
    return ""


def is_xml_like(text: str) -> bool:
    if not isinstance(text, str):
        return False
    t = text.lstrip("﻿ \t\r\n")
    if t.startswith("&lt;"):
        return True
    if not t.startswith("<"):
        return False
    head = t[:500].lower()
    return any(tag in head for tag in ("<msg", "<appmsg", "<sysmsg", "<recordinfo"))


def clean_text(text: str, limit: int = MAX_SUMMARY_CHARS) -> str:
    """去标签、还原实体、压缩空白，并截断到安全长度。"""
    if not text:
        return ""
    text = html.unescape(str(text))
    text = _TAG_RE.sub(" ", text)
    text = _CTRL_RE.sub("", text)
    text = _WS_RE.sub(" ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "…（已截断）"
    return text


def _clean_line(text: str, limit: int = MAX_ITEM_CHARS) -> str:
    return clean_text(text, limit).replace("\n", " ").strip()


# --------------------------------------------------------------------------- 结构化解析
def parse_refermsg(root: ET.Element | None) -> dict | None:
    """解析 `<refermsg>` 引用块。"""
    if root is None:
        return None
    for ref in _iter_elements(root, "refermsg"):
        msg_id = find_text(ref, "svrid", "msgid", "msg_id")
        content = find_text(ref, "content", "title")
        if not msg_id and not content:
            continue
        return {
            "msg_id": msg_id,
            "sender": find_text(ref, "fromusr", "fromusername", "chatusr"),
            "nickname": find_text(ref, "displayname", "title"),
            "content": clean_text(content, 1024),
            "time": _to_int(find_text(ref, "createtime", "create_time")),
            "type": _to_int(find_text(ref, "type")),
        }
    return None


def _parse_record_item(elem: ET.Element) -> dict | None:
    nickname = find_text(elem, "nickname", "sourcename", "displayname")
    text = find_text(elem, "text", "datadesc", "datatitle", "title")
    if not text:
        # recorditem 里常见转义后的 <recordinfo> XML
        inner = "".join(elem.itertext()).strip()
        if inner.startswith("<"):
            sub = parse_xml(inner)
            if sub is not None:
                nickname = nickname or find_text(sub, "nickname", "sourcename")
                text = find_text(sub, "text", "datadesc", "datatitle", "title")
        if not text:
            text = inner
    text = _clean_line(text)
    if not text:
        return None
    return {"nickname": _clean_line(nickname, 80), "text": text}


def parse_record_items(root: ET.Element | None) -> list[dict]:
    """解析合并转发/接龙的 `<recorditem>` 或 `<dataitem>` 列表。"""
    if root is None:
        return []
    items: list[dict] = []
    for elem in _iter_elements(root, "recorditem"):
        item = _parse_record_item(elem)
        if item:
            items.append(item)
        if len(items) >= MAX_ITEMS:
            return items
    if items:
        return items
    # 部分 vendor 用 <dataitem><datatitle>...</datatitle><datadesc>...</datadesc>
    for elem in _iter_elements(root, "dataitem"):
        title = find_text(elem, "datatitle", "title")
        desc = find_text(elem, "datadesc", "text")
        source = find_text(elem, "sourcename", "nickname")
        text = ": ".join(x for x in (title, desc) if x)
        if text:
            items.append({"nickname": _clean_line(source, 80), "text": _clean_line(text)})
        if len(items) >= MAX_ITEMS:
            break
    return items


def parse_location(root: ET.Element | None) -> dict | None:
    if root is None:
        return None
    for elem in _iter_elements(root, "location"):
        lat = _to_float(elem.attrib.get("x") or elem.attrib.get("lat"))
        lng = _to_float(elem.attrib.get("y") or elem.attrib.get("lng") or elem.attrib.get("lon"))
        if lat is None:
            lat = _to_float(find_text(elem, "lat", "latitude"))
        if lng is None:
            lng = _to_float(find_text(elem, "lng", "lon", "longitude"))
        label = (
            safe_str(elem.attrib.get("label"))
            or safe_str(elem.attrib.get("poiname"))
            or find_text(elem, "label", "poiname")
        )
        if lat is not None and lng is not None:
            return {"lat": lat, "lng": lng, "label": _clean_line(label, 120)}
    return None


def _to_float(v: Any) -> float | None:
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def parse_contact(root: ET.Element | None) -> dict | None:
    if root is None:
        return None
    for elem in root.iter():
        if _local(elem.tag) not in ("msg", "contact"):
            continue
        username = safe_str(elem.attrib.get("username") or elem.attrib.get("wxid"))
        nickname = safe_str(elem.attrib.get("nickname") or elem.attrib.get("displayname"))
        alias = safe_str(elem.attrib.get("alias"))
        if username or nickname:
            return {"username": username, "nickname": nickname, "alias": alias}
    return None


def parse_file_info(root: ET.Element | None) -> dict | None:
    if root is None:
        return None
    for elem in root.iter():
        if _local(elem.tag) not in ("appattach", "file"):
            continue
        info = {
            "filename": safe_str(elem.attrib.get("filename"))
            or find_text(elem, "filename", "title"),
            "attach_id": safe_str(elem.attrib.get("attachid"))
            or safe_str(elem.attrib.get("attach_id"))
            or find_text(elem, "attachid", "attach_id"),
            "user_name": safe_str(elem.attrib.get("user_name"))
            or find_text(elem, "user_name", "username"),
            "data_len": _to_int(
                safe_str(elem.attrib.get("totallen"))
                or safe_str(elem.attrib.get("data_len"))
                or find_text(elem, "totallen", "data_len")
            )
            or 0,
            "ext": safe_str(elem.attrib.get("fileext")) or find_text(elem, "fileext", "file_extension"),
        }
        if any(info.values()):
            return info
    return None


def parse_mini_program(root: ET.Element | None, app: dict) -> dict:
    mp = app.get("mini_program") if isinstance(app.get("mini_program"), dict) else {}
    info = {
        "app_id": safe_str(mp.get("app_id") or mp.get("appid")),
        "page_path": safe_str(mp.get("page_path") or mp.get("pagepath")),
        "username": safe_str(mp.get("username")),
        "source_display_name": safe_str(mp.get("source_display_name")),
        "version": _to_int(mp.get("version")),
    }
    if root is not None:
        for key, tags in (
            ("app_id", ("appid",)),
            ("page_path", ("pagepath", "page_path")),
            ("username", ("username",)),
            ("source_display_name", ("sourcedisplayname", "source_display_name", "nickname")),
            ("version", ("version",)),
        ):
            if not info.get(key):
                val = find_text(root, *tags)
                info[key] = _to_int(val) if key == "version" else safe_str(val)
    return info


def parse_at_list(src: dict) -> list[str]:
    """解析群消息的 @ 列表 (vendor 字段或 `<atuserlist>` XML)。"""
    if not isinstance(src, dict):
        return []
    out: list[str] = []

    def _add(v: Any) -> None:
        if isinstance(v, dict):
            v = v.get("wxid") or v.get("user_id") or v.get("username")
        if isinstance(v, str):
            for piece in re.split(r"[,;，、\s]+", v):
                piece = piece.strip()
                if piece and piece.lower() != "all" and piece not in out:
                    out.append(piece)
        elif isinstance(v, (list, tuple, set)):
            for item in v:
                _add(item)

    for key in ("at_user_list", "atUserList", "atuserlist", "at_list", "atList"):
        _add(src.get(key))
    root = parse_xml(app_xml(src))
    if root is not None:
        for elem in root.iter():
            if _local(elem.tag) in ("atuserlist", "atlist"):
                _add("".join(elem.itertext()))
                for child in elem:
                    _add("".join(child.itertext()))
    return out


def is_sysmsg(text: str) -> bool:
    """稳健判断 `<sysmsg>`，兼容 BOM、XML 声明、注释、大小写。"""
    if not isinstance(text, str):
        return False
    t = text.lstrip("﻿ \t\r\n")
    t = _XML_DECL_RE.sub("", t).strip()
    t = _COMMENT_RE.sub("", t).strip()
    return t[:20].lower().startswith("<sysmsg")


# --------------------------------------------------------------------------- 主解析 / 格式化
def parse_appmsg(src: dict) -> dict:
    """把 vendor 消息解析成统一卡片信息 (结构块优先, XML 兜底)。"""
    app = app_payload(src)
    info: dict[str, Any] = {
        "type": appmsg_type(src),
        "category": app_category(src),
        "title": safe_str(app.get("title")),
        "desc": safe_str(app.get("description") or app.get("des") or app.get("desc")),
        "url": safe_str(app.get("url")),
        "appid": "",
        "page_path": "",
        "username": "",
        "source": safe_str(app.get("source_display_name") or app.get("sourcedisplayname")),
        "location": None,
        "contact": None,
        "quote": None,
        "items": [],
        "file": None,
        "mini_program": None,
        "cover_url": "",
        "raw_xml_size": 0,
    }

    mp = parse_mini_program(None, app)
    if any(mp.values()) or info["category"] == "mini_program":
        info["mini_program"] = mp
        for key in ("app_id", "page_path", "username", "source_display_name"):
            if mp.get(key):
                info[key] = mp[key]
        if not info["source"]:
            info["source"] = mp.get("source_display_name") or mp.get("username") or ""

    file_obj = app.get("file") if isinstance(app.get("file"), dict) else {}
    if file_obj:
        info["file"] = {
            "filename": safe_str(file_obj.get("name") or file_obj.get("filename")),
            "attach_id": "",
            "user_name": "",
            "data_len": 0,
            "download_context": (
                file_obj.get("download_context")
                if isinstance(file_obj.get("download_context"), dict)
                else {}
            ),
        }

    cover = app.get("cover_image") if isinstance(app.get("cover_image"), dict) else {}
    if cover:
        dc = cover.get("download_context") if isinstance(cover.get("download_context"), dict) else {}
        info["cover_url"] = safe_str(dc.get("url") or cover.get("url"))

    xml = app_xml(src)
    info["raw_xml_size"] = len(xml)
    root = parse_xml(xml)
    if root is not None:
        if info["type"] is None:
            info["type"] = _to_int(find_text(root, "type"))
        if not info["category"]:
            cat = find_attr(root, "category")
            if cat:
                info["category"] = cat.lower()
        if not info["title"]:
            info["title"] = find_text(root, "title")
        if not info["desc"]:
            info["desc"] = find_text(root, "des", "description")
        if not info["url"]:
            info["url"] = find_text(root, "url")
        if not info["appid"]:
            info["appid"] = find_text(root, "appid")
        if not info["page_path"]:
            info["page_path"] = find_text(root, "pagepath", "page_path")
        if not info["username"]:
            info["username"] = find_text(root, "username")
        info["quote"] = info["quote"] or parse_refermsg(root)
        items = parse_record_items(root)
        if items:
            info["items"] = items
        info["location"] = info["location"] or parse_location(root)
        info["contact"] = info["contact"] or parse_contact(root)
        file_info = parse_file_info(root)
        if file_info:
            if not info["file"]:
                info["file"] = file_info
            else:
                for key, val in file_info.items():
                    if val and not info["file"].get(key):
                        info["file"][key] = val

    # vendor 的结构化引用 (app.reference)
    ref = app.get("reference")
    if isinstance(ref, dict) and not info["quote"]:
        content = safe_str(ref.get("content") or ref.get("title"))
        if content:
            info["quote"] = {
                "msg_id": safe_str(ref.get("new_msg_id") or ref.get("svr_id") or ref.get("msg_id")),
                "sender": safe_str(ref.get("from_user_id")),
                "nickname": safe_str(ref.get("display_name")),
                "content": clean_text(content, 1024),
                "time": _to_int(ref.get("create_time")),
                "type": _to_int(ref.get("msg_type")),
            }

    # 旧版 reply_context
    if not info["quote"]:
        rc = src.get("reply_context")
        if isinstance(rc, dict):
            content = safe_str(rc.get("quote_content") or rc.get("content"))
            if content:
                info["quote"] = {
                    "msg_id": safe_str(rc.get("new_msg_id") or rc.get("svr_id") or rc.get("msg_id")),
                    "sender": safe_str(rc.get("from_user_id")),
                    "nickname": safe_str(rc.get("display_name")),
                    "content": clean_text(content, 1024),
                    "time": _to_int(rc.get("create_time")),
                    "type": _to_int(rc.get("msg_type")),
                }

    loc = src.get("location")
    if isinstance(loc, dict) and not info["location"]:
        lat = _to_float(loc.get("lat") or loc.get("latitude") or loc.get("x"))
        lng = _to_float(loc.get("lng") or loc.get("lon") or loc.get("longitude") or loc.get("y"))
        if lat is not None and lng is not None:
            info["location"] = {
                "lat": lat,
                "lng": lng,
                "label": safe_str(loc.get("label") or loc.get("poiname") or loc.get("name")),
            }

    contact = src.get("contact")
    if isinstance(contact, dict) and not info["contact"]:
        info["contact"] = {
            "username": safe_str(contact.get("username") or contact.get("wxid")),
            "nickname": safe_str(contact.get("nickname") or contact.get("display_name")),
            "alias": safe_str(contact.get("alias")),
        }

    return info


def is_relay(info: dict) -> bool:
    text = f"{info.get('title', '')}\n{info.get('desc', '')}"
    if "#接龙" in text:
        return True
    return "接龙" in text and bool(re.search(r"(?:^|\n)\s*\d+\.\s", text))


def _format_relay(info: dict) -> str:
    title = _clean_line(info.get("title") or "", 160)
    items = info.get("items") or []
    lines = [f"[接龙] {title}" if title else "[接龙]"]
    if items:
        for idx, item in enumerate(items[:MAX_ITEMS], 1):
            nick = item.get("nickname") or ""
            text = item.get("text") or ""
            prefix = f"{nick}: " if nick else ""
            lines.append(f"{idx}. {prefix}{text}")
    else:
        body = _clean_line(info.get("desc") or "", MAX_SUMMARY_CHARS)
        if body:
            lines.append(body)
    if len(items) > MAX_ITEMS:
        lines.append(f"…（还有 {len(items) - MAX_ITEMS} 条已省略）")
    return clean_text("\n".join(lines))


def _format_forward(info: dict) -> str:
    title = _clean_line(info.get("title") or "聊天记录", 160)
    items = info.get("items") or []
    lines = [f"[合并转发] {title}"]
    for idx, item in enumerate(items[:MAX_ITEMS], 1):
        nick = item.get("nickname") or ""
        text = item.get("text") or ""
        prefix = f"{nick}: " if nick else ""
        lines.append(f"{idx}. {prefix}{text}")
    if not items:
        lines.append("（未解析到条目）")
    elif len(items) > MAX_ITEMS:
        lines.append(f"…（还有 {len(items) - MAX_ITEMS} 条已省略）")
    return clean_text("\n".join(lines))


def format_appmsg(src: dict, info: dict | None = None) -> str:
    """把卡片转成给 LLM 的可读摘要；无法识别时返回空串。"""
    info = info or parse_appmsg(src)
    t = info.get("type")
    cat = (info.get("category") or "").lower()
    title = _clean_line(info.get("title") or "", 160)
    desc = _clean_line(info.get("desc") or "", 600)
    url = safe_str(info.get("url"))

    if cat == "file" or t in (6, 8):
        return f"[文件] {title or 'file'}"

    if cat == "mini_program" or t in (33, 36):
        mp = info.get("mini_program") or {}
        lines = [f"[小程序卡片] {title}" if title else "[小程序卡片]"]
        who = info.get("source") or info.get("username") or mp.get("source_display_name") or "小程序"
        ids = []
        if info.get("appid") or mp.get("app_id"):
            ids.append(f"appid {info.get('appid') or mp.get('app_id')}")
        if info.get("username") or mp.get("username"):
            ids.append(info.get("username") or mp.get("username"))
        lines.append(f"[小程序] {who}" + (f" ({', '.join(ids)})" if ids else ""))
        page = info.get("page_path") or mp.get("page_path")
        if page:
            lines.append(f"页面路径: {page}")
        if desc and desc != who:
            lines.append(f"说明: {desc}")
        if info.get("cover_url"):
            lines.append(f"[小程序封面] {info['cover_url']}")
        return clean_text("\n".join(lines))

    if cat == "quote" or t == 57:
        q = info.get("quote") or {}
        lines = []
        if title:
            lines.append(title)
        if q.get("content"):
            who = q.get("nickname") or q.get("sender") or "对方"
            lines.append(f"[引用] {who}: {q['content']}")
        return clean_text("\n".join(lines))

    if t == 5 or cat == "link":
        lines = [f"[链接] {title}" if title else "[链接]"]
        if desc and desc != title:
            lines.append(f"说明: {desc}")
        if url:
            lines.append(f"链接: {url}")
        return clean_text("\n".join(lines))

    if t == 17 or cat == "location" or info.get("location"):
        loc = info.get("location") or {}
        label = loc.get("label") or title
        lines = [f"[位置] {label}" if label else "[位置]"]
        if loc.get("lat") is not None and loc.get("lng") is not None:
            lines.append(f"坐标: {loc['lat']},{loc['lng']}")
        return clean_text("\n".join(lines))

    if t == 42 or cat in ("personal_card", "contact", "card"):
        c = info.get("contact") or {}
        nick = c.get("nickname") or title or "名片"
        wxid = c.get("username")
        alias = c.get("alias")
        lines = [f"[名片] {nick}"]
        if wxid:
            lines.append(f"wxid: {wxid}")
        if alias:
            lines.append(f"微信号: {alias}")
        return clean_text("\n".join(lines))

    if t == 19 or cat in ("chat_history", "app_message"):
        if is_relay(info):
            return _format_relay(info)
        return _format_forward(info)

    if t in (2000, 2001, 2002) or any(
        key in cat for key in ("payment", "transfer", "hongbao", "redpacket")
    ):
        return ""

    if t == 51 or cat in ("channels", "finder", "live"):
        lines = [f"[视频号] {title}" if title else "[视频号]"]
        if desc and desc != title:
            lines.append(f"说明: {desc}")
        return clean_text("\n".join(lines))

    lines = []
    if title:
        lines.append(f"[卡片] {title}")
    if desc and desc != title:
        lines.append(f"说明: {desc}")
    if url:
        lines.append(f"链接: {url}")
    if not lines:
        return ""
    return clean_text("\n".join(lines))


def clean_card_text(text: str, limit: int = MAX_SUMMARY_CHARS) -> str:
    """把无法结构化的卡片正文变成纯文本 (去标签/解转义/截断)。"""
    if not text:
        return ""
    if is_xml_like(text):
        root = parse_xml(text)
        if root is not None:
            title = find_text(root, "title", "des", "description")
            if title:
                return clean_text(title, limit)
        return clean_text(text, limit)
    return clean_text(text, limit)


__all__ = [
    "MAX_XML_BYTES",
    "MAX_SUMMARY_CHARS",
    "MAX_ITEMS",
    "app_payload",
    "app_category",
    "appmsg_type",
    "app_xml",
    "parse_xml",
    "find_text",
    "find_attr",
    "is_xml_like",
    "clean_text",
    "clean_card_text",
    "parse_refermsg",
    "parse_record_items",
    "parse_location",
    "parse_contact",
    "parse_file_info",
    "parse_mini_program",
    "parse_at_list",
    "is_sysmsg",
    "parse_appmsg",
    "format_appmsg",
    "is_relay",
]
