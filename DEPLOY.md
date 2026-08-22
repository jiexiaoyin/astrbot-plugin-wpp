# 部署与运维指南

## 环境

| 组件 | 说明 |
|---|---|
| AstrBot | Docker 容器，Dashboard http://<host>:6185 |
| WPP vendor | WeChatPadPro 容器（host 网络），API 端口自定义（如 28062/18062）|
| 插件位置 | AstrBot 容器 `/AstrBot/data/plugins/astrbot-plugin-wpp/` |
| 在线检查 | `GET /api/User/GetOnlineInfo?authcode=<x>` → `Data.online==True` 才算在线 |

## 🆕 新环境接入 Checklist（换 vendor/账号时）

换 `wpp_base_url` + `wpp_auth_token` 时，插件自动适配，但需保证以下前提：

1. **新 authcode 在新 vendor 的 Redis 有效**：
   ```bash
   # vendor Redis 里配置 authcode (origin 必须 local, status enabled)
   redis-cli -h <vendor-redis> set "PERM:AUTH:<authcode>" \
     '{"authcode":"<authcode>","origin":"local","status":"enabled","remoteStatus":"enabled","bindWxid":"<wxid>"}'
   redis-cli -h <vendor-redis> set "PERM:WX2AC:<wxid>" "<authcode>"
   ```
2. **新 vendor 能真实收发微信**（已扫码登录或有有效连接）
3. **webhook 公网可达**：配置 `webhook_public_url` 为 AstrBot 对外可访问的地址（如 `http://<astrbot公网>:6199`），vendor 才能回推消息
4. **Dashboard 更新配置**：平台适配器 → wpp → 改 `wpp_base_url` + `wpp_auth_token` → 保存 → 重启平台

> 插件自动做的事：用新地址调 API、新 authcode 认证、注册 webhook（用 `webhook_public_url`）、面板显示新地址。

## 部署（新环境）

```bash
# 1. 拷贝插件到 AstrBot 容器
docker cp astrbot-plugin-wpp astrbot:/AstrBot/data/plugins/

# 2. 容器内装依赖 (aiohttp)
docker exec astrbot sh -c 'python3 -c "import aiohttp" || pip install aiohttp'

# 3. 重启 AstrBot
docker restart astrbot
```

## Dashboard 配置

**平台适配器 → 添加 "微信 WPP"**：

| 字段 | 值 | 说明 |
|---|---|---|
| `wpp_base_url` | `http://<vendor-ip>:28062` | WPP API 地址（跨容器用宿主机 IP）|
| `wpp_auth_token` | 你的 authcode | X-Access-Token = authcode |
| `wpp_allow_users` | `wxid_abc,wxid_xxx` | 白名单（空=所有私聊都触发 AI）|
| `wpp_group_reply` | `atbot` | 群消息策略 |
| `webhook_port` | `6199` | webhook 监听端口 |
| `webhook_public_url` | `http://<astrbot公网>:6199` | **webhook 公网回调地址**（注册给 vendor）。必须公网可达，否则 vendor 推不回来 |

**注意**：
- WPP 容器 host 网络，AstrBot 容器内访问 vendor 必须用**宿主机 IP**（容器名 DNS 不通）
- authcode 绑定哪个微信，就在 vendor 面板（API 端口）用哪个微信登录（iPad/Mac 扫码）
- 账号登录后若 `online=false`，插件会自动调 AutoHeartBeat 拉上线
- **webhook 关键**：`webhook_public_url` 必须是 AstrBot 对外可访问地址（公网 IP/域名）。如果 vendor 和 AstrBot 在同一主机，`0.0.0.0` 不可达，必须填公网地址（如 `http://<公网IP>:6199`）

## 开发迭代流程

```bash
# 1. 编辑源码 (在 /root/dev/astrbot-plugin-wpp/)
# 2. 语法检查
python3 -m py_compile main.py wpp_adapter.py wpp_client.py wpp_event.py
# 3. 部署到容器
docker cp main.py astrbot:/AstrBot/data/plugins/astrbot-plugin-wpp/
docker cp wpp_adapter.py astrbot:/AstrBot/data/plugins/astrbot-plugin-wpp/
docker cp wpp_client.py astrbot:/AstrBot/data/plugins/astrbot-plugin-wpp/
docker cp wpp_event.py astrbot:/AstrBot/data/plugins/astrbot-plugin-wpp/
# 4. 重启
docker restart astrbot
# 5. 看日志
docker logs -f astrbot
```

## 同步 GitHub（每次更新必做）

```bash
cd /root/dev/astrbot-plugin-wpp
# 1. 脱敏检查（必须无输出）
grep -rniE "你的authcode|你的宿主IP|你的wxid" --include="*.py" --include="*.json" --include="*.md" .
# 2. 提交
git add -A
git commit -m "desc: 更新说明"
# 3. 推送
git push origin master
```

## 故障排查

### 收不到消息 / 不回复
1. 看 vendor 日志：`docker logs wechatpadbusiness-wechatpad-1` → 应见 `增量聊天消息已分发` + `Webhook消息发送成功`
2. 看 AstrBot 日志：`docker logs astrbot` → 应见 `event_bus [wpp(wpp)]`（收到）
3. 白名单：发送者 wxid 在 `wpp_allow_users` 里吗？（日志 `消息被白名单/群策略过滤`）
4. vendor push 格式：`{Data:{messages:[...]}}`（v1）—— 如果格式变了，`_extract_msg_src` 要适配

### 收到但 AI 不回复 / 回复发不出
1. AI 需要 LLM 提供商（Dashboard 配 deepseek/minimax 等）
2. 回复发不出：确认 `WppMessageEvent`（wpp_event.py）被用 —— AstrBot 走 `event.send()`，基类不真正发送

### 图片识别不了
- 插件已把图片完整传给模型（CDN 下载 + 注入）
- 模型视觉弱（如 MiniMax-M2.7）→ 换强多模态模型（Claude/GPT-4o）

### 账号显示"未登录"
- vendor 面板 http://127.0.0.1:18062 用该微信登录（iPad/Mac 扫码）
- 登录后插件自动心跳拉上线；也可手动 `POST /Login/AutoHeartBeat`

### 图片下载失败
- 优先 CDN 路径（CdnDownloadImage）；DownloadImg 兜底（需 local_id，64KB 截断）
- `ret:-104 cacheSize do not equal totalLen` = 缓存无此图，走 CDN

## 测试账号注意

- **你的测试微信 wxid** = 你实际在用的号（实际在用，会收到大量正常消息，都是干扰）
- 测试只关注这个号发的消息；其它 wxid 忽略
- 白名单保持你需要的 wxid（或按需调整）
