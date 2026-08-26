# astrbot-plugin-wpp

AstrBot 微信 WPP (WeChatPadPro) 平台适配器。

**让微信成为 AstrBot 的一等平台**：微信消息 → WPP vendor → 本适配器 → AstrBot 核心 (LLM/命令/插件)，回复自动回微信。

> 🚀 **核心亮点**：**WS 实时主通道**（不断连、自动重连）+ **单实例多账号**（一个适配器管多个微信，通过 authcode 隔离）+ **全类型媒体**（图片/语音/文件/视频全部 AI 可读）+ **filehelper 命令**（文件助手发指令管理白名单）。

---

## ✨ 插件亮点

### 🔌 WS 实时主通道（稳定收消息）
- **WS 实时连接**：连 vendor `/ws/sync`，连接即拉全量离线 + 实时消息，**不丢消息**
- **自动重连**：断连指数退避（1s→30s），长时间无活动也不掉线
- **webhook 已退役**：不再依赖 vendor 回调，WS 主通道更可靠

### 👥 单实例多账号（一个插件管多个微信）
- **同一 vendor 地址**（`wpp_base_url` + `wpp_ws_url`），通过不同 **authcode** 隔离账号
- **每账号独立**：WS 连接 / 白名单 / 群策略 / 黑名单 / 去重 / 机器人身份 / filehelper 命令
- **配置极简**：`wpp_accounts` JSON 列表，每个账号 `{id, authcode, allow_users, ...}`
- 单账号向后兼容（`wpp_auth_token`）

### 📦 全类型消息收发（AI 全能读懂）
| 类型 | 处理 | 说明 |
|---|---|---|
| 文本 | 直接收发 | 最基础的对话 |
| 图片 | CDN 完整下载 | `/Tools/CdnDownloadImage` 完整大图注入 AI |
| 语音 | vendor 转写 | 自带 `voice.transcript`（微信官方），**无需 STT** |
| 文件 | 下载 + AI 读取 | `DownloadFileBinary` → `File` 组件，AI 可读 Excel/PDF |
| 视频 | 分片下载 | `DownloadVideo` 分片 → `Video` 组件 |

### 🛡 完善的接入能力
- **白名单**：仅白名单 wxid 触发 AI（防陌生人骚扰）
- **群消息策略**：`atbot`（只回@机器人）/ `none` / `all`
- **filehelper 白名单命令**：在文件传输助手里发指令管理白名单（仿 wpp-openclaw），三域统一 **add/del/list**
  - `/user add|del <wxid> [wxid...]` 私聊白名单（支持批量）/ `/user list` 查看
  - `/group add|del <群ID> [群ID...]` 群白名单（支持批量）/ `/group list` 查看
  - `/blacklist add|del|list <群ID> [群ID...]` 黑名单群（黑白名单**互斥**，加一边自动移另一边）
  - `/mode atbot|none|all` 群回复模式热切
  - `/at on|off` 群聊是否必须 @ 才回复
  - `/help` 显示全部命令（**自动遍历命令注册表**，新命令自动进 help）— 运行时立即生效，**持久化**（重启不丢），无需改配置重启
  - **配置文件热更新**：手动编辑 `wpp_whitelist.json` 也自动生效（约 3 秒检测），无需重启
- **多消息遍历**：vendor 一次推多条不遗漏
- **自动心跳**：账号离线自动拉上线
- **动态配置**：换 vendor 地址/authcode 自动适配（无硬编码）

### 🖥 运维体验
- **账号信息页面**：Dashboard 里查昵称/wxid/在线状态
- **在线状态卡片**：平台状态实时显示（多账号聚合）
- **动态面板地址**：UI 显示实际配置的 vendor 地址（不硬编码）

---

## 🏗 架构

```
[微信] ←→ [WPP vendor (WeChatPadPro)]
                │ ① WS 实时通道 (wss://vendor/ws/sync?authcode=账号)
                │    每账号一个独立 WS 连接 (通过 authcode 隔离)
                ▼
[astrbot 容器] astrbot-plugin-wpp (Platform 适配器)
                │ WppAccount (每账号: WS 连接 + 白名单 + 去重 + 身份)
                │ ② 解析 → WppMessageEvent → 事件总线
                ▼
        AstrBot 核心 (LLM/命令/插件)
                │ ③ event.send → 对应账号 WPP 发送 API (SendTxt/UploadImg...)
                ▼
        微信收到回复
```

- **主通道 = WS 实时连接**：连接建立即拉全量离线消息 + 实时推送，自动重连保活（webhook 已退役）
- **多账号 = 单实例内多个 WppAccount**：同一 vendor 地址，每个账号独立 authcode + 独立 WS + 独立白名单

---

## 📦 文件结构

