"""astrbot-plugin-wpp 插件入口。

官方平台适配器插件范式 (docs/zh/dev/plugin-platform-adapter.md):
  - main.py 必须有 Star 类 (否则 star_manager 拒绝加载)
  - 平台适配器在独立文件 wpp_adapter.py, 用 @register_platform_adapter 装饰
  - 在 __init__ 里 import 适配器模块, 装饰器会自动注册

微信消息流: 微信 → WPP vendor → POST webhook → 本插件 → AstrBot 核心 → 回微信
"""

import asyncio

from astrbot.api.star import Context, Star, register
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.web import request, json_response, error_response
from .wpp_client import WppClient

# 触发平台适配器注册: AstrBot 加载插件目录是 namespace package,
# 不执行 __init__ 里的 import; 必须在 main 模块顶层 import wpp_adapter,
# 才能让 @register_platform_adapter 装饰器执行注册。
from . import wpp_adapter  # noqa: F401  (must be at module top level)


PLUGIN_NAME = "astrbot-plugin-wpp"


@register(
    name=PLUGIN_NAME,
    author="jiexiaoyin",
    desc="让微信成为 AstrBot 的一等平台 (WeChatPadPro)",
    version="0.2.0",
    repo="",
)
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

    async def web_account_info(self):
        """输入 X-Access-Token, 返回该 token 绑定的微信账号在线状态/wxid。

        前端 Pages: pages/account-info/index.html
        """
        payload = await request.json(default={})
        token = (payload or {}).get("token", "")
        base_url = (payload or {}).get("base_url", "")
        instance_id = (payload or {}).get("instance_id", "")
        if not token:
            return error_response("缺少 X-Access-Token")

        # base_url 可选; 缺省从已注册的 wpp 适配器配置读取 (多账号 v0.2.0: 支持按 instance_id)
        if not base_url:
            base_url = self._get_wpp_base_url(instance_id or None)
        base_url = str(base_url).rstrip("/")

        client = WppClient(base_url, token)
        # 面板地址: 从 base_url 派生 (前端显示用, 避免硬编码 127.0.0.1:18062)
        panel_url = base_url
        try:
            online, resp = await client.is_online()
            data = resp.get("Data") or {}
            if online:
                prof = await self._fetch_profile(client)
                # ⚠️ 不要用 "data" 字段名! Dashboard 前端解构 r.data.data 会拿到它,
                #    导致 bridge 端收到的是 GetOnlineInfo.Data 而不是完整响应。
                resp_payload = {"online": True, "account_data": data, "message": resp.get("Message", "成功"), "panel_url": panel_url}
                resp_payload["profile"] = prof
                # 扁平化: 顶层也放 profile 字段, 前端可直接读 resp.nickname
                if prof:
                    resp_payload.update(prof)
                return json_response(resp_payload)
            # 不在线: 尝试自动心跳拉上线 (账号已登录但长连接未建立)
            if resp.get("Code") == 0:
                hb = await client.auto_heartbeat()
                await asyncio.sleep(2)
                online, resp = await client.is_online()
                if online:
                    data = resp.get("Data") or {}
                    prof = await self._fetch_profile(client)
                    resp_payload = {"online": True, "account_data": data, "message": f"自动心跳已拉起: {resp.get('Message', '')}", "panel_url": panel_url}
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
        """返回所有已注册的 wpp 平台适配器实例 (多账号 v0.2.0)。

        多实例时按 meta().id 区分 (instance_id)。单实例兼容: 返回 [实例] 或 []。
        """
        try:
            pm = getattr(self.context, "platform_manager", None)
            insts = getattr(pm, "platform_insts", []) if pm else []
            return [
                inst for inst in insts
                if getattr(inst, "meta", None) and inst.meta().name == "wpp"
            ]
        except Exception:  # noqa: BLE001
            return []

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
        """查询 WPP 账号在线状态 (多账号 v0.2.0: 支持 /wpp_status <实例id>)。"""
        # 从已注册的适配器实例拿凭证 (遍历平台管理器里的 wpp 实例)
        insts = self._get_wpp_instances()
        if not insts:
            yield event.plain_result(
                "WPP 适配器未启动。请先在 Dashboard 平台适配器里添加并启动 '微信 WPP'。"
            )
            return

        # 支持 /wpp_status <实例id> 指定实例; 缺省第一个 (单实例兼容)
        target_id = ""
        parts = event.message_str.strip().split()
        if len(parts) > 1:
            target_id = parts[1].strip()
        platform = next((i for i in insts if getattr(i, "instance_id", "") == target_id), insts[0]) if target_id else insts[0]

        online = await platform._api.get_online_info()
        longlink = await platform._api.get_long_link_status()

        inst_label = getattr(platform, "instance_id", "wpp")
        parts = [f"【WPP 账号状态 (实例 {inst_label})】"]
        if online.get("Code") == 0:
            parts.append(f"✅ 在线: {online.get('Data')}")
        else:
            parts.append(f"⚠️ 未登录/离线: {online.get('Message', '')}")
        parts.append(f"长连接: {longlink.get('Message', longlink.get('Data', '无数据'))}")
        yield event.plain_result("\n".join(parts))
