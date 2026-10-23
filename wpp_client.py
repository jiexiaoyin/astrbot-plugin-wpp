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
import time
from typing import Any

import aiohttp
from astrbot import logger

from .wpp_messages import SYSTEM_SENDERS

# P2-3: 文件下载大小上限 (200MB, 与视频一致), 防超大文件撑爆内存
MAX_FILE_BYTES = 200 * 1024 * 1024
# 出站图片大小上限 (20MB): 图片要以 base64 塞进 JSON, 过大会拖垮 vendor 和内存
MAX_IMAGE_BYTES = 20 * 1024 * 1024

# 群成员昵称缓存: 引用回复时用来把 At.name 的 wxid 兜底替换成真实昵称。
# 群成员列表调用较重, 同群同成员 30 分钟内只查一次 (与 wpp-openclaw 一致)。
NICKNAME_CACHE_TTL_SECONDS = 1800.0
# 查询失败/查无结果的负缓存: 避免 vendor 故障时每条引用回复都等 5s 超时。
NICKNAME_FAILURE_TTL_SECONDS = 60.0

# 入站群资料 (群名/群主/成员昵称) 缓存。第一条群消息触发一次预取,
# 后续同群消息直接复用, 让 AI 提示词拿到真实昵称而不是 wxid。
GROUP_CACHE_TTL_SECONDS = 1800.0
GROUP_FAILURE_TTL_SECONDS = 60.0
# 群资料预取不能拖死消息 worker
GROUP_INFO_TIMEOUT_SECONDS = 8.0

# 缓存/锁上限: 长期运行时群数和会话数会持续增长, 必须有界。
# 昵称缓存每群每人一条, 给足余量; 群资料/锁只需覆盖活跃会话。
MAX_NICKNAME_CACHE_ENTRIES = 5000
MAX_GROUP_CACHE_ENTRIES = 500
MAX_SESSION_LOCKS = 512
MAX_PACE_ENTRIES = 2000

# 出站消息节流 (同一会话两次 vendor 发送的最小间隔)
DEFAULT_SEND_INTERVAL_SECONDS = 0.3

# 视频分片下载总时限: 单分片超时 30s, 但分片数量没有上限 (vendor 报的
# data_len 不可信)。没有总时限时, 异常长视频会无限占用消息 worker。
VIDEO_DOWNLOAD_TIMEOUT_SECONDS = 600.0

# 这些端点会真正发消息; 统一在 _post 层做同会话串行 + 节流。
_SEND_ENDPOINTS = frozenset(
    {
        "/Msg/SendTxt",
        "/Msg/ShareLink",
        "/Msg/ShareLocation",
        "/Msg/UploadImg",
        "/Msg/SendFile",
        "/Msg/SendVideo",
        "/Msg/SendVoice",
    }
)


class WppApiError(Exception):
    def __init__(self, code: Any, message: str):
        self.code = code
        self.message = message
        super().__init__(f"WPP API error code={code}: {message}")


# P1-5: 已知 Code 语义特殊的端点白名单 — 这些端点 Code!=0 不代表调用失败 (如部分查询型),
# 调用方自行判断返回值。实测当前插件用到的端点成功均 Code=0, 此集合默认空。
_IGNORE_CODE_ENDPOINTS: frozenset[str] = frozenset()