| 文件 | 作用 |
|---|---|
| `main.py` | 插件入口 (Star) + Web API (账号信息查询) |
| `wpp_adapter.py` | 平台适配器 (账号管理器 + 账号解析 + 生命周期 + 发送路由) |
| `wpp_account.py` | WppAccount 类 (单个微信账号: WS 连接 + 白名单 + 消息处理 + filehelper) |
| `wpp_client.py` | WPP vendor HTTP/WS API 封装 (retry + 超时) |
| `wpp_event.py` | 消息事件 (重写 send 真正发送到微信) |
| `tests/` | 多账号架构测试 (pytest) |
| `pages/account-info/` | Dashboard 账号信息页面 |
| `logo.png` | 插件 + 平台适配器图标 |

---

## 🚀 部署

```bash
# 1. 拷入 AstrBot 容器插件目录
docker cp astrbot-plugin-wpp astrbot:/AstrBot/data/plugins/

# 2. 容器内确认依赖 (aiohttp + websockets — WS 主通道必需)
docker exec astrbot sh -c 'python3 -c "import aiohttp, websockets" || pip install aiohttp websockets'

# 3. 重启 AstrBot 加载适配器
docker restart astrbot

# 4. Dashboard (http://<host>:6185) → 平台适配器 → 添加 "微信 WPP"
#    填配置 (见下表) → 保存启用
```

---

## ⚙️ 平台配置字段

| 字段 | 必填 | 说明 |
|---|---|---|
| `wpp_base_url` | ✅ | WPP API 地址（如 `http://<vendor-ip>:28062`）|
| `wpp_auth_token` | 二选一 | **单账号模式**：WPP X-Access-Token（= authcode）|
| `wpp_accounts` | 二选一 | **多账号模式**：账号列表 JSON（见下方多账号章节）|
| `wpp_allow_users` | — | **单账号模式**白名单 wxid（逗号分隔），空则所有私聊触发 |
| `wpp_group_reply` | — | **单账号模式**群消息策略: `atbot` / `none` / `all` |
| `wpp_ws_url` | — | WS 实时通道地址（如 `wss://<vendor-domain>/ws/sync`），主通道 |
| `wpp_webhook_enabled` | — | 是否启用 webhook 兜底（默认 `false`，仅 WS）|

> ⚠️ **单账号 vs 多账号**：配置 `wpp_auth_token`（+ 顶层白名单）= 单账号；配置 `wpp_accounts`（JSON 列表）= 多账号。两者只用一个。

### 👥 多账号（单实例内, 通过 authcode 隔离）
一个 wpp 适配器实例可管理多个微信账号，**同一 vendor 地址**（`wpp_base_url` + `wpp_ws_url`），通过不同 `authcode` 隔离。每个账号独立：
- WS 实时连接（用该账号 authcode 鉴权）
- 白名单/群策略/黑名单（`accounts/<account_id>/wpp_whitelist.json` 独立持久化）
- filehelper 命令（各自管理自己的白名单）
- 消息去重 / 机器人身份

`wpp_accounts` 配置示例：
```json
[
  {"id": "xieyin", "authcode": "<authcode_账号1>", "allow_users": "<wxid_白名单>", "group_reply": "atbot"},
  {"id": "yirong", "authcode": "<authcode_账号2>", "allow_users": "", "group_reply": "all", "blacklist_groups": "<群id>@chatroom"},
  {"id": "test",  "authcode": "xxx", "require_at_mention": true}
]
```
每个账号字段：`id`（唯一）、`authcode`（必填）、`allow_users`/`group_reply`/`blacklist_groups`/`require_at_mention`（可选，默认同单账号）。

filehelper 命令输出带账号标识（如 `【WPP 命令 (账号 xieyin)】`），各账号命令互不影响。

### 换新环境的自动适配
配置 `wpp_base_url` + `wpp_auth_token` 后，插件**自动**：
1. 用新地址调所有 WPP API
2. 用新 authcode 认证
3. WS 连接建立即拉全量离线 + 实时消息（主通道）
4. 面板/UI 显示新地址

> ⚠️ 前提：新 authcode 需在新 vendor 的 Redis 中为有效状态（`origin=local` + `enabled`），且新 vendor 能真实收发微信。详见 [DEPLOY.md](DEPLOY.md) 的接入清单。

---

## 🔐 凭证安全

- `auth_token` 走 Dashboard WebUI 配置 (`platform_config`)，**不写进代码/日志**
- 日志不打印明文凭证
- 配置字段用 `isSecret` 标记（密码框输入）

---

## 📚 文档

- [部署与运维指南](DEPLOY.md) — 部署/新环境接入清单/故障排查
- [更新日志](CHANGELOG.md) — 版本历史 + 踩坑记录

---

## 🔗 参考

- AstrBot 平台适配器开发文档: `docs/zh/dev/plugin-platform-adapter.md`
- AstrBot 内置范例: `qqofficial_webhook` 适配器 (webhook + commit_event 同构)

## License

MIT
