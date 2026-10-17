# astrbot-plugin-wpp

**AstrBot 微信 WPP (WeChatPadPro) 平台适配器 — 让微信成为 AstrBot 的一等平台**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![AstrBot](https://img.shields.io/badge/AstrBot-v4.x-purple)](https://github.com/Soulter/AstrBot)
[![GitHub stars](https://img.shields.io/github/stars/jiexiaoyin/astrbot-plugin-wpp?style=social)](https://github.com/jiexiaoyin/astrbot-plugin-wpp/stargazers)
[![GitHub forks](https://img.shields.io/github/forks/jiexiaoyin/astrbot-plugin-wpp?style=social)](https://github.com/jiexiaoyin/astrbot-plugin-wpp/network)
[![Open Issues](https://img.shields.io/github/issues/jiexiaoyin/astrbot-plugin-wpp)](https://github.com/jiexiaoyin/astrbot-plugin-wpp/issues)
[![Last commit](https://img.shields.io/github/last-commit/jiexiaoyin/astrbot-plugin-wpp)](https://github.com/jiexiaoyin/astrbot-plugin-wpp/commits/master)

AstrBot 微信 WPP (WeChatPadPro) 平台适配器。

**让微信成为 AstrBot 的一等平台**：微信消息 → WPP vendor → 本适配器 → AstrBot 核心 (LLM/命令/插件)，回复自动回微信。

> 🚀 **核心亮点**：**WS 实时主通道**（不断连、自动重连）+ **单实例多账号**（一个适配器管多个微信，通过 authcode 隔离）+ **全类型媒体**（图片/语音/文件/视频全部 AI 可读）+ **主动回复候选群**（未 @ 也放行，由 Heartflow 等插件决定是否接话）+ **filehelper 命令**（文件助手发指令管理白名单）。

---

## 🔗 相关项目

本项目是 WeChatPadPro 跨平台适配插件生态的一部分:

| 项目 | 平台 | 语言 | 状态 |
|---|---|---|---|
| **[wpp-openclaw](https://github.com/jiexiaoyin/wpp-openclaw)** | OpenClaw | TypeScript | ✅ 活跃维护 |
| **[wpp-hermes](https://github.com/jiexiaoyin/wpp-hermes)** | Hermes Agent | Python 3 | ✅ 活跃维护 |
| **[astrbot-plugin-wpp](https://github.com/jiexiaoyin/astrbot-plugin-wpp)** (本仓库) | AstrBot | Python 3 | ✅ 活跃维护 |

三个仓库覆盖同一 vendor API,根据 agent 平台选择:

- 想用 **OpenClaw gateway** 的 LLM → [wpp-openclaw](https://github.com/jiexiaoyin/wpp-openclaw)
- 想用 **Hermes Agent** → [wpp-hermes](https://github.com/jiexiaoyin/wpp-hermes)
- 想用 **AstrBot** 的多平台架构 → 本仓库 (`astrbot-plugin-wpp`)

---

## ✨ 插件亮点

### 🔌 WS 实时主通道（稳定收消息）
- **WS 实时连接**：连 vendor `/ws/sync`，连接即拉全量离线 + 实时消息，**不丢消息**
- **自动重连**：断连指数退避（1s→30s），长时间无活动也不掉线
- **收包与处理解耦**（v0.3.0）：WS 只负责收包入队，媒体下载由单 worker 顺序处理，慢下载不会阻塞 WS ping / 收包
- **非消息事件过滤**（v0.3.0）：只处理 `sync_message` 等消息事件，朋友圈/联系人推送不再被误解析（旧版日志刷屏根因）
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
| 语音 | 转写 + STT 兜底 | 优先 `voice.transcript`（微信官方）；缺失时 `DownloadVoiceBinary` → `Record` → AstrBot 核心 STT |
| 文件 | 下载 + AI 读取 | `DownloadFileBinary` → `File` 组件，AI 可读 Excel/PDF |
| 视频 | 分片下载 | `DownloadVideo` 分片 → `Video` 组件 |
| 链接/小程序 | 结构化摘要 | `[链接]` / `[小程序]`：标题 + 描述 + URL/页面路径，**不传原始 XML** |
| 合并转发/接龙 | 条目化 | `[合并转发]` 逐条发送人+内容；`[接龙]` 保留正文 |
| 位置/名片 | 结构化摘要 | `[位置] 名称+经纬度`；`[名片] 昵称+wxid` |
| 引用回复 | 原生 `Reply` 组件 | 被引用内容进入 AI 上下文 |
| 支付/系统卡片 | 静默 | 红包/转账/公众号/服务通知等不触发 AI（任何模式） |

**出站**：文本（markdown 分片）、图片、文件、语音、视频、`At`/`AtAll`、引用回复（appmsg type=57）、链接卡片（`Share`）、位置（`Location`）。

**回复装饰（AstrBot 标准）**：在 AstrBot「平台设置」中开启
`reply_with_mention`（回复时 @ 发送人）和 `reply_with_quote`（回复时引用原消息）
后，适配器会把核心插入的 `At` / `Reply` 正确落到微信：
- 引用 `refermsg.svrid` 使用入站 `NewMsgId`（不是本地 `msg_id`）；
- 群聊引用气泡没有 `At` 字段，自动在正文前加 `@昵称 `；
  入站缺昵称时按 `At.name` → `Reply.sender_nickname` → 群成员列表 →
  通讯录依次解析，仍查不到则退回 `@wxid`（缓存 30 分钟，超时 5 秒）；
- 普通文本回复使用 vendor `At` 字段。

### 🛡 完善的接入能力
- **系统消息静默（内置，任何模式生效）**：公众号（`gh_`）、微信团队、微信支付、服务通知、财付通、微信安全中心、订阅号消息等系统会话，以及状态/系统/撤回消息、红包/转账卡片，都不会触发 AI
- **私聊黑名单**：`wpp_block_users` / `/block`，全部放行模式下排除指定 wxid，群聊里也屏蔽该成员
- **白名单**：仅白名单 wxid 触发 AI（防陌生人骚扰）
- **群消息准入策略**：`atbot`（只放行 @ 机器人）/ `none` / `all`（放行所有群消息，观察模式，不强制回复）
- **主动回复候选群**：按群 opt-in，未 @ 消息也放行给 AstrBot，但是否回复由 Heartflow / 内置 `active_reply` 决定
- **filehelper 白名单命令**：在文件传输助手里发指令管理白名单（仿 wpp-openclaw），三域统一 **add/del/list**
  - `/user add|del <wxid> [wxid...]` 私聊白名单（支持批量）/ `/user list` 查看
  - `/block add|del|list <wxid> [wxid...]` 私聊黑名单（全部放行模式下排除指定人；群聊里也屏蔽该成员）
  - `/group add|del <群ID> [群ID...]` 群白名单（支持批量）/ `/group list` 查看
  - `/blacklist add|del|list <群ID> [群ID...]` 黑名单群（黑白名单**互斥**，加一边自动移另一边）
  - `/proactive add|del|list <群ID> [群ID...]` 主动回复候选群（未 @ 也放行；`list` 会打印 UMO）
  - `/mode atbot|none|all` 群消息准入模式热切
  - `/at on|off` 群聊是否必须 @ 才回复（主动候选群优先）
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
| `wpp_account.py` | WppAccount 类 (单账号: WS + 有界队列/worker + 白名单 + 去重) |
| `wpp_messages.py` | vendor 消息解析/转换 (纯函数, 含非消息事件过滤) |
| `wpp_appmsg.py` | appmsg/XML 卡片结构化解析 (链接/小程序/位置/名片/合并转发/接龙) |
| `wpp_policy.py` | 白名单/群准入策略 (纯函数, 含 proactive_groups) |
| `wpp_media.py` | 图片/文件/语音/视频下载 + 消息链富化 (支持懒加载选择性下载) |
| `wpp_send.py` | 出站消息链发送 + 长文本分片 |
| `wpp_commands.py` | filehelper 管理命令 |
| `wpp_storage.py` | 运行时数据持久化 (白名单/去重, 原子写) |
| `wpp_client.py` | WPP vendor HTTP/WS API 封装 (retry + 超时) |
| `wpp_event.py` | 消息事件 (重写 send 真正发送到微信) |
| `tests/` | 单元测试 (pytest, 在 AstrBot 容器内运行) |
| `pages/account-info/` | Dashboard 账号信息页面 |
| `logo.png` | 插件 + 平台适配器图标 |

运行时数据（白名单、去重状态）写在 AstrBot 标准插件数据目录：
`data/plugin_data/astrbot-plugin-wpp/accounts/<account_id>/`。
旧版写在插件目录 `accounts/` 下的数据会在启动时自动迁移。

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

### 测试（容器内运行，插件依赖 AstrBot 运行时）

```bash
docker exec astrbot python3 -m pytest /AstrBot/data/plugins/astrbot-plugin-wpp/tests -v
```

---

## ⚙️ 平台配置字段

| 字段 | 必填 | 说明 |
|---|---|---|
| `wpp_base_url` | ✅ | WPP API 地址（如 `http://<vendor-ip>:28062`）|
| `wpp_auth_token` | 二选一 | **单账号模式**：WPP X-Access-Token（= authcode）|
| `wpp_accounts` | 二选一 | **多账号模式**：账号列表 JSON（见下方多账号章节）|
| `wpp_allow_users` | — | **单账号模式**白名单 wxid（逗号分隔），空则所有私聊触发 |
| `wpp_block_users` | — | **单账号模式**私聊黑名单 wxid（逗号分隔），全部放行时排除指定人；群聊里也屏蔽该成员 |
| `wpp_group_reply` | — | **单账号模式**群消息准入策略: `atbot` / `none` / `all`（`all`=放行所有群消息，观察模式，不强制回复）|
| `wpp_proactive_groups` | — | **单账号模式**主动回复候选群（逗号分隔群 ID）；未 @ 消息也放行，是否回复由决策插件决定 |
| `wpp_sync_heartflow_whitelist` | — | 群白名单自动同步到 Heartflow `chat_whitelist`（默认 `true`，未装 Heartflow 自动跳过）|
| `wpp_ws_url` | — | WS 实时通道地址（如 `wss://<vendor-domain>/ws/sync`），主通道 |
| `wpp_webhook_enabled` | — | 是否启用 webhook 兜底（默认 `false`，仅 WS）|

> ⚠️ **单账号 vs 多账号**：配置 `wpp_auth_token`（+ 顶层白名单）= 单账号；配置 `wpp_accounts`（JSON 列表）= 多账号。两者只用一个。

### 👥 多账号（单实例内, 通过 authcode 隔离）
一个 wpp 适配器实例可管理多个微信账号，**同一 vendor 地址**（`wpp_base_url` + `wpp_ws_url`），通过不同 `authcode` 隔离。每个账号独立：
- WS 实时连接（用该账号 authcode 鉴权）
- 白名单/私聊黑名单/群策略/群黑名单（`data/plugin_data/astrbot-plugin-wpp/accounts/<account_id>/wpp_whitelist.json` 独立持久化）
- filehelper 命令（各自管理自己的白名单）
- 消息去重 / 机器人身份

`wpp_accounts` 配置示例：
```json
[
  {"id": "xieyin", "authcode": "<authcode_账号1>", "allow_users": "<wxid_白名单>", "group_reply": "atbot"},
  {"id": "yirong", "authcode": "<authcode_账号2>", "allow_users": "", "block_users": "<wxid_排除>", "group_reply": "atbot", "proactive_groups": "<群id>@chatroom", "blacklist_groups": ""},
  {"id": "test",  "authcode": "xxx", "require_at_mention": true}
]
```
每个账号字段：`id`（唯一）、`authcode`（必填）、`allow_users`/`block_users`/`group_reply`/`proactive_groups`/`blacklist_groups`/`require_at_mention`（可选，默认同单账号）。

filehelper 命令输出带账号标识（如 `【WPP 命令 (账号 xieyin)】`），各账号命令互不影响。

### 🤖 主动回复（群聊未 @）

WPP 适配器只负责**准入**，不负责判断“要不要回复”。链路分三层：

| 层 | 职责 |
|---|---|
| WPP 适配器 | 硬边界（系统/支付/黑名单/群策略）+ 准入；**不设置唤醒标志** |
| 决策插件 | Heartflow / 内置 `active_reply` 决定是否主动接话 |
| AstrBot 核心 | 上下文、限流、会话、LLM |

#### 配置

1. 把群加入主动候选名单（三选一）：
   - Dashboard：`wpp_proactive_groups` / `wpp_accounts[].proactive_groups`
   - filehelper：`/proactive add 123@chatroom`
2. 启用一个决策插件（**只开一个**）：
   - **Heartflow（推荐）**：`judge_provider_name` 指向便宜的判断模型，
     `whitelist_enabled=true` + `chat_whitelist=["wpp:GroupMessage:123@chatroom"]`；
     建议 `reply_threshold=0.7`、`min_reply_interval_seconds=180` 起步。
   - **内置 active_reply**：`provider_ltm_settings.active_reply.enable=true`，
     `method=possibility_reply`，`possibility_reply` 建议 0.05–0.1；
     要求该群已有对话（先 @ 回复一次或 `/new`）。
3. 可选：开启 `group_message_history_enable` / `group_icl_enable` 提供群聊上下文
   （会增加 token 成本）。

#### 安全默认值

- **只加 `proactive_groups` 不会让机器人回复任何消息**：适配器不设
  `is_at_or_wake_command`，只有决策插件批准后核心才会调用 LLM。
- `group_reply=none`、黑名单群、`block_users` 仍然优先；`proactive_groups`
  不能绕过它们。
- 一个群必须先通过群白名单（若配置了群白名单）才会进入主动候选判断。

#### UMO 格式

Heartflow 白名单使用 `unified_msg_origin`，格式为：

```
<平台实例id>:GroupMessage:<群ID>
```

例如 `wpp:GroupMessage:123@chatroom`。用 `/proactive list` 可直接看到每个群
对应的 UMO；多账号或自定义平台实例 id 时前缀会不同。

**自动同步（默认开启）**：WPP 会把各账号的群白名单 + 主动候选群自动同步到
Heartflow 的 `chat_whitelist`：

- 只替换本平台的 `wpp:GroupMessage:` 条目，其他平台/手写条目保留；
- `/group`、`/proactive` 增删、外部修改 `wpp_whitelist.json` 热更新、适配器启动时都会同步；
- 写盘后同时更新已加载的 Heartflow 实例，**无需重启**；
- 可用 `wpp_sync_heartflow_whitelist=false` 关闭；未安装 Heartflow 时自动跳过。

#### 媒体懒加载

未 @ 放行的消息只构造 `[图片]` / `[文件]` / `[视频]` 占位符：

- 图片/文件/视频：在 `on_waiting_llm_request`（真正要调用 LLM 前）才下载；
- 语音：仍即时下载，保证 AstrBot `PreProcessStage` 的 STT 正常；
- 未触发回复的消息不会产生任何媒体下载。

注意：懒加载发生在内置群聊上下文 handler 之后，因此当前图片不会进入
`group_icl` 历史/图注，但会正常进入本次 LLM 请求。

#### 灰度建议

1. 先在一个低风险群测试，Heartflow 白名单只放这个群；
2. 观察 1–3 天日志（`🔥 心流触发`、触发频率、误触发）；
3. 调整阈值/冷却后再扩群；
4. 回滚：`/proactive del <群ID>` 或关闭 Heartflow。

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
- 配置字段用 `secret: true` 标记（密码框输入）

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
