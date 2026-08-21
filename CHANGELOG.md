# Changelog

## v0.1.0 (2026-08-21)

AstrBot 微信 WPP (WeChatPadPro) 平台适配器首个可用版本，已开源。

### 功能
- **文本收发闭环**：微信文本消息 → vendor push → webhook → AstrBot 事件总线 → AI 回复 → SendTxt → 微信
- **图片接收**：v1 图片消息 CDN 完整大图下载 (`/Tools/CdnDownloadImage`) + DownloadImg 兜底，注入 AI 消息链
- **语音转文字**：vendor 语音消息自带 `voice.transcript`（微信官方转写），直接作文本进 AI，无需 STT
- **文件接收**：`file.download_context` → `DownloadFileBinary` 下载 → `File` 组件注入，AI 可读取内容
- **视频接收**：`video.download_context` → `DownloadVideo` 分片下载 → `Video` 组件注入
- **多消息遍历**：vendor 一次推送多条（count=N），遍历处理不再漏消息
- **白名单**：`wpp_allow_users` 逗号分隔 wxid，私聊仅白名单触发 AI
- **群消息策略**：`wpp_group_reply` = atbot / none / all（atbot 只回 @机器人）
- **账号信息页面**：Dashboard 插件 Pages，查昵称/wxid/地区/签名/邮箱/手机/在线状态
- **自动心跳**：账号 offline 时自动调 `/Login/AutoHeartBeat` 拉上线
- **在线状态**：`get_stats()` 返回 `account_status`（供卡片/API）
- **图标**：`logo.png`（插件 + 平台适配器共用）
- **`wpp_status` 命令**：查询账号在线状态

### 架构
- `main.py` — 插件入口 (Star) + Web API (account-info)
- `wpp_adapter.py` — 平台适配器（webhook + 消息转换 + 白名单 + 发送 + 心跳）
- `wpp_client.py` — WPP vendor API 封装（retry + 超时）
- `wpp_event.py` — `WppMessageEvent` 重写 `send()` 真正发消息
- `pages/account-info/` — Dashboard 账号信息页面

### 关键契约
- WPP API 端口 18062（`wechatpadpromax`；18080 是 mitmdump 代理）
- 鉴权 `X-Access-Token` header = authcode（同一值，双保险都注入）
- vendor push 为 v1 `messages` 格式（sender_id/recipient_id/type/is_group）
- 图片消息 `image.cdn_download_contexts` → CdnDownloadImage

### 踩坑记录
1. AstrBot 插件目录是 namespace package → main.py 顶层 import 适配器
2. `register_star` 签名 `(name, author, desc, version, repo)`
3. 平台适配器插件必须同时有 Star 类
4. 插件 Web API 响应不能有顶层 `data` 字段（前端解构 `r.data.data` 冲突）→ 用 `account_data`
5. 插件日志必须用 `from astrbot import logger`（否则 plugin_tag 报错）
6. AstrBot 回复走 `event.send()`，基类不真正发送 → 平台子类重写
7. DownloadImg 用 `local_id`（uint32）不是 svr_id；CDN 路径优先
8. `LongLinkStatus.state:disconnected` 不可靠，以实际收发为准
9. Dashboard 卡片标题用 `s.type||s.id` 硬编码，display_name 改不动卡片

### 已开源
- GitHub: https://github.com/jiexiaoyin/astrbot-plugin-wpp
- 脱敏：宿主 IP→127.0.0.1，authcode/wxid 泛化，单 commit 无敏感历史
