# astrbot-plugin-wpp

AstrBot 微信 WPP (WeChatPadPro) 平台适配器。

**让微信成为 AstrBot 的一等平台**：微信消息 → WPP vendor → 本适配器 → AstrBot 核心 (LLM/命令/插件)，回复自动回微信。

---

## ✨ 插件亮点

### 全类型消息收发
| 类型 | 处理 | 说明 |
|---|---|---|
| 文本 | 直接收发 | 最基础的对话 |
| 图片 | CDN 完整下载 | `/Tools/CdnDownloadImage` 完整大图注入 AI |
| 语音 | vendor 转写 | 自带 `voice.transcript`（微信官方），**无需 STT** |
| 文件 | 下载 + AI 读取 | `DownloadFileBinary` → `File` 组件，AI 可读 Excel/PDF |
| 视频 | 分片下载 | `DownloadVideo` 分片 → `Video` 组件 |

### 完善的接入能力
- **白名单**：仅白名单 wxid 触发 AI（防陌生人骚扰）
- **群消息策略**：`atbot`（只回@机器人）/ `none` / `all`
- **filehelper 白名单命令**：在文件传输助手里发指令管理白名单（仿 wpp-openclaw）
  - `/adduser <wxid>` 授权私聊白名单 / `/deluser <wxid>` 移除
  - `/addgroup <群ID>` 授权群聊白名单 / `/delgroup <群ID>` 移除
  - `/blacklist add|del|list <群ID>` 黑名单群管理（黑白名单**互斥**，加一边自动移另一边）
  - `/group atbot|none|all` 群回复模式热切
  - `/at on|off` 群聊是否必须 @ 才回复
  - `/help` 显示全部命令 — 运行时立即生效，**持久化**（重启不丢），无需改配置重启
  - **配置文件热更新**：手动编辑 `wpp_whitelist.json` 也自动生效（约 3 秒检测），无需重启
- **多消息遍历**：vendor 一次推多条不遗漏
- **自动心跳**：账号离线自动拉上线
- **动态配置**：换 vendor 地址/authcode 自动适配（无硬编码）

### 运维体验
- **账号信息页面**：Dashboard 里查昵称/wxid/在线状态
- **在线状态卡片**：平台状态实时显示
- **动态面板地址**：UI 显示实际配置的 vendor 地址（不硬编码）
- **webhook 公网地址**：可配置回调 URL，vendor 消息可达

---

## 🏗 架构

```
[微信] ←→ [WPP vendor (WeChatPadPro)]
                │ ① push → POST http://<astrbot公网>:6199/wpp/webhook
                ▼
[astrbot 容器] astrbot-plugin-wpp (Platform 适配器)
                │ ② 解析 → WppMessageEvent → 事件总线
                ▼
        AstrBot 核心 (LLM/命令/插件)
                │ ③ event.send → WPP 发送 API (SendTxt/UploadImg...)
                ▼
        微信收到回复
```

---

## 📦 文件结构

| 文件 | 作用 |
|---|---|
| `main.py` | 插件入口 (Star) + Web API (账号信息查询) |
| `wpp_adapter.py` | 平台适配器 (webhook + 消息转换 + 白名单 + 发送 + 心跳) |
| `wpp_client.py` | WPP vendor HTTP API 封装 (retry + 超时) |
| `wpp_event.py` | 消息事件 (重写 send 真正发送到微信) |
| `pages/account-info/` | Dashboard 账号信息页面 |
| `config_schema.json` | 平台配置模板 |
| `logo.png` | 插件 + 平台适配器图标 |

---

## 🚀 部署

```bash
# 1. 拷入 AstrBot 容器插件目录
docker cp astrbot-plugin-wpp astrbot:/AstrBot/data/plugins/

# 2. 容器内确认依赖 (aiohttp)
docker exec astrbot sh -c 'python3 -c "import aiohttp" || pip install aiohttp'

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
| `wpp_auth_token` | ✅ | WPP X-Access-Token（= authcode）|
| `wpp_allow_users` | — | 白名单 wxid（逗号分隔），空则所有私聊触发 |
| `wpp_group_reply` | — | 群消息策略: `atbot` / `none` / `all` |
| `webhook_host` | — | webhook 监听地址（默认 `0.0.0.0`）|
| `webhook_port` | — | webhook 监听端口（默认 `6199`）|
| `webhook_path` | — | webhook 路径（默认 `/wpp/webhook`）|
| `webhook_public_url` | — | **webhook 公网回调地址**（如 `http://<astrbot公网>:6199`），注册给 vendor 用。留空则用 `webhook_host:port` |

### 换新环境的自动适配
配置 `wpp_base_url` + `wpp_auth_token` 后，插件**自动**：
1. 用新地址调所有 WPP API
2. 用新 authcode 认证
3. 注册 webhook 到新地址（`webhook_public_url` 或 fallback）
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
