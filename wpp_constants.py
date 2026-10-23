"""平台标识常量。

AstrBot 4.29 Dashboard 的机器人列表/新增下拉直接显示平台技术名和配置
`id`/`type`，不读取 `adapter_display_name`；因此这里按 Dashboard 展示需求
使用「个人微信（Pad协议）」作为技术名。运行时目录仍使用稳定的 ASCII
`wpp` 前缀，避免中文 id 被净化后互相冲突。
"""

PLATFORM_NAME = "个人微信（Pad协议）"
PLATFORM_DISPLAY_NAME = "个人微信（Pad协议）"
#: 运行时目录/webhook token 使用的稳定 ASCII 前缀（不是平台技术名）。
RUNTIME_ID_SLUG = "wpp"
#: 平台适配器图标（机器人添加/列表）。插件扩展列表仍使用根目录 logo.png。
PLATFORM_LOGO_PATH = "logo-platform.png"