class WppClient:
    """WPP vendor API 客户端。

    X-Access-Token 即 authcode (同一值), 以 token 为凭证入口,
    同时注入 header + query 双保险 (vendor 两种都认)。
    """

    def __init__(
        self,
        base_url: str,
        auth_token: str,
        timeout: int = 30,
        send_interval: float = DEFAULT_SEND_INTERVAL_SECONDS,
        repeat_guard_window: float = 0.0,
        repeat_guard_limit: int = 2,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth_token = auth_token
        self.timeout = timeout
        self._session: aiohttp.ClientSession | None = None
        # key=(chatroom_id, wxid) -> (nickname, monotonic_ts)
        self._nickname_cache: dict[tuple[str, str], tuple[str, float]] = {}
        # key=chatroom_id -> (profile, ts, ok)
        self._group_cache: dict[str, tuple[dict, float, bool]] = {}

        # 出站串行/节流 (v0.7.0):
        #   _chain_locks: 整条消息链独占同一会话, 防止两条回复的分片交错
        #   _send_locks: 单个 vendor 发送请求串行, 保证最小发送间隔
        self.send_interval = max(0.0, float(send_interval or 0.0))
        self._chain_locks: dict[str, asyncio.Lock] = {}
        self._send_locks: dict[str, asyncio.Lock] = {}
        self._last_send_at: dict[str, float] = {}

        # 出站重复保护: 同一会话相同文本在窗口内超过 limit 次后跳过,
        # 防止 LLM 复读造成刷屏 (窗口 0 = 关闭)。
        self.repeat_guard_window = max(0.0, float(repeat_guard_window or 0.0))
        self.repeat_guard_limit = max(1, int(repeat_guard_limit or 2))
        self._outbound_history: dict[tuple[str, str], list[float]] = {}

        # 发送结果统计 (WebUI/Dashboard 可见)
        self.send_stats: dict[str, Any] = {
            "sent": 0,
            "failed": 0,
            "consecutive_failures": 0,
            "last_error": "",
        }

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

    @staticmethod
    def _prune_locks(locks: dict[str, asyncio.Lock]) -> None:
        """锁表有界: 只回收未持有的锁, 绝不丢弃正在使用的锁。"""
        if len(locks) <= MAX_SESSION_LOCKS:
            return
        for key in [k for k, lock in locks.items() if not lock.locked()]:
            if len(locks) <= MAX_SESSION_LOCKS:
                break
            locks.pop(key, None)

    def send_lock(self, to_wxid: str) -> asyncio.Lock:
        """整条消息链的会话级锁: 同一会话的回复按整链串行, 分片不交错。"""
        key = str(to_wxid or "")
        lock = self._chain_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._chain_locks[key] = lock
            self._prune_locks(self._chain_locks)
        return lock

    async def _pace_send(self, to_wxid: str) -> None:
        """保证同一会话两次 vendor 发送至少间隔 send_interval 秒。"""
        if self.send_interval <= 0:
            return
        key = str(to_wxid or "")
        now = time.monotonic()
        last = self._last_send_at.get(key)
        if last is not None:
            delay = self.send_interval - (now - last)
            if delay > 0:
                await asyncio.sleep(delay)
                now = time.monotonic()
        self._last_send_at[key] = now
        if len(self._last_send_at) > MAX_PACE_ENTRIES:
            # 只保留最近使用的一半, 老的节流时间戳已经无意义
            keep = sorted(self._last_send_at.items(), key=lambda kv: kv[1])[-MAX_PACE_ENTRIES // 2 :]
            self._last_send_at = dict(keep)

    def record_send_result(self, ok: bool, error: str = "") -> None:
        """记录 vendor 发送结果; 连续失败 3 次告警一次。"""
        stats = self.send_stats
        if ok:
            stats["sent"] = int(stats.get("sent", 0)) + 1
            stats["consecutive_failures"] = 0
            return
        stats["failed"] = int(stats.get("failed", 0)) + 1
        failures = int(stats.get("consecutive_failures", 0)) + 1
        stats["consecutive_failures"] = failures
        stats["last_error"] = str(error or "")[:300]
        if failures == 3:
            logger.warning(
                f"[WPP] 连续 {failures} 次发送失败, 请检查 vendor/账号状态: "
                f"{str(error or '')[:200]}"
            )

    def should_skip_repeat(self, to_wxid: str, content: str) -> bool:
        """出站复读保护: 窗口内同一会话同一文本超过 limit 次则跳过。"""
        window = self.repeat_guard_window
        if window <= 0 or not content:
            return False
        key = (str(to_wxid or ""), str(content))
        now = time.monotonic()
        history = [t for t in self._outbound_history.get(key, []) if now - t < window]
        skip = len(history) >= self.repeat_guard_limit
        history.append(now)
        self._outbound_history[key] = history
        # 防止历史表无限增长
        if len(self._outbound_history) > 2000:
            for k in list(self._outbound_history)[:1000]:
                self._outbound_history.pop(k, None)
        if skip:
            logger.info(
                f"[WPP] 出站复读保护: 跳过 {window:.0f}s 内第 {len(history)} 次相同消息 "
                f"to={to_wxid} len={len(content)}"
            )
        return skip

    async def _post(
        self,
        endpoint: str,
        body: dict,
        max_retries: int = 2,
        retry_safe: bool = True,
        timeout: float | None = None,
    ) -> dict:
        """POST vendor API, 带 retry。

        retry_safe=False 用于发消息等**非幂等**接口: 网络超时后重试可能造成重复发送,
        因此只尝试一次。查询/下载类接口保持退避重试。

        发送类端点额外做同会话串行 + 最小发送间隔 (send_interval),
        降低多分片/多事件并发时被微信风控的概率。
        """
        if endpoint in _SEND_ENDPOINTS:
            key = str(body.get("ToWxid") or "")
            lock = self._send_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._send_locks[key] = lock
                self._prune_locks(self._send_locks)
            async with lock:
                await self._pace_send(key)
                try:
                    result = await self._post_impl(endpoint, body, max_retries, retry_safe, timeout)
                except Exception as e:  # noqa: BLE001 - 记录后原样抛出
                    self.record_send_result(False, str(e))
                    raise
                self.record_send_result(True)
                return result
        return await self._post_impl(endpoint, body, max_retries, retry_safe, timeout)

    async def _post_impl(
        self,
        endpoint: str,
        body: dict,
        max_retries: int = 2,
        retry_safe: bool = True,
        timeout: float | None = None,
    ) -> dict:
        session = await self._session_acquire()
        url = self._url(endpoint)
        if not retry_safe:
            max_retries = 0
        total_timeout = float(timeout or self.timeout)
        last_err: Exception | None = None
        for attempt in range(1, max_retries + 2):
            try:
                async with session.post(
                    url,
                    json=body,
                    headers=self._headers(),
                    timeout=aiohttp.ClientTimeout(total=total_timeout),
                ) as resp:
                    text = await resp.text()
                    if resp.status != 200:
                        raise WppApiError(
                            f"http_{resp.status}", f"{endpoint} -> HTTP {resp.status}: {text[:200]}"
                        )
                    try:
                        data = json.loads(text)
                    except json.JSONDecodeError:
                        raise WppApiError("bad_json", f"{endpoint} -> 非 JSON 响应: {text[:200]}")
                    # vendor 成功判定 (P1-5): Code!=0 或 Success is False 都视为失败。
                    # 实测 send_txt/getqr 等成功均 Code=0 + Success=true; 已知语义特殊的端点走豁免。
                    code = data.get("Code")
                    if data.get("Success") is False or (
                        code is not None and code != 0 and endpoint not in _IGNORE_CODE_ENDPOINTS
                    ):
                        raise WppApiError(
                            code if code is not None else data.get("CodeValue", "?"),
                            f"{endpoint} -> {text[:300]}",
                        )
                    return data
            except WppApiError as e:
                # 5xx / 非 JSON 响应通常是 vendor 瞬时故障: 查询类接口可以重试。
                # 业务 Code!=0 是确定性拒绝 (鉴权/参数), 重试只会放大无效请求。
                last_err = e
                code = str(e.code)
                retryable = code == "bad_json" or (
                    code.startswith("http_") and code[5:].isdigit() and int(code[5:]) >= 500
                )
                if retryable and attempt <= max_retries:
                    logger.debug(
                        f"[WPP] {endpoint} 第 {attempt} 次失败 ({code}), 退避重试"
                    )
                    await asyncio.sleep(0.5 * (2 ** (attempt - 1)))  # 0.5s, 1s
                    continue
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
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
            async with session.get(
                url, headers=self._headers(), timeout=aiohttp.ClientTimeout(total=self.timeout)
            ) as resp:
                text = await resp.text()
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return {"Code": -2, "Success": False, "Message": f"非JSON响应: {text[:200]}"}
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            return {"Code": -3, "Success": False, "Message": f"网络错误: {e}"}

    async def get_contract_profile(self) -> dict:
        """POST /api/User/GetContractProfile — 取个人信息 (昵称/wxid/地区/签名等)。

        返回原始响应。Data.userInfo 含 NickName/Alias/Province/City/Signature/UserName 等。
        注意: 无头像 URL 字段 (vendor 未提供)。
        """
        session = await self._session_acquire()
        url = self._url("/User/GetContractProfile")
        try:
            async with session.post(
                url,
                json={},
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            ) as resp:
                text = await resp.text()
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return {"Code": -2, "Success": False, "Message": f"非JSON响应: {text[:200]}"}
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            return {"Code": -3, "Success": False, "Message": f"网络错误: {e}"}

    # ------------------------------------------------------------ 昵称解析
    @staticmethod
    def _nick_value(value: Any) -> str:
        if isinstance(value, dict):
            # vendor 部分接口把昵称包成 {"string": "xxx"}
            value = value.get("string") or value.get("String") or ""
        return str(value or "").strip()

    @classmethod
    def _member_nickname(cls, member: dict) -> str:
        for key in ("NickName", "nickName", "nickname", "Remark", "DisplayName", "displayName"):
            nick = cls._nick_value(member.get(key))
            if nick:
                return nick
        return ""

    async def resolve_nickname(self, wxid: str, chatroom_id: str = "") -> str:
        """解析群成员/联系人昵称, 失败返回空字符串。

        群消息优先查群成员列表 (非好友也能查到), 再退回通讯录详情。
        结果按 (群, wxid) 缓存 30 分钟, 避免每条引用回复都打 vendor。
        入站预取的群资料会把成员昵称写进同一缓存, 引用回复通常零额外请求。
        """
        wxid = str(wxid or "").strip()
        if not wxid or wxid == "all":
            return ""
        chatroom_id = str(chatroom_id or "").strip()
        key = (chatroom_id, wxid)
        cached = self._nickname_cache.get(key)
        if cached:
            ttl = NICKNAME_CACHE_TTL_SECONDS if cached[0] else NICKNAME_FAILURE_TTL_SECONDS
            if time.monotonic() - cached[1] < ttl:
                return cached[0]

        nick = ""
        if chatroom_id.endswith("@chatroom"):
            profile = self._cached_group_profile(chatroom_id)
            if profile is not None:
                nick = str((profile.get("members") or {}).get(wxid) or "")
            else:
                nick = await self._group_member_nickname(chatroom_id, wxid)
        if not nick:
            nick = await self._contact_nickname(wxid)
        self._nickname_cache[key] = (nick, time.monotonic())
        self._prune_nickname_cache()
        return nick

    def _prune_nickname_cache(self) -> None:
        if len(self._nickname_cache) <= MAX_NICKNAME_CACHE_ENTRIES:
            return
        now = time.monotonic()
        self._nickname_cache = {
            k: v
            for k, v in self._nickname_cache.items()
            if now - v[1] < NICKNAME_CACHE_TTL_SECONDS
        }
        if len(self._nickname_cache) > MAX_NICKNAME_CACHE_ENTRIES:
            keep = sorted(self._nickname_cache.items(), key=lambda kv: kv[1][1])[
                -MAX_NICKNAME_CACHE_ENTRIES:
            ]
            self._nickname_cache = dict(keep)

    @staticmethod
    def _parse_group_members(resp: dict) -> dict[str, str]:
        """从 /Group/GetChatRoomMemberDetail 响应提取 {wxid: nickname}。"""
        data = resp.get("Data") or {}
        if not isinstance(data, dict):
            return {}
        room = data.get("NewChatroomData") or {}
        members: Any = room.get("ChatRoomMember") if isinstance(room, dict) else None
        if not members:
            members = (
                data.get("MemberList")
                or data.get("ChatRoomMemberList")
                or data.get("ChatroomMemberList")
                or []
            )
        if not isinstance(members, list):
            return {}
        out: dict[str, str] = {}
        for member in members:
            if not isinstance(member, dict):
                continue
            uid = str(member.get("UserName") or member.get("userName") or "").strip()
            if uid:
                out[uid] = WppClient._member_nickname(member)
        return out

    def _prune_group_cache(self) -> None:
        if len(self._group_cache) <= MAX_GROUP_CACHE_ENTRIES:
            return
        now = time.monotonic()
        self._group_cache = {
            k: v
            for k, v in self._group_cache.items()
            if now - v[1] < GROUP_CACHE_TTL_SECONDS
        }
        if len(self._group_cache) > MAX_GROUP_CACHE_ENTRIES:
            keep = sorted(self._group_cache.items(), key=lambda kv: kv[1][1])[
                -MAX_GROUP_CACHE_ENTRIES:
            ]
            self._group_cache = dict(keep)

    def _cached_group_profile(self, chatroom_id: str) -> dict | None:
        cached = self._group_cache.get(str(chatroom_id or ""))
        if not cached:
            return None
        profile, ts, ok = cached
        ttl = GROUP_CACHE_TTL_SECONDS if ok else GROUP_FAILURE_TTL_SECONDS
        if time.monotonic() - ts < ttl:
            return profile
        return None

    def mark_group_profile_failure(self, chatroom_id: str) -> None:
        """写入群资料失败负缓存。

        调用方 (WppAccount 预取) 可能在外层 ``wait_for`` 超时时取消本方法的下游
        查询，导致 ``get_group_profile`` 来不及在自己的末尾写缓存。这里补一条
        60s 负缓存，避免 vendor 变慢时每条群消息都白等一次预取超时。
        """
        chatroom_id = str(chatroom_id or "").strip()
        if not chatroom_id:
            return
        self._group_cache[chatroom_id] = (
            {"name": "", "owner": "", "avatar": "", "members": {}, "member_count": 0},
            time.monotonic(),
            False,
        )
        self._prune_group_cache()

    def mark_nickname_failure(self, wxid: str, chatroom_id: str = "") -> None:
        """写入昵称失败负缓存 (60s)，用途同 :meth:`mark_group_profile_failure`。"""
        wxid = str(wxid or "").strip()
        if not wxid:
            return
        self._nickname_cache[(str(chatroom_id or ""), wxid)] = ("", time.monotonic())
        self._prune_nickname_cache()

    async def get_group_profile(self, chatroom_id: str) -> dict:
        """预取群资料: 群名/群主/头像/成员昵称表。

        第一条群消息触发; 结果缓存 30 分钟 (失败负缓存 60s)。返回:
          {"name": str, "owner": str, "avatar": str, "members": {wxid: nick},
           "member_count": int}
        vendor 查询失败时返回已拿到的部分, 不抛异常。
        """
        chatroom_id = str(chatroom_id or "").strip()
        if not chatroom_id.endswith("@chatroom"):
            return {}
        cached = self._cached_group_profile(chatroom_id)
        if cached is not None:
            return cached

        profile: dict[str, Any] = {
            "name": "",
            "owner": "",
            "avatar": "",
            "members": {},
            "member_count": 0,
        }
        info_ok = False
        members_ok = False
        try:
            info = await self._post(
                "/Group/GetChatRoomInfoDetail",
                {"QID": chatroom_id},
                max_retries=0,
                timeout=GROUP_INFO_TIMEOUT_SECONDS,
            )
            data = info.get("Data") or {}
            if isinstance(data, dict):
                for key in ("NickName", "DisplayName", "Remark"):
                    name = self._nick_value(data.get(key))
                    if name:
                        profile["name"] = name
                        break
                profile["owner"] = self._nick_value(data.get("ChatRoomOwner"))
                profile["avatar"] = self._nick_value(
                    data.get("SmallHeadImgUrl") or data.get("AvatarUrl")
                )
                try:
                    profile["member_count"] = int(data.get("MemberCount") or 0)
                except (TypeError, ValueError):
                    profile["member_count"] = 0
                info_ok = True
        except Exception as e:  # noqa: BLE001 - 群资料失败不阻塞消息
            logger.debug(f"[WPP] 群资料查询失败 {chatroom_id}: {e}")

        try:
            members_resp = await self._post(
                "/Group/GetChatRoomMemberDetail",
                {"QID": chatroom_id},
                max_retries=0,
                timeout=GROUP_INFO_TIMEOUT_SECONDS,
            )
            members = self._parse_group_members(members_resp)
            members_ok = True
            if members:
                profile["members"] = members
                profile["member_count"] = profile["member_count"] or len(members)
                now = time.monotonic()
                for uid, nick in members.items():
                    if nick:
                        self._nickname_cache[(chatroom_id, uid)] = (nick, now)
                self._prune_nickname_cache()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[WPP] 群成员查询失败 {chatroom_id}: {e}")

        # 只有两个查询都成功才享受 30 分钟长缓存; 任一失败走 60s 负缓存,
        # 避免成员列表缺失时整整半小时都拿不到昵称。
        ok = info_ok and members_ok
        self._group_cache[chatroom_id] = (profile, time.monotonic(), ok)
        self._prune_group_cache()
        if ok:
            logger.debug(
                f"[WPP] 群资料已预取: {chatroom_id} name={profile['name']!r} "
                f"members={len(profile['members'])}"
            )
        return profile

    async def fetch_group_snapshot(self, chatroom_id: str) -> dict | None:
        """强拉一次群名 + 完整成员表 (v0.9.0 进群欢迎用)。

        与 :meth:`get_group_profile` 的区别是**读写都不碰 30 分钟缓存**：
        欢迎语必须看到「此刻」的成员表才能 diff 出新人。语义：

        * 成员查询失败 / 返回空 ⇒ ``None``。空成员表**不是**「群里没人了」，
          而是 vendor 没给数据 —— 调用方绝不能拿它当基线，否则会把全群当新人。
        * 群名查询失败不影响结果 (返回 ``name=""``，文案里写「本群」)。
        * 过滤 ``@chatroom`` / ``gh_`` 公众号 / 微信内置系统号，只留真人。

        返回 ``{"name": str, "members": {wxid: nick}, "member_count": int}``。
        """
        chatroom_id = str(chatroom_id or "").strip()
        if not chatroom_id.endswith("@chatroom"):
            return None

        name = ""
        try:
            info = await self._post(
                "/Group/GetChatRoomInfoDetail",
                {"QID": chatroom_id},
                max_retries=0,
                timeout=GROUP_INFO_TIMEOUT_SECONDS,
            )
            data = info.get("Data") or {}
            if isinstance(data, dict):
                for key in ("NickName", "DisplayName", "Remark"):
                    name = self._nick_value(data.get(key))
                    if name:
                        break
        except Exception as e:  # noqa: BLE001 - 群名非必需
            logger.debug(f"[WPP] 群名查询失败 {chatroom_id}: {e}")

        try:
            resp = await self._post(
                "/Group/GetChatRoomMemberDetail",
                {"QID": chatroom_id},
                max_retries=0,
                timeout=GROUP_INFO_TIMEOUT_SECONDS,
            )
        except Exception as e:  # noqa: BLE001 - 成员失败 ⇒ 调用方放弃本次 diff
            logger.debug(f"[WPP] 群成员快照失败 {chatroom_id}: {e}")
            return None
        raw_members = self._parse_group_members(resp)
        members = {
            uid: nick
            for uid, nick in raw_members.items()
            if uid
            and not uid.endswith("@chatroom")
            and not uid.startswith("gh_")
            and uid not in SYSTEM_SENDERS
        }
        if not members:
            logger.debug(f"[WPP] 群成员快照为空, 当作失败: {chatroom_id} raw={len(raw_members)}")
            return None

        now = time.monotonic()
        for uid, nick in members.items():
            if nick:
                self._nickname_cache[(chatroom_id, uid)] = (nick, now)
        return {"name": name, "members": members, "member_count": len(members)}

    async def _group_member_nickname(self, chatroom_id: str, wxid: str) -> str:
        try:
            resp = await self._post(
                "/Group/GetChatRoomMemberDetail", {"QID": chatroom_id}, max_retries=0
            )
        except Exception as e:  # noqa: BLE001 - 昵称查询失败不阻塞发送
            logger.debug(f"[WPP] 群成员昵称查询失败 {chatroom_id}: {e}")
            return ""
        return self._parse_group_members(resp).get(wxid, "")

    async def _contact_nickname(self, wxid: str) -> str:
        try:
            resp = await self._post("/Friend/GetContractDetail", {"userName": wxid}, max_retries=0)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[WPP] 联系人昵称查询失败 {wxid}: {e}")
            return ""
        data = resp.get("Data") or {}
        contacts = data.get("ContactList") if isinstance(data, dict) else None
        if not isinstance(contacts, list) or not contacts:
            return ""
        first = contacts[0]
        return self._member_nickname(first) if isinstance(first, dict) else ""

    async def get_long_link_status(self) -> dict:
        """GET /api/Login/LongLinkStatus — 长连接运行状态 (更细粒度)。"""
        session = await self._session_acquire()
        url = self._url("/Login/LongLinkStatus")
        try:
            async with session.get(
                url, headers=self._headers(), timeout=aiohttp.ClientTimeout(total=self.timeout)
            ) as resp:
                text = await resp.text()
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return {"Code": -2, "Success": False, "Message": f"非JSON响应: {text[:200]}"}
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            return {"Code": -3, "Success": False, "Message": f"网络错误: {e}"}

    # ------------------------------------------------------------------ 文件下载
    async def download_file_binary(
        self, attach_id: str, user_name: str, data_len: int
    ) -> bytes | None:
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
            # 文件可能很大, 下载超时单独放宽 (默认 30s 不够)
            timeout = aiohttp.ClientTimeout(total=max(self.timeout, 120))
            async with session.post(url, json=body, headers=headers, timeout=timeout) as resp:
                if resp.status != 200:
                    return None
                # P2-3: 大小上限 (200MB), 防超大文件撑爆内存
                content_length = resp.headers.get("Content-Length")
                if (
                    content_length
                    and content_length.isdigit()
                    and int(content_length) > MAX_FILE_BYTES
                ):
                    logger.warning(f"[WPP] file too large: {content_length} bytes, skip")
                    return None
                buf = bytearray()
                async for chunk in resp.content.iter_chunked(256 * 1024):
                    buf.extend(chunk)
                    if len(buf) > MAX_FILE_BYTES:
                        logger.warning(f"[WPP] file too large (> {MAX_FILE_BYTES} bytes), skip")
                        return None
                if len(buf) < 10:
                    logger.debug("[WPP] download_file_binary: 响应过短, 视为失败")
                    return None
                return bytes(buf)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[WPP] download_file_binary failed: {e}")
            return None

    # ------------------------------------------------------------------ 语音下载
    async def download_voice_binary(
        self,
        msg_id: str,
        new_msg_id: str = "",
        client_msg_id: str = "",
        master_buf_id: str = "0",
        length: int = 0,
        voice_format: int = 4,
        chat_room_name: str = "",
        from_user_name: str = "",
        to_user_name: str = "",
    ) -> bytes | None:
        """POST /api/Tools/DownloadVoiceBinary — 下载 v1 语音原文件 (silk 字节流)。

        参考 wpp-openclaw voice.ts / tools.js:
          authcode 走 query, TokenKey 走 header, body 使用 snake_case
          (注意是 to_user_name, 不是 to_wxid)
        返回原始 bytes; 失败返回 None。
        """
        session = await self._session_acquire()
        url = self._url("/Tools/DownloadVoiceBinary")
        headers = self._headers()
        headers["TokenKey"] = self.auth_token
        body: dict = {
            "msg_id": msg_id,
            "new_msg_id": new_msg_id,
            "client_msg_id": client_msg_id,
            "master_buf_id": master_buf_id or "0",
            "format": voice_format,
            "length": length,
            "chat_room_name": chat_room_name,
            "from_user_name": from_user_name,
            "to_user_name": to_user_name,
        }
        try:
            async with session.post(
                url,
                json=body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=max(self.timeout, 60)),
            ) as resp:
                if resp.status != 200:
                    return None
                content_length = resp.headers.get("Content-Length")
                if (
                    content_length
                    and content_length.isdigit()
                    and int(content_length) > 20 * 1024 * 1024
                ):
                    logger.warning(f"[WPP] voice too large: {content_length} bytes, skip")
                    return None
                buf = bytearray()
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    buf.extend(chunk)
                    if len(buf) > 20 * 1024 * 1024:
                        logger.warning("[WPP] voice too large (> 20MB), skip")
                        return None
                if len(buf) < 10:
                    logger.debug("[WPP] download_voice_binary: 响应过短, 视为失败")
                    return None
                return bytes(buf)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[WPP] download_voice_binary failed: {e}")
            return None

    # ------------------------------------------------------------------ 视频下载
    async def download_video(
        self, msg_id: str, to_wxid: str, data_len: int, compress_type: int = 0
    ) -> bytes | None:
        """POST /api/Tools/DownloadVideo — 分片下载 v1 视频.

        参考 wpp-openclaw video.ts (v1.3.8 VIDEO-DOWNLOAD):
          - 分片循环: startPos 从 0, 每段 min(1MB, totalLen-startPos), 直到 startPos>=totalLen
          - 每段响应 Data.data.buffer (base64) / Data.Video; Data.totalLen 更新真实总长
          - 终止条件用 startPos>=totalLen (别用 chunk.length<sectionLen, 每段固定 61440 会提前 break)
          - 200MB 上限保护
        ⚠️ 实测 (2026-08-22): vendor 此端点外层 Success:true 但内层 BaseResponse.ret=-2,
          返回的 buffer 为空 → 本函数返回 None。即当前 vendor 版本视频下载不可用
          (除非 vendor 后续修复)。调用方需接受 None 兜底。
        """
        session = await self._session_acquire()
        url = self._url("/Tools/DownloadVideo")
        headers = self._headers()
        headers["TokenKey"] = self.auth_token

        chunks: list[bytes] = []
        start_pos = 0
        total_len = data_len
        total_bytes = 0
        deadline = time.monotonic() + VIDEO_DOWNLOAD_TIMEOUT_SECONDS
        CHUNK = 1024 * 1024  # 1MB 每段
        MAX_VIDEO_BYTES = 200 * 1024 * 1024
        try:
            while start_pos < total_len:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.warning(
                        f"[WPP] 视频下载超过 {VIDEO_DOWNLOAD_TIMEOUT_SECONDS:.0f}s, 放弃: "
                        f"{start_pos}/{total_len} bytes"
                    )
                    return None
                section_len = min(CHUNK, total_len - start_pos)
                body: dict = {
                    "to_wxid": to_wxid,
                    "msg_id": int(float(msg_id)) if str(msg_id).isdigit() else msg_id,
                    "data_len": total_len,
                    "section": {"start_pos": start_pos, "data_len": section_len},
                    "compress_type": compress_type,
                }
                async with session.post(
                    url,
                    json=body,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=min(self.timeout, remaining)),
                ) as resp:
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
                        logger.warning(
                            f"[WPP] download_video: vendor 返回空分片 "
                            f"({start_pos}/{total_len}) ret={(d.get('BaseResponse') or {}).get('ret')}"
                        )
                        return None
                    chunk = base64.b64decode(b64)
                    if not chunk:
                        return None
                    chunks.append(chunk)
                    start_pos += len(chunk)
                    total_bytes += len(chunk)
                    if total_bytes > MAX_VIDEO_BYTES:
                        return None
            if not chunks:
                return None
            return b"".join(chunks)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[WPP] download_video failed: {e}")
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
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[WPP] download_image_cdn failed: {e}")
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
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[WPP] download_image failed: {e}")
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
    async def send_text(
        self,
        to_wxid: str,
        content: str,
        ats: list[str] | None = None,
        *,
        bypass_repeat_guard: bool = False,
    ) -> dict:
        """POST /api/Msg/SendTxt — 发文本。群@ 用 ats (wxid 列表, 逗号串)。

        ``bypass_repeat_guard`` 只给肯定要发的系统文案用（进群欢迎语：短时间内
        连进两个同名新人时，模板文本可能完全相同，不能当成 LLM 复读拦掉）。
        AI 回复不传，保持原复读保护。
        """
        if not bypass_repeat_guard and self.should_skip_repeat(to_wxid, content):
            return {"Code": 0, "Skipped": True, "Reason": "outbound_repeat_guard"}
        body: dict = {"ToWxid": to_wxid, "Content": content, "Type": 1}
        if ats:
            body["At"] = ",".join(ats)
        # 非幂等: 网络错误不重试, 避免重复发送
        return await self._post("/Msg/SendTxt", body, retry_safe=False)

    async def send_app_xml(
        self,
        to_wxid: str,
        xml: str,
        app_type: int = 5,
        *,
        bypass_repeat_guard: bool = False,
    ) -> dict:
        """POST /api/Msg/ShareLink — 发送 appmsg XML (引用回复/链接卡片)。

        引用回复: Type=5 + Xml 内 `<type>57</type>` (vendor /Msg/Quote 不可用时的
        gewe 范式, 见 wpp-openclaw quote-reply.js)。
        与 send_text 共用出站复读保护: 引用回复此前完全绕过该保护,
        AI 复读同一条引用会在群里刷屏。
        """
        if not bypass_repeat_guard and self.should_skip_repeat(to_wxid, xml):
            return {"Code": 0, "Skipped": True, "Reason": "outbound_repeat_guard"}
        return await self._post(
            "/Msg/ShareLink",
            {"ToWxid": to_wxid, "Type": app_type, "Xml": xml},
            retry_safe=False,
        )

    async def send_location(
        self,
        to_wxid: str,
        latitude: float,
        longitude: float,
        label: str = "",
        poi_name: str = "",
    ) -> dict:
        """POST /api/Msg/ShareLocation — 发送位置卡片 (X=纬度, Y=经度)。"""
        return await self._post(
            "/Msg/ShareLocation",
            {
                "ToWxid": to_wxid,
                "X": latitude,
                "Y": longitude,
                "Label": label,
                "Poiname": poi_name,
                "Scale": 16,
                "Infourl": "",
            },
            retry_safe=False,
        )

    async def send_image(self, to_wxid: str, image_ref: str) -> dict:
        """POST /api/Msg/UploadImg — 发图片。

        image_ref 支持: URL / 本地路径 / file:// URI / base64:// / data:image/...
        """
        base64_str = await self._resolve_image_to_base64(image_ref)
        return await self._post(
            "/Msg/UploadImg", {"ToWxid": to_wxid, "Base64": base64_str}, retry_safe=False
        )

    async def send_file(self, to_wxid: str, filename: str, data: bytes) -> dict:
        """POST /api/Msg/SendFile — 上传并发送文件。

        文件上限 100MB，base64 后体积更大，固定 30s 总超时不够用；
        与图片/语音一致，给大媒体放宽到至少 180s。
        """
        return await self._post(
            "/Msg/SendFile",
            {
                "ToWxid": to_wxid,
                "FileName": filename or "file",
                "Base64": base64.b64encode(data).decode(),
            },
            retry_safe=False,
            timeout=max(self.timeout, 180.0),
        )

    async def send_video(self, to_wxid: str, data: bytes, play_length: int = 0) -> dict:
        """POST /api/Msg/SendVideo — 发送视频 (Base64 + 可选封面/时长)。"""
        return await self._post(
            "/Msg/SendVideo",
            {
                "ToWxid": to_wxid,
                "Base64": base64.b64encode(data).decode(),
                "PlayLength": int(play_length or 0),
            },
            retry_safe=False,
            timeout=max(self.timeout, 180.0),
        )

    async def send_voice(
        self,
        to_wxid: str,
        data: bytes,
        fmt_type: int = 3,
        voice_time_ms: int = 0,
    ) -> dict:
        """POST /api/Msg/SendVoice — 发送语音。

        Type: 0=AMR 1=SPEEX 2=MP3 3=WAVE 4=SILK; VoiceTime 单位毫秒。
        """
        return await self._post(
            "/Msg/SendVoice",
            {
                "ToWxid": to_wxid,
                "Base64": base64.b64encode(data).decode(),
                "Type": int(fmt_type),
                "VoiceTime": int(voice_time_ms or 0),
            },
            retry_safe=False,
        )

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

    async def set_business_webhook(self, sync_message_url: str, logout_url: str = "") -> bool:
        """POST /api/Webhook/Business/Set — 设置业务回调 URL (完整消息推送)。

        参考 wpp-openclaw: 只配 /Webhook/Set 只会推空 Data 的 sync_message。
        业务回调才能推完整消息 (AddMsgs 含 content)。
        """
        try:
            await self._post(
                "/Webhook/Business/Set",
                {
                    "syncMessageUrl": sync_message_url,
                    "logoutUrl": logout_url or sync_message_url,
                },
            )
            return True
        except Exception:  # noqa: BLE001
            return False

    async def start_auto_sync(self, target_url: str) -> bool:
        """POST /api/Msg/StartAutoSync — 启动自动同步 (vendor 推完整消息到 target_url)。"""
        try:
            await self._post("/Msg/StartAutoSync", {"TargetURL": target_url})
            return True
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------------ 工具
    @staticmethod
    def _ensure_image_size(size: int, image_ref: str) -> None:
        if size > MAX_IMAGE_BYTES:
            raise WppApiError(
                "image_too_large",
                f"图片过大 ({size} > {MAX_IMAGE_BYTES} bytes): {image_ref[:120]}",
            )

    async def _resolve_image_to_base64(self, image_ref: str) -> str:
        """把 URL/路径/file:// URI/base64 统一成裸 base64 (无前缀)。"""
        if not image_ref:
            raise WppApiError("bad_image", "空的图片引用")
        if image_ref.startswith("base64://"):
            b64 = image_ref[len("base64://") :]
            self._ensure_image_size(len(b64) * 3 // 4, image_ref)
            return b64
        if image_ref.startswith("data:image"):
            _head, _, b64 = image_ref.partition(";base64,")
            if b64:
                self._ensure_image_size(len(b64) * 3 // 4, image_ref)
                return b64
        if image_ref.startswith("file://"):
            # file:///tmp/a.jpg / file://tmp/a.jpg → 本地路径
            from urllib.parse import unquote, urlparse

            parsed = urlparse(image_ref)
            image_ref = unquote(parsed.path or "")
        if image_ref.startswith("http://") or image_ref.startswith("https://"):
            # P1-2 SSRF 防护: 仅允许白名单域名 (微信 CDN / 腾讯云 OSS), 禁止请求内网/任意外部
            if not self._is_allowed_image_url(image_ref):
                raise WppApiError("ssrf_blocked", f"图片 URL 不在白名单, 已阻止下载: {image_ref}")
            session = await self._session_acquire()
            buf = bytearray()
            async with session.get(image_ref, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                if resp.status != 200:
                    raise WppApiError("download", f"图片下载失败 HTTP {resp.status}: {image_ref}")
                # 流式读取 + 上限检查: 不信任远端声明的 Content-Length,
                # 避免恶意/异常响应把内存撑爆。
                content_type = str(resp.headers.get("Content-Type") or "")
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    buf.extend(chunk)
                    self._ensure_image_size(len(buf), image_ref)
            if content_type and not content_type.lower().startswith("image/"):
                logger.debug(f"[WPP] 图片 Content-Type 非 image/*: {content_type}")
            data = bytes(buf)
            return (await asyncio.to_thread(base64.b64encode, data)).decode()
        # 本地路径
        from pathlib import Path

        p = Path(image_ref)
        if not p.exists():
            raise WppApiError("bad_image", f"无法解析图片引用: {image_ref}")
        try:
            size = p.stat().st_size
        except OSError as e:
            raise WppApiError("bad_image", f"无法读取图片: {image_ref}: {e}") from e
        self._ensure_image_size(size, image_ref)
        # 读文件 + base64 都是同步阻塞操作, 放线程池避免卡住事件循环
        data = await asyncio.to_thread(p.read_bytes)
        return (await asyncio.to_thread(base64.b64encode, data)).decode()

    @staticmethod
    def _is_allowed_image_url(url: str) -> bool:
        """SSRF 白名单: 仅放行微信/腾讯系图片 CDN 域名。

        精确匹配域名本身或它的子域。不能直接用 ``host.endswith("qlogo.cn")``:
        ``evilqlogo.cn`` 也会匹配, 等于白名单形同虚设。
        本机/回环地址一律拒绝 —— 本地图片应直接传文件路径, 不走 HTTP。
        """
        import urllib.parse

        host = (urllib.parse.urlparse(url).hostname or "").lower().rstrip(".")
        if not host:
            return False
        allowed_domains = ("qlogo.cn", "qpic.cn", "myqcloud.com")
        return any(host == d or host.endswith("." + d) for d in allowed_domains)
