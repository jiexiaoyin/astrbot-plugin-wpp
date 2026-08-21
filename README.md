# astrbot-plugin-wpp

AstrBot 微信 WPP (WeChatPadPro) 平台适配器。

让微信成为 AstrBot 的一等平台：微信消息 → WPP vendor → 本适配器 → AstrBot 核心 (LLM/命令/插件)，回复自动回微信。

## 功能

- **文本收发**：微信文本消息 → AstrBot → AI 回复回微信
- **图片接收**：支持 CDN 完整大图下载并注入 AI 消息链（走 `/Tools/CdnDownloadImage`）
- **语音转文字**：vendor 自带 `voice.transcript`（微信官方转写），直接作文本，无需 STT
- **白名单**：仅白名单 wxid 的私聊能触发 AI（`wpp_allow_users` 配置）
- **群消息策略**：`atbot`（只回@机器人）/ `none`（忽略）/ `all`（都回）
- **账号信息页面**：Dashboard 插件 Pages 里查询账号昵称/wxid/在线状态
- **自动心跳**：账号离线自动拉上线（`AutoHeartBeat`）
- **在线状态卡片**：`get_stats` 返回账号状态

## 架构

```
[微信] ←→ [WPP vendor]
                │ ① push → POST http://<astrbot>:6199/wpp/webhook
                ▼
[astrbot 容器] astrbot-plugin-wpp (Platform 适配器)
                │ ② 解析 → WppMessageEvent → 事件总线
                ▼
        AstrBot 核心 (LLM/命令)
                │ ③ event.send → WPP 发送 API
                ▼
        微信收到回复
```

## 文件

| 文件 | 作用 |
|---|---|
| `main.py` | 插件入口 (Star) + Web API (账号信息查询) |
| `wpp_adapter.py` | 平台适配器 (webhook + 消息转换 + 白名单 + 发送) |
| `wpp_client.py` | WPP vendor HTTP API 封装 |
| `wpp_event.py` | 消息事件 (重写 send 真正发送到微信) |
| `pages/account-info/` | Dashboard 账号信息页面 |
| `config_schema.json` | 平台配置模板 |
| `logo.png` | 插件 + 平台适配器图标 |

## 部署

```bash
# 1. 拷入 AstrBot 容器插件目录
docker cp astrbot-plugin-wpp astrbot:/AstrBot/data/plugins/

# 2. 容器内确认依赖 (aiohttp)
docker exec astrbot sh -c 'python3 -c "import aiohttp" || pip install aiohttp'

# 3. 重启 AstrBot 加载适配器
docker restart astrbot

# 4. Dashboard (http://<host>:6185) → 平台适配器 → 添加 "微信 WPP" →
#    填 wpp_base_url / wpp_auth_token (X-Access-Token = authcode) / 白名单等
```

## 平台配置字段

| 字段 | 说明 |
|---|---|
| `wpp_base_url` | WPP API 地址 (如 `http://127.0.0.1:18062`) |
| `wpp_auth_token` | WPP X-Access-Token (= authcode) |
| `wpp_allow_users` | 白名单 wxid (逗号分隔)，空则所有私聊 |
| `wpp_group_reply` | 群消息策略: `atbot` / `none` / `all` |
| `webhook_host` / `webhook_port` / `webhook_path` | webhook 服务配置 |

## 凭证安全

- auth_token 走 Dashboard WebUI 配置 (`platform_config`)，**不写进代码/日志**
- 日志不打印明文凭证

## 文档

- [部署与运维指南](DEPLOY.md) — 部署/迭代/故障排查
- [更新日志](CHANGELOG.md) — 版本历史 + 踩坑记录

## 参考

- AstrBot 平台适配器开发文档: `docs/zh/dev/plugin-platform-adapter.md`
- AstrBot 内置范例: `qqofficial_webhook` 适配器 (webhook + commit_event 同构)

## License

MIT
