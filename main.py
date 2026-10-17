"""astrbot-plugin-wpp 插件入口。

官方平台适配器插件范式 (docs/zh/dev/plugin-platform-adapter.md):
  - main.py 必须有 Star 类 (类名以 Plugin 结尾会被自动发现)
  - 平台适配器在独立文件 wpp_adapter.py, 用 @register_platform_adapter 装饰
  - 在 __init__ 里 import 适配器模块, 装饰器会自动注册

微信消息流: 微信 → WPP vendor → WS / webhook → 本插件 → AstrBot 核心 → 回微信
"""

import asyncio
import re

from astrbot import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import PermissionType
from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, json_response, request

from .wpp_client import WppClient
from .wpp_commands import plugin_version
from .wpp_heartflow import configure as configure_heartflow_sync
from .wpp_heartflow import resolve_heartflow_groups
from .wpp_media import enrich_media

# 触发平台适配器注册: AstrBot 加载插件目录是 namespace package,
# 不执行 __init__ 里的 import; 必须在 main 模块顶层 import wpp_adapter,
# 才能让 @register_platform_adapter 装饰器执行注册。
from . import wpp_adapter  # noqa: F401  (must be at module top level)


PLUGIN_NAME = "astrbot-plugin-wpp"


class WppPlugin(Star):
    def __init__(self, context: Context) -> None:
        super().__init__(context)
        # Heartflow 白名单同步需要一个能访问已加载插件的 Context,
        # 平台适配器本身没有 context, 在这里注入给 wpp_heartflow。
        configure_heartflow_sync(context)
        # 注册插件 Pages 后端 Web API (账号信息查询)
        context.register_web_api(
            f"/{PLUGIN_NAME}/account-info",
            self.web_account_info,
            ["POST"],
            "查询 WPP 账号在线信息 (X-Access-Token)",
        )
        # 策略管理页: 查看/修改各账号白名单、群策略、主动候选群
        context.register_web_api(
            f"/{PLUGIN_NAME}/policy",
            self.web_policy_query,
            ["POST"],
            "查询 WPP 账号策略",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/policy/update",
            self.web_policy_update,
            ["POST"],
            "更新 WPP 账号策略",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/heartflow-sync",
            self.web_heartflow_sync,
            ["POST"],
            "立即同步 WPP 群白名单到 Heartflow",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/status-refresh",
            self.web_status_refresh,
            ["POST"],
            "刷新 WPP 账号在线状态",
        )

    @filter.on_waiting_llm_request()
    async def wpp_lazy_media(self, event: AstrMessageEvent):
        """未 @ 群消息的媒体懒加载: 只在真正要调用 LLM 前下载。

        WPP 适配器对主动候选群的未 @ 消息只构造占位组件; 这个钩子在
        `collect_initial_request` 之前执行, 补齐图片/文件/视频, 避免群里
        每条消息都提前下载媒体。语音已在适配器层即时下载 (保证 STT)。
        """
        if event.get_platform_name() != "wpp":
            return
        account = event.get_extra("_wpp_lazy_media")
        if account is None:
            return
        event.set_extra("_wpp_lazy_media", None)  # 只消费一次
        msg = getattr(event, "message_obj", None)
        if msg is None:
            return
        try:
            paths = await enrich_media(account._api, msg, exclude={"voice"})
            for path in paths:
                event.track_temporary_local_file(path)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"[WPP:{getattr(account, 'account_id', '-')}] 懒加载媒体失败: {e}"
            )

    async def web_account_info(self):
        """输入 X-Access-Token, 返回该 token 绑定的微信账号在线状态/wxid。

        前端 Pages: pages/diagnostics/index.html
        """
        payload = await request.json(default={})
        payload = payload if isinstance(payload, dict) else {}
        token = str(payload.get("token", "") or "").strip()
        base_url = str(payload.get("base_url", "") or "").strip()
        instance_id = str(payload.get("instance_id", "") or "").strip()
        if not token:
            return error_response("缺少 X-Access-Token")

        # SSRF 防护: base_url 只允许使用已配置的适配器地址, 不接受任意 URL
        configured = self._configured_base_urls()
        if base_url:
            base_url = base_url.rstrip("/")
            if not configured:
                return error_response("尚未配置 WPP 平台适配器, 无法校验 base_url")
            if base_url not in configured:
                return error_response("base_url 不在已配置的 WPP 地址列表中")
        else:
            base_url = self._get_wpp_base_url(instance_id or None).rstrip("/")

        client = WppClient(base_url, token)
        panel_url = base_url
        try:
            online, resp = await client.is_online()
            data = resp.get("Data") or {}
            if online:
                prof = await self._fetch_profile(client)
                resp_payload = {
                    "online": True,
                    "account_data": data,
                    "message": resp.get("Message", "成功"),
                    "panel_url": panel_url,
                }
                resp_payload["profile"] = prof
                # 扁平化: 顶层也放 profile 字段, 前端可直接读 resp.nickname
                if prof:
                    resp_payload.update(prof)
                return json_response(resp_payload)
            # 不在线: 尝试自动心跳拉上线 (账号已登录但长连接未建立)
            if resp.get("Code") == 0:
                await client.auto_heartbeat()
                await asyncio.sleep(2)
                online, resp = await client.is_online()
                if online:
                    data = resp.get("Data") or {}
                    prof = await self._fetch_profile(client)
                    resp_payload = {
                        "online": True,
                        "account_data": data,
                        "message": f"自动心跳已拉起: {resp.get('Message', '')}",
                        "panel_url": panel_url,
                    }
                    resp_payload["profile"] = prof
                    if prof:
                        resp_payload.update(prof)
                    return json_response(resp_payload)
            # 未登录/离线
            return json_response(
                {
                    "online": False,
                    "account_data": data,
                    "profile": None,
                    "message": resp.get("Message", "未登录或缓存缺失"),
                    "auto_heartbeat": resp.get("Code") == 0,
                    "panel_url": panel_url,
                }
            )
        except Exception as e:  # noqa: BLE001
            return error_response(f"查询失败: {e}")
        finally:
            await client.close()

    # ------------------------------------------------------------------ 策略管理
    @staticmethod
    def _account_policy_view(acct) -> dict:
        auto_proactive = sorted(
            str(u) for u in acct.allow_users if str(u).endswith("@chatroom")
        )
        return {
            "id": acct.account_id,
            "allow_users": sorted(acct.allow_users),
            "block_users": sorted(acct.block_users),
            "blacklist_groups": sorted(acct.blacklist_groups),
            "proactive_groups": sorted(acct.proactive_groups),
            "auto_proactive_groups": auto_proactive,
            "effective_proactive_groups": sorted(
                set(acct.proactive_groups) | set(auto_proactive)
            ),
            "group_reply": acct.group_reply,
            "require_at_mention": acct.require_at_mention,
            "lazy_media": bool(getattr(acct, "lazy_media", True)),
            "stats": acct.stats,
        }

    def _policy_instances(self) -> list[dict]:
        instances = []
        for inst in self._get_wpp_instances():
            accounts = list(getattr(inst, "accounts", []) or [])
            mode = str(
                getattr(inst, "heartflow_whitelist_mode", "auto") or "auto"
            )
            try:
                targets, rejected = resolve_heartflow_groups(
                    accounts,
                    getattr(inst, "heartflow_groups", set()) or set(),
                    mode,
                )
            except Exception:  # noqa: BLE001
                targets, rejected = set(), set()
            instances.append(
                {
                    "id": getattr(inst, "instance_id", ""),
                    "heartflow_sync_enabled": bool(
                        getattr(inst, "heartflow_sync_enabled", True)
                    ),
                    "heartflow_mode": mode,
                    "heartflow_custom_groups": sorted(
                        str(g) for g in (getattr(inst, "heartflow_groups", set()) or set())
                    ),
                    "heartflow_target_groups": sorted(targets),
                    "heartflow_rejected_groups": sorted(rejected),
                    "accounts": [self._account_policy_view(a) for a in accounts],
                }
            )
        return instances

    def _find_account(self, instance_id: str = "", account_id: str = ""):
        for inst in self._get_wpp_instances():
            if instance_id and getattr(inst, "instance_id", "") != instance_id:
                continue
            for acct in getattr(inst, "accounts", []):
                if not account_id or acct.account_id == account_id:
                    return inst, acct
        return None, None

    async def web_policy_query(self):
        return json_response(
            {
                "version": plugin_version(),
                "instances": self._policy_instances(),
            }
        )

    async def web_policy_update(self):
        payload = await request.json(default={})
        payload = payload if isinstance(payload, dict) else {}
        inst, acct = self._find_account(
            str(payload.get("instance_id", "") or "").strip(),
            str(payload.get("account_id", "") or "").strip(),
        )
        if acct is None:
            return error_response("未找到 WPP 账号")
        # 先全部校验再落盘: 避免某个字段校验失败时, 前面的字段已经被
        # 改到内存里却没有保存, 造成"页面提示失败但运行时策略已部分生效"。
        updates = {}
        for field in (
            "allow_users",
            "block_users",
            "blacklist_groups",
            "proactive_groups",
        ):
            if field not in payload:
                continue
            value = payload.get(field)
            if isinstance(value, str):
                # 控制面板支持逗号/换行/分号分隔, 与 /help 的逗号写法兼容。
                value = re.split(r"[\s,，;；]+", value)
            if not isinstance(value, (list, tuple, set)):
                return error_response(f"{field} 必须是数组")
            items = {str(v).strip() for v in value if str(v).strip()}
            # 服务端与页面保持同一套校验, 避免脚本/第三方调用写入
            # 语义错误的数据 (例如把群 ID 填进私聊黑名单)。
            if field in ("blacklist_groups", "proactive_groups"):
                bad = sorted(v for v in items if not v.endswith("@chatroom"))
                if bad:
                    return error_response(
                        f"{field} 只能包含以 @chatroom 结尾的群 ID："
                        + ", ".join(bad[:5])
                    )
            if field == "block_users":
                bad = sorted(v for v in items if v.endswith("@chatroom"))
                if bad:
                    return error_response(
                        "block_users 是私聊黑名单；群 ID 请填到 blacklist_groups："
                        + ", ".join(bad[:5])
                    )
            updates[field] = items
        if "group_reply" in payload:
            mode = str(payload.get("group_reply", "") or "").strip()
            if mode not in ("atbot", "none", "all"):
                return error_response("group_reply 只支持 atbot / none / all")
            updates["group_reply"] = mode
        if "require_at_mention" in payload:
            updates["require_at_mention"] = bool(payload.get("require_at_mention"))
        for field, value in updates.items():
            setattr(acct, field, value)
        if updates:
            acct.save_policy()
        if inst is not None:
            inst.sync_heartflow_whitelist("dashboard")
        return json_response(
            {"ok": True, "account": self._account_policy_view(acct)}
        )

    async def web_heartflow_sync(self):
        payload = await request.json(default={})
        payload = payload if isinstance(payload, dict) else {}
        target = str(payload.get("instance_id", "") or "").strip()
        synced = []
        for inst in self._get_wpp_instances():
            if target and getattr(inst, "instance_id", "") != target:
                continue
            inst.sync_heartflow_whitelist("dashboard")
            synced.append(getattr(inst, "instance_id", ""))
        return json_response({"ok": True, "synced": synced})

    async def web_status_refresh(self):
        """主动查询账号在线状态, 供控制面板「刷新状态」使用。

        状态本身由适配器每 30s 刷新一次; 这里用于打开面板/排障时立即拉取,
        不返回 authcode 等敏感配置。
        """
        payload = await request.json(default={})
        payload = payload if isinstance(payload, dict) else {}
        instance_id = str(payload.get("instance_id", "") or "").strip()
        account_id = str(payload.get("account_id", "") or "").strip()
        tasks = []
        refreshed = []
        for inst in self._get_wpp_instances():
            if instance_id and getattr(inst, "instance_id", "") != instance_id:
                continue
            refresh = getattr(inst, "_refresh_account_status", None)
            if not callable(refresh):
                continue
            for acct in getattr(inst, "accounts", []) or []:
                if account_id and acct.account_id != account_id:
                    continue
                tasks.append(refresh(acct))
                refreshed.append(
                    {
                        "instance_id": getattr(inst, "instance_id", ""),
                        "account_id": acct.account_id,
                    }
                )
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
        else:
            results = []
        failed = []
        for item, result in zip(refreshed, results):
            if isinstance(result, BaseException):
                failed.append({**item, "error": "刷新失败"})
                logger.warning(
                    f"[WPP] 刷新账号状态失败: instance={item['instance_id']} "
                    f"account={item['account_id']}: {result}"
                )
        return json_response(
            {
                "ok": not failed,
                "refreshed": refreshed,
                "failed": failed,
                "version": plugin_version(),
                "instances": self._policy_instances(),
            }
        )

    def _get_wpp_instances(self) -> list:
        """返回所有已注册的 wpp 平台适配器实例。"""
        try:
            pm = getattr(self.context, "platform_manager", None)
            insts = getattr(pm, "platform_insts", []) if pm else []
            return [
                inst for inst in insts
                if getattr(inst, "meta", None) and inst.meta().name == "wpp"
            ]
        except Exception:  # noqa: BLE001
            return []

    def _configured_base_urls(self) -> set[str]:
        """已配置的 WPP vendor 地址集合 (web API 的 base_url 白名单)。"""
        urls: set[str] = set()
        for inst in self._get_wpp_instances():
            base = str(getattr(inst, "base_url", "") or "").rstrip("/")
            if base:
                urls.add(base)
        return urls

    def _get_wpp_base_url(self, instance_id: str | None = None) -> str:
        """从已注册的 wpp 平台适配器配置读取 base_url。

        instance_id 指定实例 (meta().id); 缺省取第一个 (单实例兼容)。
        """
        try:
            insts = self._get_wpp_instances()
            if not insts:
                return "http://127.0.0.1:18062"
            for inst in insts:
                if instance_id and getattr(inst, "instance_id", "") == instance_id:
                    return str(getattr(inst, "base_url", "") or "http://127.0.0.1:18062")
            return str(getattr(insts[0], "base_url", "") or "http://127.0.0.1:18062")
        except Exception:  # noqa: BLE001
            return "http://127.0.0.1:18062"

    @staticmethod
    async def _fetch_profile(client) -> dict | None:
        """拉取 GetContractProfile 并提取昵称/wxid/地区/签名等。失败返回 None。"""
        try:
            prof = await client.get_contract_profile()
            if prof.get("Code") != 0 or prof.get("Success") is False:
                return None
            ui = (prof.get("Data") or {}).get("userInfo") or {}

            def s(v):
                # 处理 {"string": "xxx"} 或 "xxx"
                if isinstance(v, dict):
                    return str(v.get("string", ""))
                return str(v) if v is not None else ""

            return {
                "wxid": s(ui.get("UserName")),
                "nickname": s(ui.get("NickName")),
                "alias": s(ui.get("Alias")),
                "sex": ui.get("Sex"),
                "province": s(ui.get("Province")),
                "city": s(ui.get("City")),
                "country": s(ui.get("Country")),
                "signature": s(ui.get("Signature")),
                "email": s(ui.get("BindEmail")),
                "mobile": s(ui.get("BindMobile")),
            }
        except Exception:  # noqa: BLE001
            return None

    @filter.command("wpp_status")
    async def wpp_status(self, event: AstrMessageEvent):
        """查询 WPP 账号在线状态 (多账号: /wpp_status [实例id|账号id])。"""
        insts = self._get_wpp_instances()
        if not insts:
            yield event.plain_result(
                "WPP 适配器未启动。请先在 Dashboard 平台适配器里添加并启动 '微信 WPP'。"
            )
            return

        parts = event.message_str.strip().split()
        target = parts[1].strip() if len(parts) > 1 else ""

        # 目标可以是实例 id (列出该实例全部账号) 或账号 id (只列该账号)
        targets = []
        for inst in insts:
            if not target or getattr(inst, "instance_id", "") == target:
                targets.extend(getattr(inst, "accounts", []))
            else:
                targets.extend(
                    a for a in getattr(inst, "accounts", [])
                    if a.account_id == target
                )
        if not targets:
            yield event.plain_result(f"未找到 WPP 实例/账号: {target}")
            return

        lines = ["【WPP 账号状态】"]
        for acct in targets:
            try:
                online = await acct._api.get_online_info()
                longlink = await acct._api.get_long_link_status()
            except Exception as e:  # noqa: BLE001
                lines.append(f"⚠️ {acct.account_id}: 查询失败 ({e})")
                continue
            if online.get("Code") == 0:
                data = online.get("Data") or {}
                wxid = data.get("wxid", "")
                online_flag = data.get("online")
                icon = "✅" if online_flag is True else "⚠️"
                lines.append(f"{icon} {acct.account_id}: wxid={wxid or '-'} online={online_flag}")
            else:
                lines.append(f"❌ {acct.account_id}: {online.get('Message', '未登录/离线')}")
            lines.append(
                f"   长连接: {longlink.get('Message', longlink.get('Data', '无数据'))}"
            )
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("wpp_policy")
    async def wpp_policy(self, event: AstrMessageEvent):
        """查看/修改 WPP 账号策略 (仅 AstrBot 管理员)。

        用法:
          /wpp_policy                         查看全部账号
          /wpp_policy <账号> <字段> add <值...>
          /wpp_policy <账号> <字段> del <值...>
          /wpp_policy <账号> <字段> set <值...>
          /wpp_policy <账号> group_reply set atbot|none|all
          /wpp_policy <账号> require_at_mention set true|false

        字段: allow_users / block_users / blacklist_groups / proactive_groups /
              group_reply / require_at_mention
        """
        parts = event.message_str.strip().split()
        if len(parts) <= 1:
            lines = ["【WPP 账号策略】"]
            for inst in self._policy_instances():
                lines.append(f"实例 {inst['id']} (Heartflow 同步={inst['heartflow_sync_enabled']})")
                for acct in inst["accounts"]:
                    lines.append(
                        f"  {acct['id']}: group_reply={acct['group_reply']} "
                        f"allow={acct['allow_users']} "
                        f"proactive={acct['effective_proactive_groups']}"
                    )
                    lines.append(
                        f"    block={acct['block_users']} "
                        f"blacklist={acct['blacklist_groups']} "
                        f"require_at={acct['require_at_mention']}"
                    )
            yield event.plain_result("\n".join(lines))
            return

        if len(parts) < 4:
            yield event.plain_result(
                "用法: /wpp_policy <账号> <字段> <add|del|set> <值...>"
            )
            return

        account_id, field, action = parts[1], parts[2], parts[3].lower()
        values = parts[4:]
        inst, acct = self._find_account(account_id=account_id)
        if acct is None:
            yield event.plain_result(f"未找到 WPP 账号: {account_id}")
            return

        list_fields = {
            "allow_users",
            "block_users",
            "blacklist_groups",
            "proactive_groups",
        }
        if field in list_fields:
            target = set(getattr(acct, field))
            if action == "set":
                target = set(values)
            elif action == "add":
                target.update(values)
            elif action == "del":
                target.difference_update(values)
            else:
                yield event.plain_result("列表字段只支持 add / del / set")
                return
            setattr(acct, field, target)
        elif field == "group_reply":
            value = values[0].lower() if values else ""
            if value not in ("atbot", "none", "all"):
                yield event.plain_result("group_reply 只支持 atbot / none / all")
                return
            acct.group_reply = value
        elif field == "require_at_mention":
            value = (values[0].lower() if values else "").strip()
            if value not in ("true", "false", "1", "0", "on", "off"):
                yield event.plain_result("require_at_mention 只支持 true/false")
                return
            acct.require_at_mention = value in ("true", "1", "on")
        else:
            yield event.plain_result(f"不支持的字段: {field}")
            return

        acct.save_policy()
        if inst is not None:
            inst.sync_heartflow_whitelist("command")
        yield event.plain_result(
            f"已更新 {account_id}: group_reply={acct.group_reply} "
            f"allow={sorted(acct.allow_users)} "
            f"proactive={self._account_policy_view(acct)['effective_proactive_groups']} "
            f"require_at={acct.require_at_mention}"
        )

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("wpp_sync")
    async def wpp_sync(self, event: AstrMessageEvent):
        """立即把 WPP 群白名单同步到 Heartflow (仅 AstrBot 管理员)。"""
        parts = event.message_str.strip().split()
        target = parts[1].strip() if len(parts) > 1 else ""
        synced = []
        for inst in self._get_wpp_instances():
            if target and getattr(inst, "instance_id", "") != target:
                continue
            inst.sync_heartflow_whitelist("command")
            synced.append(getattr(inst, "instance_id", ""))
        if not synced:
            yield event.plain_result(f"未找到 WPP 实例: {target}")
            return
        yield event.plain_result(f"已同步 Heartflow 白名单: {', '.join(synced)}")
