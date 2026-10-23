"""WPP 单实例单账号: 一个微信账号 (一个 authcode) 的封装。

每个 WppAccount 持有该账号独立的:
  - API 客户端 (WppClient, base_url + 该账号 authcode)
  - WS 实时通道任务 (连 vendor /ws/sync, 该账号 authcode 鉴权)
  - 有界消息队列 + 单 worker (WS 收包不被媒体下载阻塞)
  - 白名单/群策略/黑名单 (data/plugin_data/astrbot_plugin_wpp/accounts/<id>/ 持久化)
  - 去重状态 (环形缓冲, 落盘, 重启后仍有效)
  - 机器人身份 (_self_wxid/_self_nickname)
  - filehelper 命令 (wpp_commands.FilehelperCommands)

v0.3.0 变化:
  - WS 收包 → `asyncio.Queue` → 单 worker 顺序处理: 媒体下载慢时不会阻塞 WS ping/收包
  - 只处理 `Data.type` 属于消息事件的推送 (过滤朋友圈 friend_circle_update 等噪声)
  - 白名单过滤日志降为 debug + 每分钟 INFO 汇总 (旧版每消息 WARNING 刷屏)
  - 去重落盘 (重启后不会重复处理历史消息)
  - 媒体文件登记到事件, 由 AstrBot 在事件结束后清理
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from astrbot import logger

# 只压本插件 WS 连接自己的 websockets 日志, 不改动全局第三方 logger。
_WS_LOGGER = logging.getLogger("astrbot_plugin_wpp.websockets")
_WS_LOGGER.setLevel(logging.WARNING)

from .wpp_client import WppClient
from .wpp_commands import FilehelperCommands
from .wpp_join_events import parse_notice
from .wpp_join_welcome import (
    MEMBER_EVENT_THROTTLE_SECONDS,
    MODE_ALL,
    MODE_BLOCKLIST,
    MODE_WHITELIST,
    build,
    diff_members,
    resolve_cfg,
    should_welcome,
)
from .wpp_media import enrich_media
from .wpp_messages import (
    extract_all_msg_src,
    is_message_payload,
    parse_at_list,
    safe_str,
    src_chatroom,
    src_content,
    src_is_filehelper,
    src_is_incoming,
    src_msg_key,
    src_recipient,
    src_sender,
    skip_reason,
    to_astrbot_message,
)
from .wpp_policy import PolicySnapshot, classify, group_whitelist
from .wpp_storage import AccountStore

QUEUE_MAXSIZE = 1000
DEDUP_MAX = 500
FILTER_REPORT_INTERVAL = 60.0  # 过滤日志汇总间隔 (秒)
# 关闭时清空队列的总时限：每条 3s × 1000 条会让进程退出挂近 50 分钟。
DRAIN_DEADLINE_S = 10.0
# WS 连接稳定存活超过该时长才把重连退避重置回 1s。
WS_STABLE_RESET_SECONDS = 30.0

# 日志里必须隐去的 query 参数 (值只显示为 <set>)
_SENSITIVE_QUERY_KEYS = {"authcode", "token", "key", "secret", "password"}


def _ws_url_with_authcode(url: str, authcode: str) -> str:
    """把 authcode 作为 query 参数拼到 WS 地址上。

    旧实现只在 URL 完全没有 ``?`` 时才拼接, 带其它 query 的地址根本不会
    带上鉴权; 而且直接字符串拼接没有 URL 编码, authcode 含 ``&``/``+`` 时
    会被截断或改写。这里统一用 query 解析/编码, 并去掉重复的 authcode。
    """
    if not authcode:
        return url
    parts = urlsplit(url)
    query = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k != "authcode"
    ]
    query.append(("authcode", authcode))
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


def _safe_ws_url(url: str) -> str:
    """日志用地址: 隐去敏感 query 参数的值, 避免 authcode 进日志。"""
    parts = urlsplit(url)
    if not parts.query:
        return url
    query = [
        (k, "<set>" if k.lower() in _SENSITIVE_QUERY_KEYS and v else v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


class WppAccount:
    """单个微信账号的完整封装 (一个 authcode = 一个账号)。

    platform: WppPlatformAdapter 引用 (用于 commit_event / meta)。
    """

    def __init__(
        self,
        platform,
        account_id: str,
        authcode: str,
        base_url: str,
        ws_url: str,
        allow_users: set[str] | None = None,
        group_reply: str = "atbot",
        blacklist_groups: set[str] | None = None,
        block_users: set[str] | None = None,
        require_at_mention: bool = False,
        private_allow_all: bool = False,
        proactive_groups: set[str] | None = None,
        lazy_media: bool = True,
        send_interval_ms: int = 300,
        repeat_guard_window: float = 300.0,
        repeat_guard_limit: int = 2,
    ) -> None:
        self.platform = platform
        self.account_id = account_id
        self.authcode = authcode
        self.base_url = base_url
        self.ws_url = ws_url
        self.lazy_media = bool(lazy_media)

        # API 客户端 (该账号 authcode)
        self._api = WppClient(
            self.base_url,
            self.authcode,
            send_interval=max(0, int(send_interval_ms or 0)) / 1000.0,
            repeat_guard_window=max(0.0, float(repeat_guard_window or 0.0)),
            repeat_guard_limit=max(1, int(repeat_guard_limit or 2)),
        )

        # 运行时数据 (data/plugin_data/astrbot_plugin_wpp/accounts/<id>/)
        self.store = AccountStore(account_id)

        # 白名单/群策略 (账号级, 独立); 持久化文件优先于启动配置
        self.allow_users: set[str] = set(allow_users or ())
        self.group_reply = group_reply
        self.blacklist_groups: set[str] = set(blacklist_groups or ())
        self.block_users: set[str] = set(block_users or ())
        self.require_at_mention = require_at_mention
        # 空白名单是否放行所有私聊（显式开关；默认关闭）。
        self.private_allow_all = bool(private_allow_all)
        # 主动回复候选群: 未 @ 消息也放行, 但适配器不唤醒, 由决策插件决定
        self.proactive_groups: set[str] = set(proactive_groups or ())
        # 进群欢迎 (v0.9.0): 原始配置段由 _load_policy_file 填充, 每次现解析。
        self._join_welcome_raw: dict = {}
        self._load_policy_file()
        self._whitelist_mtime = self.store.config_mtime()

        # 群成员基线 + 欢迎语后台任务 (抓取/发送绝不阻塞消息 worker)
        self._members_state: dict = self.store.load_members()
        #: 串行化基线「改内存 + 线程池落盘」, 防止两个群的 diff 交叉写坏快照。
        self._members_lock = asyncio.Lock()
        self._welcome_tasks: set[asyncio.Task] = set()
        self._welcome_locks: dict[str, asyncio.Lock] = {}
        #: chatroom_id -> monotonic 时间; 系统通知可能对一次变动连推多条。
        self._welcome_last_check: dict[str, float] = {}

        # 账号状态缓存 (WebUI / 命令展示)
        self._account_status = {"online": None, "wxid": "", "nickname": "", "message": "查询中..."}
        self._self_wxid = ""
        self._self_nickname = ""

        # 消息去重 (环形缓冲, 落盘)
        self._seen_msg_ids: set[str] = set()
        self._seen_msg_ids_order: list[str] = []
        self._dedup_dirty = False
        for mid in self.store.load_dedup()[-DEDUP_MAX:]:
            if mid not in self._seen_msg_ids:
                self._seen_msg_ids.add(mid)
                self._seen_msg_ids_order.append(mid)

        # WS 有界队列 + 单 worker
        self._queue: asyncio.Queue[dict] | None = None
        self._worker_task: asyncio.Task | None = None
        self._ws_task: asyncio.Task | None = None
        self._reload_task: asyncio.Task | None = None

        # 过滤日志汇总
        self._filtered_since_report = 0
        self._ignored_since_report: dict[str, int] = {}
        # 累计值 (不随汇报清零): 面板/排障需要「启动以来总共丢了多少」
        self._total_filtered = 0
        self._total_ignored = 0
        self._total_dropped = 0
        self._total_observed = 0
        self._last_filter_report = time.monotonic()
        self._drop_since_report = 0
        self._observe_since_report = 0
        # Heartflow 白名单周期性自愈 (启动/策略变更/热更新之外)
        self._last_heartflow_sync = time.monotonic()

        # filehelper 命令 (账号级)
        self.filehelper = FilehelperCommands(self)

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        """启动消息 worker + WS 实时通道 + 配置热更新。"""
        if self._queue is None:
            self._queue = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        if self._worker_task is None:
            self._worker_task = asyncio.create_task(self._worker_loop())
        if self.ws_url and self._ws_task is None:
            self._ws_task = asyncio.create_task(self._ws_client_loop())
            logger.info(f"[WPP:{self.account_id}] WS 实时通道已启动: {self.ws_url}")
        if self._reload_task is None:
            self._reload_task = asyncio.create_task(self._maintenance_loop())
        # 已启用欢迎语但还没有基线 (首次部署/换机) ⇒ 先建基线, 避免把老成员当新人。
        self._schedule_welcome_baselines("启动", only_missing=True)

    async def stop(self) -> None:
        """停止 WS / worker / 热更新任务, 落盘去重状态。"""
        for attr in ("_ws_task", "_worker_task", "_reload_task"):
            task: asyncio.Task | None = getattr(self, attr)
            if task:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
                setattr(self, attr, None)
        # 欢迎语任务最后取消: worker 已停, 不会再有新任务进来。
        welcome_tasks = [t for t in self._welcome_tasks if not t.done()]
        for task in welcome_tasks:
            task.cancel()
        if welcome_tasks:
            await asyncio.gather(*welcome_tasks, return_exceptions=True)
        self._welcome_tasks.clear()
        await self._flush_dedup()
        try:
            await self._api.close()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ WS 实时通道
    async def _ws_client_loop(self) -> None:
        """WS 实时消息客户端: 只负责收包入队, 处理交给 worker。"""
        import websockets

        delay = 1.0
        while True:
            connected_at = 0.0
            try:
                url = _ws_url_with_authcode(self.ws_url, self.authcode)
                logger.info(
                    f"[WPP:{self.account_id} WS] connecting: "
                    f"{_safe_ws_url(self.ws_url)}"
                )
                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                    max_size=100 * 1024 * 1024,
                    logger=_WS_LOGGER,
                ) as ws:
                    connected_at = time.monotonic()
                    logger.info(f"[WPP:{self.account_id} WS] connected")
                    async for raw in ws:
                        try:
                            payload = json.loads(raw)
                        except Exception:  # noqa: BLE001
                            continue
                        if not isinstance(payload, dict):
                            continue
                        if not is_message_payload(payload):
                            # 朋友圈/联系人等非消息事件: 静默跳过 (旧版会误解析 items 刷屏)
                            logger.debug(
                                f"[WPP:{self.account_id} WS] 忽略非消息事件: "
                                f"type={safe_str((payload.get('Data') or {}).get('type'))!r}"
                            )
                            continue
                        for src in extract_all_msg_src(payload):
                            self._enqueue(src)
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[WPP:{self.account_id} WS] 连接断开/异常, {delay}s 后重连: {e}")
            # 只有连接稳定存活过才重置退避: 连上即断的抖动不能让间隔永远停在 1s,
            # 否则 vendor 侧故障时会变成每秒一次的连接风暴。
            if (
                connected_at
                and time.monotonic() - connected_at >= WS_STABLE_RESET_SECONDS
            ):
                delay = 1.0
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)  # 指数退避 1s→30s 封顶

    def _enqueue(self, src: dict) -> None:
        """非阻塞入队; 队列满时丢最旧的消息 (WS 不能被处理端拖死)。"""
        if self._queue is None:
            return
        try:
            self._queue.put_nowait(src)
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except (asyncio.QueueEmpty, ValueError):
                pass
            try:
                self._queue.put_nowait(src)
            except asyncio.QueueFull:
                pass
            self._drop_since_report += 1
            self._total_dropped += 1
            logger.warning(
                f"[WPP:{self.account_id}] 消息队列已满, 丢弃最旧消息 "
                f"(累计 {self._total_dropped})"
            )

    async def _worker_loop(self) -> None:
        """单 worker 顺序处理队列消息 (保证同一会话的顺序, 且不阻塞 WS)。"""
        assert self._queue is not None
        while True:
            try:
                src = await self._queue.get()
                try:
                    await self._process_src(src)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[WPP:{self.account_id}] 处理消息失败: {e}")
                finally:
                    self._queue.task_done()
            except asyncio.CancelledError:
                # 退出前尽量清空队列, 不丢已收到的消息
                await self._drain_queue()
                break

    async def _drain_queue(self) -> None:
        if self._queue is None:
            return
        deadline = time.monotonic() + DRAIN_DEADLINE_S
        while not self._queue.empty():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning(
                    f"[WPP:{self.account_id}] 关闭时队列仍未清空 "
                    f"(剩余 {self._queue.qsize()} 条)，已停止等待"
                )
                break
            try:
                src = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            try:
                await asyncio.wait_for(
                    self._process_src(src), timeout=min(3.0, remaining)
                )
            except Exception:  # noqa: BLE001
                pass
            finally:
                self._queue.task_done()

    # ------------------------------------------------------------------ 消息处理
    async def _prefetch_group_profile(self, chatroom_id: str) -> dict | None:
        """预取群资料 (带超时); 失败返回 None, 不影响消息进入 AstrBot。"""
        getter = getattr(self._api, "get_group_profile", None)
        if not callable(getter):
            return None
        try:
            profile = await asyncio.wait_for(getter(chatroom_id), timeout=10)
        except Exception as e:  # noqa: BLE001 - 预取失败不阻塞消息
            logger.debug(f"[WPP:{self.account_id}] 群资料预取失败 {chatroom_id}: {e}")
            # wait_for 可能在中途取消 get_group_profile，令它来不及写自己的失败
            # 负缓存；不补的话 vendor 变慢时每条消息都会再等满 10s。
            marker = getattr(self._api, "mark_group_profile_failure", None)
            if callable(marker):
                try:
                    marker(chatroom_id)
                except Exception:  # noqa: BLE001
                    pass
            return None
        return profile if isinstance(profile, dict) else None

    async def _prefetch_sender_nickname(self, from_wxid: str, src: dict) -> str:
        """私聊缺昵称时查通讯录; 已有 fromNick 则不产生额外请求。"""
        existing = safe_str(
            src.get("fromNick")
            or src.get("fromNickname")
            or src.get("FromNick")
            or src.get("sender_name")
        ).strip()
        if existing:
            return ""
        resolver = getattr(self._api, "resolve_nickname", None)
        if not callable(resolver):
            return ""
        try:
            nick = await asyncio.wait_for(resolver(from_wxid, ""), timeout=8)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[WPP:{self.account_id}] 私聊昵称解析失败 {from_wxid}: {e}")
            marker = getattr(self._api, "mark_nickname_failure", None)
            if callable(marker):
                try:
                    marker(from_wxid, "")
                except Exception:  # noqa: BLE001
                    pass
            return ""
        return str(nick or "").strip()

    async def _process_src(self, src: dict) -> None:
        """处理单条消息: 去重 → filehelper → 白名单 → 转换 → 媒体下载 → 提交事件。"""
        if not isinstance(src, dict):
            return

        from_wxid = src_sender(src)
        chatroom_id = src_chatroom(src)
        content = src_content(src)
        recipient_id = src_recipient(src)
        at_list = parse_at_list(src)
        is_group = bool(chatroom_id)

        # 去重: 入口只检查, 真正记录放到"处理完成"之后 ——
        # 入队后若下载/提交抛异常, worker 会丢弃这条消息; 若此时已记录,
        # 重连重推时就会被当成重复而永久丢失。故意丢弃的分支 (命令/过滤/
        # 系统消息) 仍会立即记录, 避免重推时反复处理。
        msg_key = src_msg_key(src)
        if msg_key and msg_key in self._seen_msg_ids:
            logger.debug(f"[WPP:{self.account_id}] 重复消息已跳过: id={msg_key}")
            return

        # filehelper 管理命令 (自身发出的消息 direction=outgoing, 需在方向过滤之前处理)。
        # 管理命令只认「本机发往文件助手」的消息; vendor 明确标为入站的直接拒绝,
        # 防止回调被伪造/重放时把任意外来消息当管理命令执行。
        if src_is_filehelper(src) and content.startswith("/"):
            if safe_str(src.get("direction")) and src_is_incoming(src):
                logger.warning(
                    f"[WPP:{self.account_id}] 拒绝入站消息中的 filehelper 管理命令: "
                    f"from={from_wxid or '-'}"
                )
            else:
                await self._handle_filehelper_command(content)
            self._record_seen_msg(msg_key)
            return

        # 群成员变动通知 (v0.9.0): 只调度后台 diff, 不阻塞收信;
        # 必须在 skip_reason 之前 —— 10000 系统通知会在那里被静默丢弃。
        self._maybe_join_notice(src)

        # 系统/服务会话 (公众号/微信团队/微信支付/服务通知...)、状态推送、撤回、支付卡片:
        # 任何模式都静默 —— "全部放行"也不能让这些消息触发 AI
        reason = skip_reason(src, self._self_wxid)
        if reason:
            key = reason.split(":", 1)[0]
            self._ignored_since_report[key] = self._ignored_since_report.get(key, 0) + 1
            self._total_ignored += 1
            logger.debug(
                f"[WPP:{self.account_id}] 已忽略消息: reason={reason} from={from_wxid or '-'}"
            )
            self._record_seen_msg(msg_key)
            return

        logger.debug(
            f"[WPP:{self.account_id}] 收到: from={from_wxid} group={chatroom_id or '-'} "
            f"content={content[:40]!r} self_wxid={self._self_wxid!r}"
        )

        # 白名单/群策略 (只决定"是否进入 AstrBot", 不决定是否唤醒/回复)
        decision = classify(
            self.policy_snapshot(),
            from_wxid,
            is_group,
            chatroom_id,
            content,
            recipient_id,
            at_list,
        )
        if not decision.allowed:
            self._filtered_since_report += 1
            self._total_filtered += 1
            logger.debug(
                f"[WPP:{self.account_id}] 被白名单/群策略过滤: "
                f"from={from_wxid} group={chatroom_id or '-'} reason={decision.reason}"
            )
            self._record_seen_msg(msg_key)
            return

        # 入站昵称/群资料预取 (v0.7.0):
        #   - 群消息: 群名/群主/成员昵称表, 让 AI 看到"张三"而不是"wxid_xxx";
        #     第一条消息触发, 结果 30 分钟缓存, 引用回复也能直接复用。
        #   - 私聊: 入站缺 fromNick 时查通讯录昵称。
        sender_nickname = ""
        group_profile = None
        if is_group:
            group_profile = await self._prefetch_group_profile(chatroom_id)
            if group_profile:
                sender_nickname = str((group_profile.get("members") or {}).get(from_wxid) or "")
        else:
            sender_nickname = await self._prefetch_sender_nickname(from_wxid, src)

        msg = to_astrbot_message(
            src,
            self._self_wxid,
            self._self_nickname,
            sender_nickname=sender_nickname,
            group_profile=group_profile,
        )
        if msg is None:
            self._record_seen_msg(msg_key)
            return

        # 未 @ 的群消息 (主动候选群 / all 观察模式) 只构造占位组件:
        #   - 语音仍即时下载, 否则会错过 PreProcessStage 的 STT;
        #   - 图片/文件/视频延迟到 on_waiting_llm_request 真正要调 LLM 时再下载。
        # lazy_media=false 时即使未 @ 也立即下载媒体 (配合内置 group_icl /
        # group_message_history 等需要在事件创建时就拿到真实媒体的插件)。
        observe_only = is_group and not decision.at_me and self.lazy_media
        if observe_only:
            temp_files = await enrich_media(self._api, msg, include={"voice"})
            self._observe_since_report += 1
            self._total_observed += 1
            logger.debug(
                f"[WPP:{self.account_id}] 主动观察放行: from={from_wxid} "
                f"group={chatroom_id} reason={decision.reason}"
            )
        else:
            # 媒体富化 (下载图片/文件/视频/语音), 临时文件交给 AstrBot 事件结束清理
            temp_files = await enrich_media(self._api, msg)

        # 主动发送路由: 记录 session → 本账号, 再按平台约定创建事件
        event = self.platform.create_event(msg)
        if observe_only:
            # 懒加载媒体: main.py 的 on_waiting_llm_request 钩子会消费这个标记
            event.set_extra("_wpp_lazy_media", self)
        for path in temp_files:
            event.track_temporary_local_file(path)
        self.platform.commit_event(event)
        # 提交成功后才记为已处理: 之前的异常会让这条消息保持"未处理",
        # 重推时有机会重试, 而不是永久丢失。
        self._record_seen_msg(msg_key)

    def _record_seen_msg(self, msg_id_key: str) -> None:
        if not msg_id_key or msg_id_key in self._seen_msg_ids:
            return
        self._seen_msg_ids.add(msg_id_key)
        self._seen_msg_ids_order.append(msg_id_key)
        while len(self._seen_msg_ids_order) > DEDUP_MAX:
            old = self._seen_msg_ids_order.pop(0)
            self._seen_msg_ids.discard(old)
        self._dedup_dirty = True

    async def _flush_dedup(self) -> None:
        """把去重环形缓冲落盘 (含 fsync), 放到线程池执行。

        维护循环每 3 秒调一次; 同步 fsync 直接跑在事件循环上会周期性
        卡住 WS 收包与发送节流。先清脏标记再写: 写入期间进来的新消息
        会重新置脏, 下一轮补写。
        """
        if not self._dedup_dirty:
            return
        payload = self._seen_msg_ids_order[-DEDUP_MAX:]
        self._dedup_dirty = False
        try:
            await asyncio.to_thread(self.store.save_dedup, payload)
        except Exception as e:  # noqa: BLE001
            self._dedup_dirty = True
            logger.warning(f"[WPP:{self.account_id}] 保存去重状态失败: {e}")

    # ------------------------------------------------------------------ 策略快照/持久化
    def policy_snapshot(self) -> PolicySnapshot:
        return PolicySnapshot(
            allow_users=set(self.allow_users),
            block_users=set(self.block_users),
            blacklist_groups=set(self.blacklist_groups),
            proactive_groups=set(self.proactive_groups),
            group_reply=self.group_reply,
            require_at_mention=self.require_at_mention,
            private_allow_all=self.private_allow_all,
            self_wxid=self._self_wxid,
            self_nickname=self._self_nickname,
        )

    def _load_policy_file(self) -> None:
        """读取该账号的持久化配置 (filehelper 命令写入; 旧目录自动迁移)。"""
        try:
            data = self.store.load_config()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[WPP:{self.account_id}] 读取配置文件失败: {e}")
            return
        if not isinstance(data, dict):
            data = {}
        # 进群欢迎配置段: 原样保存 (每次用 resolve_cfg 现算)。
        # 文件里**删掉**这一段 = 回到默认关闭 (fail-closed), 不能沿用旧配置。
        jw = data.get("join_welcome")
        self._join_welcome_raw = dict(jw) if isinstance(jw, dict) else {}
        if not data:
            return
        # 文件里一旦存在 allow_users，它就是权威值（整体替换，不做并集）：
        # 否则启动配置里的种子项永远删不掉，/user del 与面板移除都会“复活”。
        arr = data.get("allow_users")
        if isinstance(arr, list):
            self.allow_users = {str(u).strip() for u in arr if str(u).strip()}
        if isinstance(data.get("group_reply"), str):
            self.group_reply = data["group_reply"]
        bl = data.get("blacklist_groups", [])
        if isinstance(bl, list):
            self.blacklist_groups = {str(g).strip() for g in bl if str(g).strip()}
        bu = data.get("block_users", [])
        if isinstance(bu, list):
            self.block_users = {str(u).strip() for u in bu if str(u).strip()}
        if data.get("require_at_mention") is not None:
            self.require_at_mention = bool(data["require_at_mention"])
        if data.get("private_allow_all") is not None:
            self.private_allow_all = bool(data["private_allow_all"])
        # 只在文件里显式存在时覆盖, 避免旧配置文件把平台配置里的初始值清空
        pg = data.get("proactive_groups")
        if isinstance(pg, list):
            self.proactive_groups = {str(g).strip() for g in pg if str(g).strip()}

    def _save_policy_file(self) -> bool:
        """同步落盘策略 (atomic write + fsync)；返回是否真正写成功。

        **读-改-写**：只覆盖本函数负责的键，保留 ``join_welcome`` 以及未知键 ——
        旧版整份重写会把用户在文件里手写的欢迎语配置抹掉。
        异步调用方应走 :meth:`save_policy_async`, 别在事件循环里直接 fsync。
        """
        try:
            data = self.store.load_config()
            if not isinstance(data, dict):
                data = {}
            data.update(
                {
                    "allow_users": sorted(self.allow_users),
                    "group_reply": self.group_reply,
                    "blacklist_groups": sorted(self.blacklist_groups),
                    "block_users": sorted(self.block_users),
                    "require_at_mention": self.require_at_mention,
                    "private_allow_all": self.private_allow_all,
                    "proactive_groups": sorted(self.proactive_groups),
                }
            )
            self.store.save_config(data)
            self._whitelist_mtime = self.store.config_mtime()
            logger.info(f"[WPP:{self.account_id}] 运行时配置已持久化 → {self.store.config_path}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[WPP:{self.account_id}] 写配置文件失败: {e}")
            return False
        return True

    def save_policy(self) -> bool:
        """同步入口 (测试 / 非事件循环场景): 落盘并同步 Heartflow 白名单。"""
        if not self._save_policy_file():
            return False
        self._sync_heartflow_whitelist("policy")
        return True

    async def save_policy_async(self) -> bool:
        """事件循环内使用的入口: 文件 IO 与 Heartflow 同步都放到线程池。"""
        if not await asyncio.to_thread(self._save_policy_file):
            return False
        await asyncio.to_thread(self._sync_heartflow_whitelist, "policy")
        return True

    def _sync_heartflow_whitelist(self, reason: str) -> None:
        """白名单变化后同步给 Heartflow (平台级聚合所有账号)。"""
        sync = getattr(self.platform, "sync_heartflow_whitelist", None)
        if not callable(sync):
            return
        try:
            sync(reason)
        except Exception as e:  # noqa: BLE001 - 可选集成, 失败不影响命令
            logger.debug(f"[WPP:{self.account_id}] Heartflow 白名单同步失败: {e}")

    # ------------------------------------------------------------------ 进群欢迎 (v0.9.0)
    def join_welcome_cfg(self) -> dict:
        """当前生效的欢迎语配置 (每次从原始段现算, 热更新即时生效)。"""
        return resolve_cfg(self._join_welcome_raw)

    def join_welcome_groups(self) -> list[str]:
        """当前配置下会发欢迎语的群 (命令展示 / 基线预建共用)。"""
        cfg = self.join_welcome_cfg()
        if not cfg["enabled"]:
            return []
        whitelist = group_whitelist(self.policy_snapshot())
        blacklisted = set(self.blacklist_groups or ())
        mode = cfg["mode"]
        if mode == MODE_WHITELIST:
            return sorted(whitelist - blacklisted)
        if mode == MODE_ALL:
            known = (
                whitelist
                | set(self.proactive_groups)
                | set(self._members_state.get("chatrooms") or {})
            )
            return sorted(known - blacklisted)
        if mode == MODE_BLOCKLIST:
            known = (
                whitelist
                | set(self.proactive_groups)
                | set(self._members_state.get("chatrooms") or {})
            )
            return sorted(known - set(cfg["groups"]) - blacklisted)
        return sorted(set(cfg["groups"]) - blacklisted)

    def join_welcome_status(self) -> dict[str, Any]:
        """命令展示用：生效配置 + 生效群 + 每群基线成员数。"""
        cfg = self.join_welcome_cfg()
        groups = self.join_welcome_groups()
        chatrooms = self._members_state.get("chatrooms") or {}
        baselines = {
            group: len((chatrooms.get(group) or {}).get("members") or {}) for group in groups
        }
        return {"cfg": cfg, "groups": groups, "baselines": baselines}

    def _welcome_allowed(self, chatroom_id: str, cfg: dict | None = None) -> bool:
        return should_welcome(
            cfg or self.join_welcome_cfg(),
            chatroom_id,
            group_whitelist=group_whitelist(self.policy_snapshot()),
            blacklisted_groups=self.blacklist_groups,
        )

    def _save_join_welcome_enabled(self, enabled: bool) -> bool:
        """同步写 ``join_welcome.enabled`` (保留其余键)。"""
        raw = dict(self._join_welcome_raw or {})
        raw["enabled"] = bool(enabled)
        try:
            data = self.store.load_config()
            if not isinstance(data, dict):
                data = {}
            data["join_welcome"] = raw
            self.store.save_config(data)
            self._whitelist_mtime = self.store.config_mtime()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[WPP:{self.account_id}] 保存进群欢迎开关失败: {e}")
            return False
        self._join_welcome_raw = raw
        return True

    def _after_join_welcome_toggle(self, enabled: bool, was_enabled: bool) -> None:
        logger.info(f"[WPP:{self.account_id}] 进群欢迎已{'开启' if enabled else '关闭'}")
        if enabled and not was_enabled:
            # 开启瞬间先把生效群当前成员记为基线, 老成员不会被当成新人欢迎。
            self._schedule_welcome_baselines("命令启用")

    def set_join_welcome_enabled(self, enabled: bool) -> bool:
        """同步入口 (测试 / 非事件循环场景)。"""
        was_enabled = self.join_welcome_cfg()["enabled"]
        if not self._save_join_welcome_enabled(enabled):
            return False
        self._after_join_welcome_toggle(enabled, was_enabled)
        return True

    async def set_join_welcome_enabled_async(self, enabled: bool) -> bool:
        """事件循环内入口: 落盘放线程池, 基线调度回事件循环。"""
        was_enabled = self.join_welcome_cfg()["enabled"]
        if not await asyncio.to_thread(self._save_join_welcome_enabled, enabled):
            return False
        self._after_join_welcome_toggle(enabled, was_enabled)
        return True

    def _maybe_join_notice(self, src: dict) -> None:
        """识别成员变动通知并调度后台 diff (同步返回, 绝不阻塞 worker)。"""
        try:
            chatroom_id = src_chatroom(src)
            if not chatroom_id.endswith("@chatroom"):
                # 机器人自己被拉进群时可能没有群 id; AstrBot 侧没有全量群列表,
                # 这类通知只留痕, 不参与欢迎。
                return
            notice = parse_notice(src)
            if notice is None:
                return
            if notice.kind == "self_join":
                logger.info(
                    f"[WPP:{self.account_id}] 机器人被邀请入群 group={chatroom_id} "
                    f"raw={notice.raw[:80]}"
                )
                return
            cfg = self.join_welcome_cfg()
            if not self._welcome_allowed(chatroom_id, cfg):
                logger.debug(
                    f"[WPP:{self.account_id}] 成员变动通知但该群未启用欢迎: group={chatroom_id}"
                )
                return
            now = time.monotonic()
            last = self._welcome_last_check.get(chatroom_id, 0.0)
            if now - last < MEMBER_EVENT_THROTTLE_SECONDS:
                logger.debug(
                    f"[WPP:{self.account_id}] 成员变动节流跳过 group={chatroom_id} "
                    f"({now - last:.0f}s 内已查过)"
                )
                return
            self._welcome_last_check[chatroom_id] = now
            self._spawn_welcome_task(self._run_join_diff(chatroom_id), f"diff:{chatroom_id}")
        except Exception as e:  # noqa: BLE001 - 通知解析失败不能拖累收信
            logger.warning(f"[WPP:{self.account_id}] 成员变动通知处理失败: {e}")

    def _spawn_welcome_task(self, coro, label: str) -> None:
        try:
            task = asyncio.create_task(coro)
        except RuntimeError:
            # 没有运行中的事件循环 (同步命令/测试): 放弃本次调度, 别泄漏协程。
            coro.close()
            logger.debug(f"[WPP:{self.account_id}] 无事件循环, 跳过欢迎任务: {label}")
            return
        self._welcome_tasks.add(task)
        task.add_done_callback(self._on_welcome_task_done)
        logger.debug(f"[WPP:{self.account_id}] 已调度进群欢迎任务: {label}")

    def _on_welcome_task_done(self, task: asyncio.Task) -> None:
        self._welcome_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.warning(f"[WPP:{self.account_id}] 进群欢迎后台任务异常: {exc}")

    def _schedule_welcome_baselines(self, reason: str, *, only_missing: bool = False) -> None:
        """为生效群预建成员基线 (启用/启动/白名单变更时调用)。"""
        if not self.join_welcome_cfg()["enabled"]:
            return
        chatrooms = self._members_state.get("chatrooms") or {}
        scheduled = 0
        for chatroom_id in self.join_welcome_groups():
            if only_missing and (chatrooms.get(chatroom_id) or {}).get("members"):
                continue
            self._spawn_welcome_task(self._run_join_diff(chatroom_id), f"baseline:{chatroom_id}")
            scheduled += 1
        if scheduled:
            logger.info(f"[WPP:{self.account_id}] 进群欢迎预建基线 ({reason}): {scheduled} 群")

    async def _run_join_diff(self, chatroom_id: str) -> None:
        """强拉成员表 → 与基线 diff → 先落盘再发欢迎语。"""
        chatroom_id = str(chatroom_id or "").strip()
        if not chatroom_id.endswith("@chatroom"):
            return
        lock = self._welcome_locks.setdefault(chatroom_id, asyncio.Lock())
        async with lock:
            cfg = self.join_welcome_cfg()
            if not self._welcome_allowed(chatroom_id, cfg):
                return
            snapshot = await self._api.fetch_group_snapshot(chatroom_id)
            if snapshot is None:
                # 空/失败都不是「群里没人了」: 保留旧基线, 下次再对比。
                logger.debug(
                    f"[WPP:{self.account_id}] 进群欢迎: 成员快照失败, 保留旧基线 "
                    f"group={chatroom_id}"
                )
                return
            current = snapshot.get("members") or {}
            if not current:
                return
            group_name = str(snapshot.get("name") or "")
            baseline_group = (self._members_state.get("chatrooms") or {}).get(chatroom_id) or {}
            baseline = baseline_group.get("members") or {}
            if not baseline:
                # 首次同步: 只建基线, 绝不把全群欢迎一遍。
                if await self._write_baseline(chatroom_id, group_name, current):
                    logger.info(
                        f"[WPP:{self.account_id}] 进群欢迎基线已建立 "
                        f"group={chatroom_id} members={len(current)}"
                    )
                return

            added, removed = diff_members(baseline, current)
            plan = build(
                cfg,
                chatroom_id,
                added,
                hour=time.localtime().tm_hour,
                group_name=group_name or str(baseline_group.get("name") or ""),
                group_whitelist=group_whitelist(self.policy_snapshot()),
                blacklisted_groups=self.blacklist_groups,
            )
            if plan is None:
                if added:
                    logger.info(
                        f"[WPP:{self.account_id}] 成员变动但不发欢迎语 group={chatroom_id} "
                        f"added={len(added)} removed={len(removed)} "
                        f"(静默时段/超过 maxBatch={cfg['maxBatch']}/群未生效)"
                    )
                await self._write_baseline(chatroom_id, group_name, current)
                return

            # 先落盘后发送: 至多一次 —— 宁可丢一条欢迎, 绝不重复刷屏。
            if not await self._write_baseline(chatroom_id, group_name, current):
                return
            text, ats = plan
            logger.info(
                f"[WPP:{self.account_id}] 成员变动 group={chatroom_id} "
                f"added={len(added)} removed={len(removed)} ⇒ 发欢迎语 ats={len(ats)}"
            )
            await self._send_join_welcome(chatroom_id, text, ats)

    async def _write_baseline(
        self, chatroom_id: str, group_name: str, members: dict
    ) -> bool:
        """把当前成员表写成新基线; 落盘失败则回滚内存并返回 False。

        落盘含 fsync, 放线程池执行; 用账号级锁串行化「改内存 + 写文件」,
        避免多个群的 diff 同时把半更新的快照写进 members.json。
        """
        async with self._members_lock:
            chatrooms = self._members_state.setdefault("chatrooms", {})
            old_group = chatrooms.get(chatroom_id) or {}
            old_members = old_group.get("members") or {}
            now_ts = int(time.time())
            merged: dict[str, dict] = {}
            for wxid, nick in members.items():
                prev = old_members.get(wxid)
                first_seen = 0
                if isinstance(prev, dict):
                    try:
                        first_seen = int(prev.get("first_seen") or 0)
                    except (TypeError, ValueError):
                        first_seen = 0
                merged[wxid] = {
                    "nick": str(nick or ""),
                    "first_seen": first_seen or now_ts,
                }
            previous = chatrooms.get(chatroom_id)
            previous_updated_at = self._members_state.get("updated_at")
            chatrooms[chatroom_id] = {
                "name": str(group_name or old_group.get("name") or ""),
                "synced_at": now_ts,
                "members": merged,
            }
            self._members_state["version"] = 1
            self._members_state["updated_at"] = now_ts
            try:
                await asyncio.to_thread(self.store.save_members, self._members_state)
                return True
            except Exception as e:  # noqa: BLE001 - 落盘失败 ⇒ 本次不发欢迎
                logger.warning(
                    f"[WPP:{self.account_id}] 保存群成员基线失败 "
                    f"group={chatroom_id}: {e}"
                )
                if previous is None:
                    chatrooms.pop(chatroom_id, None)
                else:
                    chatrooms[chatroom_id] = previous
                self._members_state["updated_at"] = previous_updated_at
                return False

    async def _send_join_welcome(self, chatroom_id: str, text: str, ats: list[str]) -> None:
        try:
            resp = await self._api.send_text(
                chatroom_id, text, ats=ats or None, bypass_repeat_guard=True
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - 欢迎语失败不影响收信
            logger.warning(f"[WPP:{self.account_id}] 进群欢迎语发送失败 group={chatroom_id}: {e}")
            return
        if resp.get("Skipped"):
            logger.warning(f"[WPP:{self.account_id}] 进群欢迎语被跳过 group={chatroom_id}: {resp}")
            return
        logger.info(
            f"[WPP:{self.account_id}] 已发进群欢迎语 group={chatroom_id} "
            f"ats={len(ats or [])}: {text[:60]}"
        )

    # ------------------------------------------------------------------ 维护循环
    async def _maintenance_loop(self) -> None:
        """每 3 秒: 配置热更新检查 + 去重落盘 + 过滤日志汇总。"""
        while True:
            try:
                await asyncio.sleep(3)
                mtime = self.store.config_mtime()
                if mtime > 0 and mtime != self._whitelist_mtime:
                    self._whitelist_mtime = mtime
                    old = self.policy_snapshot()
                    old_welcome_raw = self._join_welcome_raw
                    old_welcome_cfg = self.join_welcome_cfg()
                    self._load_policy_file()
                    policy_changed = (
                        self.allow_users != old.allow_users
                        or self.block_users != old.block_users
                        or self.blacklist_groups != old.blacklist_groups
                        or self.group_reply != old.group_reply
                        or self.require_at_mention != old.require_at_mention
                        or self.proactive_groups != old.proactive_groups
                    )
                    if policy_changed:
                        logger.info(f"[WPP:{self.account_id}] 配置文件外部修改已热更新")
                        self._sync_heartflow_whitelist("hot-reload")

                    welcome_cfg = self.join_welcome_cfg()
                    if self._join_welcome_raw != old_welcome_raw:
                        logger.info(
                            f"[WPP:{self.account_id}] 进群欢迎配置已热更新: "
                            f"enabled={welcome_cfg['enabled']} mode={welcome_cfg['mode']} "
                            f"生效群={len(self.join_welcome_groups())}"
                        )
                    if welcome_cfg["enabled"] and not old_welcome_cfg["enabled"]:
                        # 开启瞬间先记基线, 防止把老成员当新人。
                        self._schedule_welcome_baselines("配置启用")
                    elif welcome_cfg["enabled"] and (
                        policy_changed or self._join_welcome_raw != old_welcome_raw
                    ):
                        # 白名单/模式变化可能新增生效群: 只给还没有基线的群补建。
                        self._schedule_welcome_baselines("配置变更", only_missing=True)

                await self._flush_dedup()

                now = time.monotonic()
                # Heartflow 白名单周期性自愈: 配置被外部改写/Heartflow 重载后
                # 最多 5 分钟内恢复一致 (启动/策略写入/热更新已有即时同步)。
                if now - self._last_heartflow_sync >= 300:
                    self._last_heartflow_sync = now
                    self._sync_heartflow_whitelist("periodic")
                if (
                    self._filtered_since_report
                    or self._drop_since_report
                    or self._ignored_since_report
                    or self._observe_since_report
                ) and now - self._last_filter_report >= FILTER_REPORT_INTERVAL:
                    parts = []
                    if self._filtered_since_report:
                        parts.append(f"过滤 {self._filtered_since_report} 条 (白名单/群策略)")
                    if self._observe_since_report:
                        parts.append(
                            f"主动观察放行 {self._observe_since_report} 条 (未@, 由决策插件判断)"
                        )
                    if self._ignored_since_report:
                        detail = ", ".join(
                            f"{k}={v}" for k, v in sorted(self._ignored_since_report.items())
                        )
                        parts.append(
                            f"忽略系统/支付/状态 {sum(self._ignored_since_report.values())} 条 ({detail})"
                        )
                    if self._drop_since_report:
                        parts.append(f"队列溢出丢弃 {self._drop_since_report} 条")
                    logger.info(
                        f"[WPP:{self.account_id}] 近 {int(now - self._last_filter_report)}s: "
                        + ", ".join(parts)
                    )
                    self._filtered_since_report = 0
                    self._ignored_since_report = {}
                    self._drop_since_report = 0
                    self._observe_since_report = 0
                    self._last_filter_report = now
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[WPP:{self.account_id}] 维护循环异常: {e}")

    # ------------------------------------------------------------------ filehelper 命令
    async def _handle_filehelper_command(self, content: str) -> None:
        async def reply(text: str) -> None:
            try:
                await self._api.send_text("filehelper", text)
                logger.info(f"[WPP:{self.account_id} FILEHELPER] → {text[:60]}")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[WPP:{self.account_id} FILEHELPER] 回发失败: {e}")

        await self.filehelper.handle(content, reply)

    # ------------------------------------------------------------------ 其他
    @property
    def stats(self) -> dict[str, Any]:
        return {
            "id": self.account_id,
            "status": dict(self._account_status),
            "queue_size": self._queue.qsize() if self._queue else 0,
            "dedup_size": len(self._seen_msg_ids),
            "send": dict(getattr(self._api, "send_stats", {}) or {}),
            # 启动以来的累计计数 (过滤/忽略/队列溢出/主动观察), 排查丢消息用
            "inbound": {
                "filtered": self._total_filtered,
                "ignored": self._total_ignored,
                "dropped": self._total_dropped,
                "observed": self._total_observed,
            },
            "lazy_media": self.lazy_media,
        }


__all__ = ["WppAccount"]
