"""astrbot-plugin-wpp 插件入口。

官方平台适配器插件范式 (docs/zh/dev/plugin-platform-adapter.md):
  - main.py 必须有 Star 类 (类名以 Plugin 结尾会被自动发现)
  - 平台适配器在独立文件 wpp_adapter.py, 用 @register_platform_adapter 装饰
  - 在 __init__ 里 import 适配器模块, 装饰器会自动注册

微信消息流: 微信 → WPP vendor → WS / webhook → 本插件 → AstrBot 核心 → 回微信
"""

import asyncio

from astrbot import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, json_response, request

from .wpp_client import WppClient
from .wpp_media import enrich_media

# 触发平台适配器注册: AstrBot 加载插件目录是 namespace package,
# 不执行 __init__ 里的 import; 必须在 main 模块顶层 import wpp_adapter,
# 才能让 @register_platform_adapter 装饰器执行注册。
from . import wpp_adapter  # noqa: F401  (must be at module top level)


PLUGIN_NAME = "astrbot-plugin-wpp"


class WppPlugin(Star):
    def __init__(self, context: Context) -> None:
        super().__init__(context)
        # 注册插件 Pages 后端 Web API (账号信息查询)
        context.register_web_api(
            f"/{PLUGIN_NAME}/account-info",
            self.web_account_info,
            ["POST"],
            "查询 WPP 账号在线信息 (X-Access-Token)",
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

        前端 Pages: pages/account-info/index.html
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
