# astrbot-plugin-wpp

**AstrBot 个人微信（Pad协议）适配器 (WeChatPadPro) — 让微信成为 AstrBot 的一等平台**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![AstrBot](https://img.shields.io/badge/AstrBot-v4.x-purple)](https://github.com/Soulter/AstrBot)
[![GitHub stars](https://img.shields.io/github/stars/jiexiaoyin/astrbot-plugin-wpp?style=social)](https://github.com/jiexiaoyin/astrbot-plugin-wpp/stargazers)
[![GitHub forks](https://img.shields.io/github/forks/jiexiaoyin/astrbot-plugin-wpp?style=social)](https://github.com/jiexiaoyin/astrbot-plugin-wpp/network)
[![Open Issues](https://img.shields.io/github/issues/jiexiaoyin/astrbot-plugin-wpp)](https://github.com/jiexiaoyin/astrbot-plugin-wpp/issues)
[![Last commit](https://img.shields.io/github/last-commit/jiexiaoyin/astrbot-plugin-wpp)](https://github.com/jiexiaoyin/astrbot-plugin-wpp/commits/master)

AstrBot 个人微信（Pad协议）适配器 (WeChatPadPro)，Dashboard 显示名
**个人微信（Pad协议）**。

**让微信成为 AstrBot 的一等平台**：微信消息 → WPP vendor → 本适配器 → AstrBot 核心 (LLM/命令/插件)，回复自动回微信。

> 🚀 **核心亮点**：**WS 实时主通道**（不断连、自动重连）+ **一个实例一个微信账号**（一个 X-Access-Token/authcode 对应一个微信，多账号用多个平台实例）+ **全类型媒体**（图片/语音/文件/视频全部 AI 可读）+ **主动回复候选群**（未 @ 也放行，由 Heartflow 等插件决定是否接话）+ **进群欢迎语**（成员表 diff 识别真实新人，模板可 @、防刷屏、热更新）+ **filehelper 命令**（文件助手发指令管理白名单与欢迎语）。

## 📖 目录

- [快速开始](#-快速开始)
- [相关项目](#-相关项目)
- [插件亮点](#-插件亮点)
- [命令参考](#-命令参考)
- [架构](#-架构)
- [文件结构](#-文件结构)
- [部署](#-部署)
- [平台配置字段](#平台配置字段)
- [多账号](#-多账号多个平台实例)
- [主动回复（群聊未 @）](#-主动回复群聊未-)
- [常见问题](#-常见问题)
- [开发与测试](#-开发与测试)
- [版本与兼容性](#版本与兼容性)
- [凭证安全](#-凭证安全)
- [文档](#-文档)

## ⚡ 快速开始

**环境要求**：AstrBot `>=4.16.0,<5`（Docker 部署）、Python 3.10+、可用的
WeChatPadPro vendor（HTTP API + WS `/ws/sync`）。

1. **安装插件**：把本仓库放入 AstrBot 插件目录后重启：
   ```bash
   docker cp astrbot_plugin_wpp astrbot:/AstrBot/data/plugins/
   docker restart astrbot
   ```
2. **添加平台**：Dashboard → 平台适配器 → 添加「个人微信（Pad协议）」，至少填写：
   - `wpp_base_url`：vendor HTTP 地址，如 `http://<vendor-ip>:28062`
   - `wpp_auth_token`：vendor authcode（一个实例对应一个微信账号）
   - `wpp_ws_url`：vendor WS 地址，如 `wss://<vendor-domain>/ws/sync`
   - 保存并启用。
3. **验证在线**：AstrBot 日志出现 `账号已在线` + `WS connected`；私聊机器人
   或在群里 `@机器人` 发一条消息即可收到回复。
4. **按需收紧策略**：在文件传输助手发 `/help` 查看当前策略与全部命令；
   把群 ID 加入群白名单后，该群自动成为主动回复候选（见
   [主动回复](#-主动回复群聊未-)）。

最小可用配置只有 `base_url + authcode + ws_url`。白名单为空时私聊全部放行、
群聊按 `wpp_group_reply`（默认 `atbot`）处理，建议上线前先配置白名单。

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

### 👥 一个实例一个微信账号
- **一个平台实例 = 一个 `wpp_auth_token`（X-Access-Token = authcode）= 一个微信账号**
- 多账号请添加多个「个人微信（Pad协议）」平台实例，`id` 唯一、各自填自己的 authcode
- **每个实例独立**：WS 连接 / 白名单 / 群策略 / 黑名单 / 去重 / 机器人身份 / filehelper 命令
- 同一 authcode 不允许被两个实例同时使用，后启动的实例会被拒绝

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
- **长回复**：引用气泡放第一段（含 `@昵称`），剩余内容自动续发普通文本，
  不再因为超过 500 字而丢失引用/@；引用发送失败会回退为普通文本；
- 普通文本回复使用 vendor `At` 字段。

### 🛡 完善的接入能力
- **系统消息静默（内置，任何模式生效）**：公众号（`gh_`）、微信团队、微信支付、服务通知、财付通、微信安全中心、订阅号消息等系统会话，以及状态/系统/撤回消息、红包/转账卡片，都不会触发 AI
- **私聊黑名单**：`wpp_block_users` / `/block`，全部放行模式下排除指定 wxid，群聊里也屏蔽该成员
- **白名单**：私聊仅白名单 wxid 触发 AI；群白名单里的 `@chatroom` 同时作为主动回复候选
- **群消息准入策略**：`atbot`（只放行 @ 机器人）/ `none` / `all`（放行所有群消息，观察模式，不强制回复）
- **主动回复候选群**：**与群白名单共用**，群进白名单即放行未 @ 消息；是否回复由 Heartflow / 内置 `active_reply` 决定
- **filehelper 命令**：在文件传输助手里发 `/help` 查看当前策略、分组命令和单条
  命令详情；`/user` `/block` `/group` `/blacklist` `/proactive` 均支持
  `add/del/list`（批量），运行时立即生效并持久化；手动编辑
  `wpp_whitelist.json` 也会在约 3 秒内热加载。完整列表见
  [命令参考](#-命令参考)。
- **进群欢迎语（v0.9.0）**：群成员变动系统通知触发一次成员表 diff，
  真正新增的成员发模板欢迎语（可 @ 新人）。纯模板、无 LLM 润色；
  默认关闭，配置在 `wpp_whitelist.json` 的 `join_welcome` 段，3 秒热更新；
  `/welcome on|off|list` 管理。首次同步只建基线；`maxBatch` / 30 秒节流 /
  静默时段防刷屏；先落盘基线再发送，至多一次。
- **多消息遍历**：vendor 一次推多条不遗漏
- **自动心跳**：账号离线自动拉上线
- **动态配置**：换 vendor 地址/authcode 自动适配（无硬编码）

### 🖥 运维体验
- **WeChatPad Control Center**（中文环境显示「微信(Pad协议)控制中心」；
  Dashboard → 插件页面 → 对应名称）：
  - 顶部概览：账号数、在线数、群白名单、生效主动候选、发送成功/失败；
  - 账号卡片：在线徽标、昵称/wxid、队列、发送统计、最近错误；
  - 编辑策略：私聊/群白名单、私聊/群黑名单、显式主动候选、群消息模式、
    是否必须 @；输入时实时预览生效主动候选；
  - 保存前校验群 ID（必须是 `@chatroom`），保存后立即落盘并同步 Heartflow；
  - 「刷新状态」立即向 vendor 查询在线状态；有未保存修改时只刷新状态、
    保留表单；
  - 响应式布局 + 深色主题，适配 Dashboard 主题切换。
  - 入口：Dashboard → **插件** → 已安装插件 → `astrbot_plugin_wpp` →
    **打开插件UI界面**（打开的是第一页 `console`，即控制台）；
    也可进入插件详情 → 行为 → 页面条目 → 「打开」。
  - 控制台是**运行时策略的唯一日常入口**；vendor 地址/authcode、平台实例、
    Heartflow 模式等仍在 Dashboard → 平台适配器 → 个人微信（Pad协议）配置中修改。
  - 适配器配置里的白名单/群策略字段是账号启动时的初始值；账号保存过策略后，
    以控制台持久化的运行时配置为准。
- **账号诊断 tab**（控制台内）：查看已配置账号在线状态，或用 X-Access-Token 查询
  昵称/wxid/地区/签名；Token 仅用于本次请求，不落盘。
- **在线状态卡片**：平台状态实时显示（多实例聚合）
- **动态面板地址**：UI 显示实际配置的 vendor 地址（不硬编码）
- **发送统计**：`WppAccount.stats["send"]` 暴露成功/失败/连续失败/最后错误；
  连续失败 3 次写 WARNING 日志

### 🛡 出站稳定性
- **会话级串行**：同一会话的整条消息链持有锁，两条回复的分片不会交错；
- **最小发送间隔**：`wpp_send_interval_ms`（默认 300ms）控制同一会话的
  vendor 发送节奏，降低微信风控概率；
- **复读保护**：同一会话相同文本在窗口内第 3 次起跳过，防止 LLM 复读刷屏；
- **入站资料预取**：群名/群主/成员昵称缓存 30 分钟，AI 提示词拿真实昵称，
  引用回复 @ 也复用同一份缓存。

## 🎛 命令参考

### filehelper 命令

在微信「文件传输助手」里发送，只影响**当前账号**。`/help` 会显示版本、
当前策略摘要和分组命令；`/help <命令>` 查看单条详情。输出为微信纯文本
（无 Markdown/表格/列对齐），按行阅读即可。

| 命令 | 作用 | 说明 |
|---|---|---|
| `/help [命令]` | 帮助 | 无参数显示策略摘要 + 全部命令；如 `/help group` |
| `/user add\|del <wxid>...` | 私聊白名单 | 支持批量；`/user list` 查看 |
| `/block add\|del <wxid>...` | 私聊黑名单 | 全部放行时排除指定人；群聊里也屏蔽该成员；`list` 查看 |
| `/group add\|del <群ID>...` | 群白名单 | 支持批量；**群白名单自动成为主动候选**；`list` 查看 |
| `/blacklist add\|del <群ID>...` | 群黑名单 | 与群白名单**互斥**，加一边自动移另一边；`list` 查看 |
| `/proactive add\|del <群ID>...` | 显式主动候选 | 群白名单已自动纳入；仅未配置群白名单时作补充；`list` 查看 |
| `/mode atbot\|none\|all` | 群消息准入 | `atbot`=只放行 @；`none`=忽略群；`all`=放行全部群消息（观察，不强制回复） |
| `/at on\|off` | 必须 @ | 群聊是否必须 @ 机器人才放行；主动候选群优先 |
| `/welcome on\|off\|list` | 进群欢迎 | 开关/查看生效群与基线人数；配置见 `join_welcome` 段 |

**进群欢迎配置**（`wpp_whitelist.json`，3 秒热更新）：

```json
"join_welcome": {
  "enabled": true,
  "mode": "whitelist",
  "groups": [],
  "template": "欢迎 @{name} 加入{group}！",
  "mention": true,
  "quietHours": [],
  "maxBatch": 3
}
```

`mode`：`whitelist`（动态跟随群白名单，推荐）/ `allowlist` / `blocklist` /
`all`；群黑名单永远排除。`{name}` 为新人昵称（多人用「、」连接），
`{group}` 为群名（查不到写「本群」）。首次同步某群只建基线；
一次 diff 超过 `maxBatch` 人视为基线重建，不发欢迎；同群 30 秒内只抓一次。

**约定**：

- 群 ID 以 `@chatroom` 结尾；私聊 ID 是 `wxid_...`。
- 所有修改立即持久化到 `data/plugin_data/astrbot_plugin_wpp/accounts/<账号>/wpp_whitelist.json`。
- 黑白名单互斥；`block_users` 优先级最高，主动候选不能绕过。
- `/group del` 会把群从群白名单和自动主动候选中一起移除；如仍需主动观察，
  改用 `/proactive add`（仅在未配置群白名单时有效）。

### AstrBot 管理员命令

以下命令需要 AstrBot 管理员权限（`PermissionType.ADMIN`），可在任意会话使用：

| 命令 | 作用 |
|---|---|
| `/wpp_status [实例id\|账号id]` | 查询账号在线状态、wxid、长连接状态 |
| `/wpp_policy` | 查看全部实例/账号的策略摘要（含生效主动候选） |
| `/wpp_policy <账号> <字段> add\|del\|set <值...>` | 修改列表字段：`allow_users` / `block_users` / `blacklist_groups` / `proactive_groups` |
| `/wpp_policy <账号> group_reply set atbot\|none\|all` | 修改群消息准入模式 |
| `/wpp_policy <账号> require_at_mention set true\|false` | 修改是否必须 @ |
| `/wpp_sync [实例id]` | 立即把 WPP 群白名单同步到 Heartflow |

> 管理员命令修改后同样会触发 Heartflow 白名单同步。Heartflow 白名单来源
> （`auto` / `custom`）在 Dashboard 平台配置中设置，见
> [主动回复](#-主动回复群聊未-)。

---

## 🏗 架构

```
[微信] ←→ [WPP vendor (WeChatPadPro)]
                │ ① WS 实时通道 (wss://vendor/ws/sync?authcode=账号)
                │    每账号一个独立 WS 连接 (通过 authcode 隔离)
                ▼
[astrbot 容器] astrbot_plugin_wpp (Platform 适配器)
                │ WppAccount (每账号: WS 连接 + 白名单 + 去重 + 身份)
                │ ② 解析 → WppMessageEvent → 事件总线
                ▼
        AstrBot 核心 (LLM/命令/插件)
                │ ③ event.send → 对应账号 WPP 发送 API (SendTxt/UploadImg...)
                ▼
        微信收到回复
```

- **主通道 = WS 实时连接**：连接建立即拉全量离线消息 + 实时推送，自动重连保活（webhook 已退役）
- **一个实例 = 一个 WppAccount**：一个 authcode + 一个 WS + 一套白名单；多账号 = 多个平台实例（AstrBot UMO 前缀按实例 id 区分）

---

## 📦 文件结构

| 文件 | 作用 |
|---|---|
| `main.py` | 插件入口 (Star) + Web API (账号信息/策略/Heartflow) + 媒体懒加载钩子 |
| `wpp_adapter.py` | 平台适配器 (账号管理器 + 账号解析 + 生命周期 + 发送路由) |
| `wpp_account.py` | WppAccount 类 (单账号: WS + 有界队列/worker + 白名单 + 去重) |
| `wpp_messages.py` | vendor 消息解析/转换 (纯函数, 含非消息事件过滤) |
| `wpp_join_events.py` | 进群/退群系统通知识别 (纯函数, 递归找原文) |
| `wpp_join_welcome.py` | 进群欢迎配置/diff/文案渲染 (纯函数) |
| `wpp_appmsg.py` | appmsg/XML 卡片结构化解析 (链接/小程序/位置/名片/合并转发/接龙) |
| `wpp_policy.py` | 白名单/群准入策略 (纯函数, 群白名单自动主动候选) |
| `wpp_heartflow.py` | 群白名单 → Heartflow `chat_whitelist` 同步 (auto/custom + 子集不变量) |
| `wpp_media.py` | 图片/文件/语音/视频下载 + 消息链富化 (支持懒加载选择性下载) |
| `wpp_send.py` | 出站消息链发送 + 长文本分片 |
| `wpp_commands.py` | filehelper 管理命令 + 动态 `/help` |
| `wpp_storage.py` | 运行时数据持久化 (白名单/去重/成员基线, 原子写) |
| `wpp_client.py` | WPP vendor HTTP/WS API 封装 (retry + 超时) |
| `wpp_event.py` | 消息事件 (重写 send 真正发送到微信) |
| `tests/` | 单元测试 (pytest, 在 AstrBot 容器内运行) |
| `pages/console/` | WeChatPad Control Center (策略编辑/生效预览/Heartflow 同步 + 账号诊断 tab) |
| `.astrbot-plugin/i18n/` | Dashboard 页面标题与显示名 (zh-CN / en-US) |
| `logo.png` | 插件扩展列表图标（灰色微信线条） |
| `logo-platform.png` | 平台适配器图标（机器人添加/列表，黄色微信） |

运行时数据（白名单、去重状态、群成员基线 `members.json`）写在 AstrBot 标准
插件数据目录：`data/plugin_data/astrbot_plugin_wpp/accounts/<account_id>/`。
插件不再读写自身源码目录下的 `accounts/`、`data/`；旧版残留可备份后删除
（见 [DEPLOY.md](DEPLOY.md)）。

---

## 🚀 部署

```bash
# 1. 拷入 AstrBot 容器插件目录
docker cp astrbot_plugin_wpp astrbot:/AstrBot/data/plugins/

# 2. 容器内确认依赖 (aiohttp + websockets — WS 主通道必需)
docker exec astrbot sh -c 'python3 -c "import aiohttp, websockets" || pip install aiohttp websockets'

# 3. 重启 AstrBot 加载适配器
docker restart astrbot

# 4. Dashboard (http://<host>:6185) → 平台适配器 → 添加 "个人微信（Pad协议）"
#    填配置 (见下表) → 保存启用

# 5. 确认版本与启动状态
docker logs --since 60s astrbot 2>&1 | grep -E "astrbot_plugin_wpp|账号已在线|WS.*connected"
```

也可以直接执行仓库自带的 `./deploy.sh`（拷贝 → 依赖检查 → 重启 → 日志检查）。

### 测试（容器内运行，插件依赖 AstrBot 运行时）

```bash
docker exec -w /AstrBot/data/plugins astrbot \
  python -m pytest -q astrbot-plugin-wpp/tests
```

---

## 平台配置字段

| 字段 | 必填 | 说明 |
|---|---|---|
| `wpp_base_url` | ✅ | WPP API 地址（如 `http://<vendor-ip>:28062`）|
| `wpp_auth_token` | ✅ | WPP X-Access-Token（= authcode）；一个实例只能填一个，对应一个微信账号 |
| `wpp_allow_users` | — | 白名单 wxid / 群 ID（逗号分隔）；`@chatroom` 群ID 同时作为群白名单和主动回复候选 |
| `wpp_block_users` | — | 私聊黑名单 wxid（逗号分隔），全部放行时排除指定人；群聊里也屏蔽该成员 |
| `wpp_group_reply` | — | 群消息准入策略: `atbot` / `none` / `all`（`all`=放行所有群消息，观察模式，不强制回复）|
| `wpp_proactive_groups` | — | 额外主动候选群（逗号分隔群 ID）；群白名单已自动共用，通常无需填写 |
| `wpp_sync_heartflow_whitelist` | — | 群白名单自动同步到 Heartflow `chat_whitelist`（默认 `true`，未装 Heartflow 自动跳过；每 5 分钟自愈）|
| `wpp_heartflow_whitelist_mode` | — | `auto`=参考 WPP 群白名单（默认）；`custom`=单独使用 `wpp_heartflow_groups` |
| `wpp_heartflow_groups` | — | `custom` 模式的单独 Heartflow 群白名单（逗号分隔群 ID），必须是 WPP 群白名单的子集 |
| `wpp_lazy_media` | — | 未 @ 群消息媒体懒加载（默认 `true`）；启用内置 `group_icl_enable` / `group_message_history_enable` 时建议设为 `false` |
| `wpp_send_interval_ms` | — | 同一会话两次发送的最小间隔，默认 `300` 毫秒；`0`=不节流 |
| `wpp_outbound_repeat_guard` | — | 出站复读保护（默认 `true`）：窗口内相同文本超过上限后跳过 |
| `wpp_outbound_repeat_window` | — | 复读保护窗口，默认 `300` 秒 |
| `wpp_outbound_repeat_limit` | — | 复读保护上限，默认 `2`（第 3 条相同文本起跳过）|
| `wpp_ws_url` | — | WS 实时通道地址（如 `wss://<vendor-domain>/ws/sync`），主通道 |
| `wpp_webhook_enabled` | — | 是否启用 webhook 兜底（默认 `false`，仅 WS）|

> ⚠️ **一个平台实例 = 一个 `wpp_auth_token` = 一个微信账号。** 不再支持
> `wpp_accounts` 单实例多账号配置；旧配置会告警并被忽略（只使用
> `wpp_auth_token`）。多账号请拆成多个平台实例。
>
> ⚠️ 上表中的白名单/群策略字段是账号启动时的初始值。日常增删白名单、切换群模式请使用 **WeChatPad Control Center**；账号保存过运行时策略后，以控制台写入的 `wpp_whitelist.json` 为准，避免两处配置产生歧义。

### 👥 多账号（多个平台实例）
**一个微信账号 = 一个 authcode = 一个「个人微信（Pad协议）」平台实例。** 多账号时在
Dashboard 再添加一个「个人微信（Pad协议）」平台实例：

```text
平台实例 个人微信（Pad协议）          (authcode A) ── 微信账号 A
平台实例 个人微信（Pad协议）-shop2    (authcode B) ── 微信账号 B
```

每个实例：
- `id` 全平台唯一（决定 AstrBot UMO 前缀和运行时数据目录）；
- `wpp_auth_token` 填该账号自己的 authcode；
- 独立 WS 连接、白名单/私聊黑名单/群策略/群黑名单
  （默认实例写入 `data/plugin_data/astrbot_plugin_wpp/accounts/wpp/wpp_whitelist.json`，
  `-shop2` 等后缀实例写入对应的 `accounts/wpp-shop2/`）；
- 独立 filehelper 命令、消息去重和机器人身份。

> 运行时目录会把平台名前缀 `个人微信（Pad协议）` 映射为稳定的 ASCII `wpp`，
> 避免中文实例 id 被净化后多个账号落到同一目录；这只是存储前缀，不影响
> Dashboard 显示和 UMO 前缀。

**同一个 authcode 只允许一个实例轮询**：重复配置时后启动的实例会报
`同一 authcode 已被平台实例 ... 使用` 并拒绝启动，避免两个 WS 互抢消息。
authcode 不能复用于多个账号，也不存在「一个 authcode 下挂多个微信」的配置。

filehelper 命令输出带账号标识与版本（如 `【WPP 命令 · 账号 个人微信（Pad协议） · v<版本>】`），
各实例命令互不影响。

### 🤖 主动回复（群聊未 @）

WPP 适配器只负责**准入**，不负责判断“要不要回复”。链路分三层：

| 层 | 职责 |
|---|---|
| WPP 适配器 | 硬边界（系统/支付/黑名单/群策略）+ 准入；**不设置唤醒标志** |
| 决策插件 | Heartflow / 内置 `active_reply` 决定是否主动接话 |
| AstrBot 核心 | 上下文、限流、会话、LLM |

#### 配置

1. 把群加入**群白名单**即可（主动回复与白名单共用同一份列表）：
   - Dashboard（对应平台实例）：`wpp_allow_users` 加群 ID
   - filehelper：`/group add 123@chatroom`
   - 未配置群白名单、只想单独 opt-in 时，才用 `proactive_groups` / `/proactive add`
2. 启用一个决策插件（**只开一个**）：
   - **Heartflow（推荐）**：`judge_provider_name` 指向便宜的判断模型，
     `whitelist_enabled=true` + `chat_whitelist=["个人微信（Pad协议）:GroupMessage:123@chatroom"]`；
     建议 `reply_threshold=0.7`、`min_reply_interval_seconds=180` 起步。
   - **内置 active_reply**：`provider_ltm_settings.active_reply.enable=true`，
     `method=possibility_reply`，`possibility_reply` 建议 0.05–0.1；
     要求该群已有对话（先 @ 回复一次或 `/new`）。
3. 可选：开启 `group_message_history_enable` / `group_icl_enable` 提供群聊上下文
   （会增加 token 成本）。

#### 安全默认值

- **进群白名单或 `proactive_groups` 都不会让机器人自动回复任何消息**：适配器
  只放行、不设 `is_at_or_wake_command`，只有决策插件批准后核心才会调用 LLM。
- `group_reply=none`、黑名单群、`block_users` 仍然优先；白名单/主动候选
  不能绕过它们。
- 一个群必须先通过群白名单（若配置了群白名单）才会进入主动候选判断；
  群白名单里的 `@chatroom` 自动成为主动候选，无需重复配置。

#### 群消息准入顺序

```
系统/支付/撤回等卡片       → 静默（任何模式）
block_users               → 拒绝（群成员黑名单）
blacklist_groups          → 拒绝
群白名单（存在 @chatroom 时）→ 不在名单则拒绝
group_reply=none          → 拒绝
已 @ 机器人               → 放行（at_me）
主动候选（白名单群自动纳入）→ 放行（proactive，不唤醒）
require_at_mention=on     → 拒绝
group_reply=all           → 放行（观察模式）
其他                       → 拒绝
```

注意：`allow_users` 里只有在**至少有一个 `@chatroom`** 时才构成群白名单；
如果只有私聊 wxid，群聊不会因为群白名单被拦截，而是继续按
`group_reply` / `require_at_mention` 判断。

#### UMO 格式

Heartflow 白名单使用 `unified_msg_origin`，格式为：

```
<平台实例id>:GroupMessage:<群ID>
```

例如 `个人微信（Pad协议）:GroupMessage:123@chatroom`。前缀就是平台实例 `id`；
用 `/proactive list` 可直接看到每个群对应的 UMO，多实例或自定义实例 id 时前缀会不同。

**自动同步（默认开启）**：WPP 会把各账号的群白名单（`auto` 模式下还包括
显式 `proactive_groups`）同步到 Heartflow 的 `chat_whitelist`。同步结果始终
满足**群白名单必须包含心流白名单**：

- 群白名单非空时，同步目标会与群白名单取交集；白名单外的显式候选不会同步；
- 群白名单为空（不限制群）时，`auto` 模式才使用显式 `proactive_groups`；
- 只替换本平台（`<平台实例id>:GroupMessage:`，默认实例即
  `个人微信（Pad协议）:GroupMessage:`）的条目，其他平台/手写条目保留；
- `/group`、`/proactive` 增删、外部修改 `wpp_whitelist.json` 热更新、适配器启动时都会同步；
- 另外每 5 分钟幂等自愈一次，Heartflow 重载/配置被外部改写后能自动恢复；
- 写盘后同时更新已加载的 Heartflow 实例，**无需重启**；
- 可用 `wpp_sync_heartflow_whitelist=false` 关闭；未安装 Heartflow 时自动跳过。

**两种白名单来源**（`wpp_heartflow_whitelist_mode`）：

| 模式 | 行为 |
|---|---|
| `auto`（默认） | 参考 WPP 群白名单：Heartflow 白名单 = 群白名单 + 显式 `proactive_groups`，再与群白名单取交集 |
| `custom` | 单独设置：Heartflow 白名单 = `wpp_heartflow_groups` 中列出的群 |

> ⚠️ **不变量：群白名单必须包括心流白名单**。`custom` 中的群如果不在
> WPP 群白名单（`allow_users` 的 `@chatroom`）中，会被拒绝并跳过同步，
> 同时写 WARNING 日志。因为适配器会先按群白名单拦截，Heartflow 永远
> 看不到这些群的消息。请先用 `/group add <群ID>` 把群加入群白名单。

`custom` 模式适合“群白名单很大，但只让心流观察其中几个群”的场景；
`auto` 模式适合“所有白名单群都交给心流判断”。

#### 媒体懒加载

未 @ 放行的消息只构造 `[图片]` / `[文件]` / `[视频]` 占位符：

- 图片/文件/视频：在 `on_waiting_llm_request`（真正要调用 LLM 前）才下载；
- 语音：仍即时下载，保证 AstrBot `PreProcessStage` 的 STT 正常；
- 未触发回复的消息不会产生任何媒体下载。

注意：懒加载发生在内置群聊上下文 handler 之后，因此当前图片不会进入
`group_icl` 历史/图注，但会正常进入本次 LLM 请求。如果启用了
`group_icl_enable` / `group_message_history_enable` 且需要图片进入历史，
请设置 `wpp_lazy_media=false` 改为事件创建时即时下载。

#### 灰度建议

1. 先把一个低风险群加入群白名单；若白名单里还有其他群，用
   `wpp_heartflow_whitelist_mode=custom` + `wpp_heartflow_groups` 只让心流观察
   这一个群（群白名单仍需包含它）；
2. 观察 1–3 天日志（`🔥 心流触发`、触发频率、误触发）；
3. 调整阈值/冷却后再扩群；
4. 回滚：`/group del <群ID>`（同时退出主动候选和 Heartflow 同步），或关闭
   Heartflow；`custom` 模式也可从 `wpp_heartflow_groups` 移除该群或切回 `auto`。

### 换新环境的自动适配
配置 `wpp_base_url` + `wpp_auth_token` 后，插件**自动**：
1. 用新地址调所有 WPP API
2. 用新 authcode 认证
3. WS 连接建立即拉全量离线 + 实时消息（主通道）
4. 面板/UI 显示新地址

> ⚠️ 前提：新 authcode 需在新 vendor 的 Redis 中为有效状态（`origin=local` + `enabled`），且新 vendor 能真实收发微信。详见 [DEPLOY.md](DEPLOY.md) 的接入清单。

---

## ❓ 常见问题

### 群里 @ 了机器人但不回复
按准入顺序排查：发送者是否在 `block_users`、群是否在 `blacklist_groups`、
群白名单是否不包含该群、`group_reply` 是否为 `none`、`require_at_mention`
是否开启但消息实际没有 @ 到机器人。日志里的拒绝原因会显示
`block_user` / `blacklist_group` / `group_whitelist` / `group_none` /
`require_at` / `no_at`。

### 群白名单已配置，为什么未 @ 消息没有主动接话
适配器只负责**准入**，不负责唤醒。确认：
1. 该群确实在群白名单（`/group list`，白名单群自动成为主动候选）；
2. 已启用且只启用一个决策插件（Heartflow 或内置 `active_reply`）；
3. Heartflow 的 `enable_heartflow`、`whitelist_enabled`、`chat_whitelist`
   包含该群的 UMO（`/proactive list` 可查看）；
4. 判断阈值/冷却未拦截（如 `reply_threshold=0.7`、
   `min_reply_interval_seconds=180`），且群里确实有可判断的上下文。

### 心流白名单里少了某个群
群白名单必须包含心流白名单。`custom` 模式下，不在 `allow_users`
群白名单中的群会被拒绝同步并写 WARNING；先用 `/group add <群ID>` 把群加入
群白名单。`auto` 模式下，显式 `proactive_groups` 也会与群白名单取交集。

### 为什么公众号/微信团队/支付消息不会触发回复
这些系统会话和支付/系统卡片在适配器层静默，任何模式都不会进入 AI。
若旧版本出现误触发，升级到 v0.6.0+ 即可；仍异常时检查日志中的消息类型。

### 引用回复没有 @ 被回复的人
在 AstrBot 平台设置中开启 `reply_with_mention` 和 `reply_with_quote`。
群聊引用气泡没有原生 At 字段，适配器会把 `@昵称` 合并到正文；昵称会依次
从入站、引用、群成员、通讯录解析，失败才退回 `@wxid`。引用发送失败会自动
回退为普通文本。

### 多账号会互相影响吗
不会。多账号 = 多个「个人微信（Pad协议）」平台实例，每个实例独立 authcode、WS、白名单、
黑名单、群策略、去重和 filehelper 命令；filehelper 输出会带实例/账号标识
（如 `【WPP 命令 · 账号 个人微信（Pad协议）-shop2】`）。管理员命令可用实例 id 指定目标。
注意同一个 authcode 不能同时配到两个实例。

### 升级后如何确认生效
查看启动日志中的 `Plugin astrbot-plugin-wpp (x.y.z)`，或在文件传输助手发
`/help` 查看版本与策略摘要。

---

## 🧪 开发与测试

```bash
# 单元测试（插件依赖 AstrBot 运行时，建议在容器内执行）
docker exec -w /AstrBot/data/plugins astrbot \
  python -m pytest -q astrbot-plugin-wpp/tests

# 真实主路径冒烟（适配器解析 → 准入 → 事件 → 发送）
docker exec -w /AstrBot/data/plugins astrbot \
  python -m pytest -q astrbot-plugin-wpp/tests/test_mainpath_smoke.py

# 静态检查
ruff check .
python -m compileall -q .
```

**发布检查清单**：

1. 更新 `metadata.yaml` 版本与 `CHANGELOG.md`；
2. 跑完整测试 + 主路径冒烟 + `ruff` / `compileall`；
3. 扫描敏感信息（真实 authcode、wxid、群 ID、vendor 域名/宿主 IP）；
4. 同步到部署目录并重启，确认插件版本、账号在线、WS connected；
5. 提交并推送 `master`。

> 测试只使用临时目录/桩数据，不要连接生产 vendor；发布前确认
> `accounts/`、`data/`、`__pycache__`、`.pytest_cache` 未被提交。

---

## 版本与兼容性

| 项目 | 要求 / 说明 |
|---|---|
| AstrBot | `>=4.16.0,<5`（见 `metadata.yaml`）|
| Python | 3.10+ |
| 依赖 | `aiohttp` + `websockets`（WS 主通道必需）|
| WPP vendor | 提供 HTTP API 与 `/ws/sync` WebSocket；认证使用 authcode |
| Heartflow | 可选；未安装时白名单同步自动跳过 |
| 平台 ID | 默认实例 id 为 `个人微信（Pad协议）`，Heartflow UMO 前缀随实例 id 变化；运行时目录固定使用 ASCII `wpp` 前缀 |

升级插件只需替换插件目录并重启 AstrBot；白名单和去重数据保存在
`data/plugin_data/astrbot_plugin_wpp/`，不会因替换插件文件丢失。

> ⚠️ **v0.13.0 升级**：平台技术名恢复为 **`个人微信（Pad协议）`**。
> AstrBot 4.29 Dashboard 的机器人列表/新增下拉直接显示平台技术名和配置
> `id`/`type`，不读取 `adapter_display_name`；因此要让 Dashboard 显示中文，
> 平台配置的 `type`/`id` 必须使用中文。若曾按 v0.12.0 改成 `wpp`，需把配置
> `type`/`id`、DB 中的 UMO/平台字段和 Heartflow 白名单一起迁回中文；运行时
> 数据目录与 webhook token 仍保持 `wpp` 前缀，**不需要迁移**。步骤见
> [DEPLOY.md](DEPLOY.md)。
>
> ⚠️ **v0.10.0 升级**：`wpp_accounts` 单实例多账号配置已移除。若旧配置里用了
> 它，请为每个账号各建一个「个人微信（Pad协议）」平台实例（`id` 唯一、各自填
> `wpp_auth_token`）。运行时数据目录按实例 id 隔离；需要沿用旧
> `accounts/<旧账号 id>/` 下的白名单/去重数据时，把该目录改名为对应实例的
> 运行时目录（`accounts/wpp/`、`accounts/wpp-shop2/` 等；单账号且仍使用默认
> 实例时无需处理）。

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
