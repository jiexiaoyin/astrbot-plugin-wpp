"""WPP (WeChatPadPro) vendor HTTP API 客户端。

契约 (已核实):
  - basePath = /api
  - 鉴权: X-Access-Token header + authcode query (两者都要)
  - 发文本: POST /api/Msg/SendTxt  body={ToWxid, Content, At, Type:1}
  - 发图片: POST /api/Msg/UploadImg  body={ToWxid, Base64}   (实测比 SendCDNImg 可靠)
  - 设置 webhook: POST /api/Webhook/Set  body={url, secret, enabled,...}
  - 响应: {Code:0=OK, CodeValue, Data, ...}
"""

import asyncio
import base64
import json
from typing import Any

import aiohttp
from astrbot import logger


class WppApiError(Exception):
    def __init__(self, code: Any, message: str):
        self.code = code
        self.message = message
        super().__init__(f"WPP API error code={code}: {message}")


class WppClient:
    """WPP vendor API 客户端。

    X-Access-Token 即 authcode (同一值), 以 token 为凭证入口,
    同时注入 header + query 双保险 (vendor 两种都认)。
    """

    def __init__(self, base_url: str, auth_token: str, timeout: int = 30) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth_token = auth_token
        self.timeout = timeout
        self._session: aiohttp.ClientSession | None = None

    async def _session_acquire(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def _url(self, endpoint: str) -> str:
        """拼接 {base}/api{endpoint}?authcode=xxx (query 用 token 值)"""
        ep = endpoint if endpoint.startswith("/") else f"/{endpoint}"
        url = f"{self.base_url}/api{ep}"
        if self.auth_token:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}authcode={self.auth_token}"
        return url

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        # X-Access-Token header (即 authcode)
        if self.auth_token:
            h["X-Access-Token"] = self.auth_token
        return h

    async def _post(self, endpoint: str, body: dict, max_retries: int = 2) -> dict:
        """POST vendor API, 带 retry (参考 wpp-openclaw postWppJson: 网络错误指数退避重试)。"""
        session = await self._session_acquire()
        url = self._url(endpoint)
        last_err: Exception | None = None
        for attempt in range(1, max_retries + 2):
            try:
                async with session.post(url, json=body, headers=self._headers(), timeout=aiohttp.ClientTimeout(total=self.timeout)) as resp:
                    text = await resp.text()
                    if resp.status != 200:
                        raise WppApiError(f"http_{resp.status}", f"{endpoint} -> HTTP {resp.status}: {text[:200]}")
                    try:
                        data = json.loads(text)
                    except json.JSONDecodeError:
                        raise WppApiError("bad_json", f"{endpoint} -> 非 JSON 响应: {text[:200]}")
                    # vendor 成功判定: 以 Success:true 为准 (Code 语义不统一, 有些端点 0=成功有些 1=成功)
                    if data.get("Success") is False:
                        raise WppApiError(data.get("Code"), f"{endpoint} -> {text[:300]}")
                    return data
            except aiohttp.ClientError as e:
                last_err = e
                if attempt <= max_retries:
                    await asyncio.sleep(0.5 * (2 ** (attempt - 1)))  # 0.5s, 1s
                    continue
        raise WppApiError("network", f"{endpoint} -> {last_err}") from last_err

    # ------------------------------------------------------------------ 状态检查
    async def get_online_info(self) -> dict:
        """GET /api/User/GetOnlineInfo — 账号在线信息。

        返回原始响应 dict (不抛异常, 因为 Code=-1 也代表"确认过端点"):
          {Code:0, Data:{...}, Message, Success}  在线
          {Code:-1, Message:"未登录或缓存缺失...", ...}  未登录/离线
        """
        session = await self._session_acquire()
        url = self._url("/User/GetOnlineInfo")
        try:
            async with session.get(url, headers=self._headers(), timeout=aiohttp.ClientTimeout(total=self.timeout)) as resp:
                text = await resp.text()
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return {"Code": -2, "Success": False, "Message": f"非JSON响应: {text[:200]}"}
        except aiohttp.ClientError as e:
            return {"Code": -3, "Success": False, "Message": f"网络错误: {e}"}

    async def get_contract_profile(self) -> dict:
        """POST /api/User/GetContractProfile — 取个人信息 (昵称/wxid/地区/签名等)。

        返回原始响应。Data.userInfo 含 NickName/Alias/Province/City/Signature/UserName 等。
        注意: 无头像 URL 字段 (vendor 未提供)。
        """
        session = await self._session_acquire()
        url = self._url("/User/GetContractProfile")
        try:
            async with session.post(url, json={}, headers=self._headers(), timeout=aiohttp.ClientTimeout(total=self.timeout)) as resp:
                text = await resp.text()
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return {"Code": -2, "Success": False, "Message": f"非JSON响应: {text[:200]}"}
        except aiohttp.ClientError as e:
            return {"Code": -3, "Success": False, "Message": f"网络错误: {e}"}

    async def get_long_link_status(self) -> dict:
        """GET /api/Login/LongLinkStatus — 长连接运行状态 (更细粒度)。"""
        session = await self._session_acquire()
        url = self._url("/Login/LongLinkStatus")
        try:
            async with session.get(url, headers=self._headers(), timeout=aiohttp.ClientTimeout(total=self.timeout)) as resp:
                text = await resp.text()
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return {"Code": -2, "Success": False, "Message": f"非JSON响应: {text[:200]}"}
        except aiohttp.ClientError as e:
            return {"Code": -3, "Success": False, "Message": f"网络错误: {e}"}

    # ------------------------------------------------------------------ 文件下载
    async def download_file_binary(self, attach_id: str, user_name: str, data_len: int) -> bytes | None:
        """POST /api/Tools/DownloadFileBinary — 完整下载 v1 文件 (原始字节流, 非 base64)。

        参考 wpp-openclaw file.ts (v1.2.5 FILE-DOWNLOAD-BINARY):
          authcode 走 query, TokenKey 走 header, body {attach_id, user_name, data_len, section}
        返回原始文件 bytes; 失败返回 None。
        """
        session = await self._session_acquire()
        url = self._url("/Tools/DownloadFileBinary")
        headers = self._headers()
        headers["TokenKey"] = self.auth_token
        body: dict = {
            "attach_id": attach_id,
            "user_name": user_name,
            "data_len": data_len,
            "section": {"start_pos": 0, "data_len": data_len},
        }
        try:
            async with session.post(url, json=body, headers=headers, timeout=aiohttp.ClientTimeout(total=self.timeout)) as resp:
                if resp.status != 200:
                    return None
                buf = await resp.read()
                if len(buf) < 10:
                    return None
                return buf
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------ 视频下载
    async def download_video(self, msg_id: str, to_wxid: str, data_len: int, compress_type: int = 0) -> bytes | None:
        """POST /api/Tools/DownloadVideo — 分片下载 v1 视频.

        参考 wpp-openclaw video.ts (v1.3.8 VIDEO-DOWNLOAD):
          - 分片循环: startPos 从 0, 每段 min(1MB, totalLen-startPos), 直到 startPos>=totalLen
          - 每段响应 Data.data.buffer (base64) / Data.Video; Data.totalLen 更新真实总长
          - 终止条件用 startPos>=totalLen (别用 chunk.length<sectionLen, 每段固定 61440 会提前 break)
          - 200MB 上限保护
        """
        session = await self._session_acquire()
        url = self._url("/Tools/DownloadVideo")
        headers = self._headers()
        headers["TokenKey"] = self.auth_token

        chunks: list[bytes] = []
        start_pos = 0
        total_len = data_len
        CHUNK = 1024 * 1024  # 1MB 每段
        MAX_VIDEO_BYTES = 200 * 1024 * 1024
        try:
            while start_pos < total_len:
                section_len = min(CHUNK, total_len - start_pos)
                body: dict = {
                    "to_wxid": to_wxid,
                    "msg_id": int(float(msg_id)) if str(msg_id).isdigit() else msg_id,
                    "data_len": total_len,
                    "section": {"start_pos": start_pos, "data_len": section_len},
                    "compress_type": compress_type,
                }
                async with session.post(url, json=body, headers=headers, timeout=aiohttp.ClientTimeout(total=self.timeout)) as resp:
                    if resp.status != 200:
                        return None
                    try:
                        data = json.loads(await resp.text())
                    except json.JSONDecodeError:
                        return None
                    d = data.get("Data") or {}
                    if isinstance(d.get("totalLen"), (int, float)) and d["totalLen"] > 0:
                        total_len = int(d["totalLen"])
                    b64 = (d.get("data") or {}).get("buffer") or d.get("Video") or ""
                    if not b64:
                        return None
                    chunk = base64.b64decode(b64)
                    if not chunk:
                        return None
                    chunks.append(chunk)
                    start_pos += len(chunk)
                    total_bytes = sum(len(c) for c in chunks)
                    if total_bytes > MAX_VIDEO_BYTES:
                        return None
            if not chunks:
                return None
            return b"".join(chunks)
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------ 图片下载
    async def download_image_cdn(self, file_aes_key: str, file_no: str) -> bytes | None:
        """POST /api/Tools/CdnDownloadImage — 通过 CDN 下载完整大图 (v1.2.5 首选路径)。

        cdn_download_contexts 含 file_aes_key + file_no (标准/缩略图 variant)。
        返回 Data.Image (base64 JPEG) → bytes。失败返回 None。
        """
        try:
            resp = await self._post(
                "/Tools/CdnDownloadImage",
                {"fileAesKey": file_aes_key, "fileNo": file_no},
            )
            image = (resp.get("Data") or {}).get("Image")
            if not image:
                return None
            return base64.b64decode(image)
        except Exception:  # noqa: BLE001
            return None

    async def download_image(self, msg_id: str, to_wxid: str, data_len: int = 0) -> bytes | None:
        """POST /api/Tools/DownloadImg — 下载 v1 图片 (返回 JPEG bytes)。

        vendor 硬限: 首 64KB (小图可, 大图截断)。失败返回 None。
        返回 Data.data.buffer (base64) → 解码为 bytes。
        """
        body: dict = {
            "msg_id": msg_id,
            "to_wxid": to_wxid,
            "data_len": data_len,
            "compress_type": 0,
        }
        if data_len:
            body["section"] = {"start_pos": 0, "data_len": data_len}
        try:
            resp = await self._post("/Tools/DownloadImg", body)
            data = resp.get("Data") or {}
            base_ret = (data.get("BaseResponse") or {}).get("ret")
            if base_ret not in (None, 0):
                return None
            buf = (data.get("data") or {}).get("buffer")
            if not buf:
                return None
            return base64.b64decode(buf)
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------ 心跳
    async def auto_heartbeat(self) -> dict:
        """POST /api/Login/AutoHeartBeat — 开启自动心跳 + 自动二次登录。

        账号登录后若 online=false (长连接未建立), 调此接口拉上线。
        """
        return await self._post("/Login/AutoHeartBeat", {})

    async def is_online(self) -> tuple[bool, dict]:
        """查 GetOnlineInfo, 返回 (是否真正在线, 原始响应)。

        注意: Code==0 只代表能查到账号, 不代表在线。
        真正在线需 Data.online == True (长连接已建立, heartbeatRunning)。
        """
        resp = await self.get_online_info()
        data = resp.get("Data") or {}
        if resp.get("Code") == 0 and data.get("online") is True:
            return True, resp
        return False, resp

    # ------------------------------------------------------------------ 发送
    async def send_text(self, to_wxid: str, content: str, ats: list[str] | None = None) -> dict:
        """POST /api/Msg/SendTxt — 发文本。群@ 用 ats (wxid 列表, 逗号串)。"""
        body: dict = {"ToWxid": to_wxid, "Content": content, "Type": 1}
        if ats:
            body["At"] = ",".join(ats)
        return await self._post("/Msg/SendTxt", body)

    async def send_image(self, to_wxid: str, image_ref: str) -> dict:
        """POST /api/Msg/UploadImg — 发图片。

        image_ref 支持: URL / 本地路径 / base64://xxx / data:image/...;base64,xxx
        """
        base64_str = await self._resolve_image_to_base64(image_ref)
        return await self._post("/Msg/UploadImg", {"ToWxid": to_wxid, "Base64": base64_str})

    # ------------------------------------------------------------------ webhook
    async def set_webhook(self, url: str, secret: str = "") -> bool:
        """POST /api/Webhook/Set — 把回调 url 注册进 vendor。"""
        body: dict = {
            "url": url,
            "enabled": True,
            "enabledSet": True,
            "includeSelfMessage": False,
            "messageTypes": [],
            "retryCount": 0,
            "retryCountSet": False,
            "secret": secret,
            "timeout": 0,
        }
        await self._post("/Webhook/Set", body)
        return True

    # ------------------------------------------------------------------ 工具
    async def _resolve_image_to_base64(self, image_ref: str) -> str:
        """把 URL/路径/base64 统一成裸 base64 (无前缀)。"""
        if image_ref.startswith("base64://"):
            return image_ref[len("base64://"):]
        if image_ref.startswith("data:image"):
            head, _, b64 = image_ref.partition(";base64,")
            if b64:
                return b64
        if image_ref.startswith("http://") or image_ref.startswith("https://"):
            session = await self._session_acquire()
            async with session.get(image_ref, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                if resp.status != 200:
                    raise WppApiError("download", f"图片下载失败 HTTP {resp.status}: {image_ref}")
                data = await resp.read()
            return base64.b64encode(data).decode()
        # 本地路径
        from pathlib import Path
        p = Path(image_ref)
        if p.exists():
            return base64.b64encode(p.read_bytes()).decode()
        raise WppApiError("bad_image", f"无法解析图片引用: {image_ref}")
