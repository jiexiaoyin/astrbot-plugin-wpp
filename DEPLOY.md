# 部署与运维指南

## 环境

| 组件 | 说明 |
|---|---|
| AstrBot | Docker 容器，Dashboard http://<host>:6185 |
| WPP vendor | WeChatPadPro 容器（host 网络），API 端口自定义（如 28062/18062）|
| 插件位置 | AstrBot 容器 `/AstrBot/data/plugins/astrbot-plugin-wpp/` |
| 在线检查 | `GET /api/User/GetOnlineInfo?authcode=<x>` → `Data.online==True` 才算在线 |

## 🆕 新环境接入 Checklist（换 vendor/账号时）

换 vendor 或加账号时，需保证以下前提：

1. **authcode 在 vendor 的 Redis 有效**：
   ```bash
   # vendor Redis 里配置 authcode (origin 必须 local, status enabled)
   redis-cli -h <vendor-redis> set "PERM:AUTH:<authcode>" \
     '{"authcode":"<authcode>","origin":"local","status":"enabled","remoteStatus":"enabled","bindWxid":"<wxid>"}'
   redis-cli -h <vendor-redis> set "PERM:WX2AC:<wxid>" "<authcode>"
   ```
2. **vendor 能真实收发微信**（该微信已扫码登录到 vendor，或有有效连接）
3. **WS 端口可达**：插件通过 `wpp_ws_url` 连 vendor 的 WebSocket（WS 主通道，连接即拉离线+实时消息）。AstrBot 容器内必须能访问该地址（同主机用宿主机 IP / 公网域名）
4. **Dashboard 更新配置**：平台适配器 → wpp → 改 `wpp_base_url` + `wpp_auth_token` → 保存 → 重启平台。一个平台实例只对应一个 authcode/微信账号；多账号请添加多个平台实例

> 插件自动做的事：用新地址调 API、为当前 authcode 建立独立 WS 连接、面板显示状态。webhook 已退役，无需配置 `webhook_public_url`。

## 部署（新环境）

```bash
# 1. 拷贝插件到 AstrBot 容器
docker cp astrbot-plugin-wpp astrbot:/AstrBot/data/plugins/

# 2. 容器内装依赖 (aiohttp + websockets — WS 主通道必需)
docker exec astrbot sh -c 'python3 -c "import aiohttp, websockets" || pip install aiohttp websockets'

# 3. 重启 AstrBot
docker restart astrbot
```

## Dashboard 配置

**平台适配器 → 添加 "个人微信（Pad协议）"**：

### 平台实例配置
| 字段 | 值 | 说明 |
|---|---|---|
| `wpp_base_url` | `http://<vendor-ip>:28062` | WPP API 地址（跨容器用宿主机 IP）|
| `wpp_auth_token` | 你的 authcode | X-Access-Token = authcode；一个实例一个账号 |
| `wpp_ws_url` | `wss://<vendor-domain>/ws/sync` | **WS 主通道地址**（连接即收消息）。容器内可用宿主机 IP 或公网域名 |
| `wpp_allow_users` | `wxid_abc,123@chatroom` | 私聊 wxid / 群 ID（`@chatroom`）；空=私聊全部放行、群按 `group_reply` |
| `wpp_group_reply` | `atbot` | 群消息准入：`atbot` / `none` / `all`（`all` 只观察，不强制回复）|
| `wpp_proactive_groups` | 留空 | 显式主动候选群；群白名单已自动共用，通常无需填写 |
| `wpp_sync_heartflow_whitelist` | `true` | 群白名单同步到 Heartflow（未安装自动跳过）|
| `wpp_heartflow_whitelist_mode` | `auto` | `auto`=参考群白名单；`custom`=用 `wpp_heartflow_groups` 单独设置 |
| `wpp_heartflow_groups` | 留空 | `custom` 模式的心流群名单；必须是群白名单的子集 |
| `wpp_webhook_enabled` | `false` | webhook 已退役，保持 false（仅 WS）|

### 多账号（多个平台实例）
**一个平台实例 = 一个 `wpp_auth_token` = 一个微信账号。** Dashboard 里再添加一个
「个人微信（Pad协议）」平台实例，`id` 唯一（如 `wpp-shop2`），
`wpp_auth_token` 填另一个账号：

```text
平台实例 个人微信（Pad协议）  id=wpp        (authcode A) ── 微信账号 A
平台实例 个人微信（Pad协议）  id=wpp-shop2  (authcode B) ── 微信账号 B
```

同一个 authcode 不允许配到两个实例：后启动的实例会被拒绝
（日志提示 `同一 authcode 已被平台实例 ... 使用`），避免两个 WS 互抢消息。
旧的 `wpp_accounts` 单实例多账号配置已移除，升级后请按实例拆分。
详见 [README.md 多账号章节](README.md)。

### Heartflow 白名单

WPP 群白名单会自动同步到 Heartflow 的 `chat_whitelist`，有两种来源：

| `wpp_heartflow_whitelist_mode` | 同步目标 |
|---|---|
| `auto`（默认） | 群白名单（`allow_users` 的 `@chatroom`）+ 显式 `proactive_groups`，再与群白名单取交集 |
| `custom` | `wpp_heartflow_groups` 单独列出的群 |

**不变量：群白名单必须包含 Heartflow 白名单。** `custom` 中不在群白名单里的
群会被拒绝同步，启动日志/修改日志会出现：

```text
[WPP] Heartflow 白名单要求群白名单必须包含它们; 以下群不在 allow_users 群白名单, 已跳过同步: ...
```

处理方式：先 `/group add <群ID>` 把群加入群白名单，或从
`wpp_heartflow_groups` 移除该群。群白名单为空表示不限制群，此时
`custom` 群可以正常同步。

同步触发点：适配器启动、`/group` `/proactive` 修改、策略页保存、
`wpp_whitelist.json` 热更新、每 5 分钟自愈；可用 `/wpp_sync` 手动触发。
写盘后会同时更新已加载的 Heartflow 实例，无需重启。

**注意**：
- WPP 容器 host 网络，AstrBot 容器内访问 vendor 必须用**宿主机 IP** 或**公网域名**（容器名 DNS 不通）
- authcode 绑定哪个微信，就在 vendor 面板（API 端口）用哪个微信登录（iPad/Mac 扫码）
- 账号登录后若 `online=false`，插件会自动调 AutoHeartBeat 拉上线
- **WS 主通道**：连接建立即拉全量离线 + 实时消息；自动重连（指数退避 1s→30s），无需额外保活

## v0.12.0 平台技术名迁回 `wpp`

v0.11.0 曾把平台配置 `type`/`id` 改成中文「个人微信（Pad协议）」，这不符合
AstrBot 对平台技术标识保持 ASCII 的约定（参照 `aiocqhttp`、`discord`）。
v0.12.0 起 `type` 与默认 `id` 恢复为 `wpp`，Dashboard 展示名仍由
`adapter_display_name` 提供为「个人微信（Pad协议）」。升级前先停容器，
按以下顺序迁移（含 v0.11.x 运行时目录与插件目录残留清理）：

```bash
docker stop astrbot
DATA=/opt/1panel/apps/astrbot/astrbot/data
TS=$(date +%Y%m%d-%H%M%S)
BK="/data/astrbot-wpp-v012-migration-$TS"
mkdir -p "$BK"

# 1. 备份配置/数据库(含 WAL/SHM)/Heartflow/插件数据/插件目录残留
cp "$DATA/cmd_config.json" "$BK/"
cp "$DATA/data_v4.db"* "$BK/"
cp "$DATA/config/astrbot_plugin_Heartflow_config.json" "$BK/"
cp -a "$DATA/plugin_data/astrbot-plugin-wpp" "$BK/plugin-data"
cp -a "$DATA/plugins/astrbot-plugin-wpp/accounts" "$BK/plugin-dir-accounts" 2>/dev/null || true
cp -a "$DATA/plugins/astrbot-plugin-wpp/data" "$BK/plugin-dir-data" 2>/dev/null || true
```

2. 把 `cmd_config.json` 中该平台对象的 `type` 从 `个人微信（Pad协议）` 改为
   `wpp`；`id` 同步改回 `wpp`（多实例按自定义 ASCII id 命名，如
   `个人微信（Pad协议）-shop2` → `wpp-shop2`）。保留 UTF-8 BOM，建议用
   Python 脚本读改写。
3. 迁移数据库 UMO/统计/会话/偏好（字符串含中文，用 Python `sqlite3` 执行）：

```sql
UPDATE platform_stats SET platform_id='wpp' WHERE platform_id='个人微信（Pad协议）';
UPDATE provider_stats SET umo=replace(umo, '个人微信（Pad协议）:', 'wpp:') WHERE umo LIKE '个人微信（Pad协议）:%';
UPDATE conversations SET platform_id='wpp' WHERE platform_id='个人微信（Pad协议）';
UPDATE conversations SET user_id=replace(user_id, '个人微信（Pad协议）:', 'wpp:') WHERE user_id LIKE '个人微信（Pad协议）:%';
UPDATE preferences SET scope_id=replace(scope_id, '个人微信（Pad协议）:', 'wpp:') WHERE scope_id LIKE '个人微信（Pad协议）:%';
UPDATE umo_aliases SET umo=replace(umo, '个人微信（Pad协议）:', 'wpp:') WHERE umo LIKE '个人微信（Pad协议）:%';
-- 会话内容里模型工具回显的 UMO
UPDATE conversations SET content=replace(content, '个人微信（Pad协议）:FriendMessage:', 'wpp:FriendMessage:');
UPDATE conversations SET content=replace(content, '个人微信（Pad协议）:GroupMessage:', 'wpp:GroupMessage:');
```

`preferences` 中键为 `umop_config_routing` 的全局值若是
`{"val":{"个人微信（Pad协议）:*:*":"default"}}`，需把键改回 `wpp:*:*`。

4. Heartflow 配置里 `chat_whitelist` 的 `个人微信（Pad协议）:GroupMessage:*`
   改回 `wpp:GroupMessage:*`（同步逻辑只替换当前平台前缀，旧前缀不清理会残留）。
5. 运行时目录：v0.11.x 把实例 id 净化成 `accounts/Pad/`，改回
   `accounts/wpp/`；`webhooks/Pad.token` 改名为 `webhooks/wpp.token`
   （webhook 已退役，通常可忽略）。

```bash
PD="$DATA/plugin_data/astrbot-plugin-wpp"
[ -d "$PD/accounts/Pad" ] && mv "$PD/accounts/Pad" "$PD/accounts/wpp"
[ -f "$PD/webhooks/Pad.token" ] && mv "$PD/webhooks/Pad.token" "$PD/webhooks/wpp.token"
```

6. 清理插件源码目录残留（v0.12.0 起不再读取；已在上一步备份）：

```bash
rm -rf "$DATA/plugins/astrbot-plugin-wpp/accounts" "$DATA/plugins/astrbot-plugin-wpp/data"
```

7. 部署 v0.12.0 代码并启动：

```bash
docker start astrbot
docker logs --since 3m astrbot 2>&1 | grep -E 'WPP|wpp'
```

期望日志：

```
Loading IM platform adapter wpp (个人微信（Pad协议）) ...
[WPP] 账号已注册: id=wpp ...
[WPP:wpp] 账号已在线: wxid=...
[WPP:wpp WS] connected
```

> 历史提示：v0.11.0 的反向迁移（`wpp` → 中文）已废弃，不要重复执行。

## 开发迭代流程

```bash
# 1. 编辑源码 (在 /root/dev/astrbot-plugin-wpp/)
# 2. 静态检查 + 全量测试
ruff check .
docker exec -w /AstrBot/data/plugins astrbot \
  python -m pytest -q astrbot-plugin-wpp/tests
# 3. 真实主路径冒烟 (必做, 纯单测全绿不代表主路径能跑)
docker exec -w /AstrBot/data/plugins astrbot \
  python -m pytest -q astrbot-plugin-wpp/tests/test_mainpath_smoke.py
# 4. 部署到容器 (保留运行时 accounts/data)
rsync -a --delete \
  --exclude='.git' --exclude='__pycache__' --exclude='.pytest_cache' \
  --exclude='.ruff_cache' --exclude='accounts' --exclude='data' \
  ./ /opt/1panel/apps/astrbot/astrbot/data/plugins/astrbot-plugin-wpp/
# 5. 重启并确认版本/在线/WS
docker restart astrbot
docker logs --since 60s astrbot 2>&1 \
  | grep -E "astrbot-plugin-wpp|账号已在线|WS.*connected|Traceback"
```

> 若部署目录不是 host bind mount，可用 `docker cp`；原理相同。升级前建议备份
> 部署目录到 `/data/`，回滚时替换回旧目录并重启。

## 同步 GitHub（每次更新必做）

```bash
cd /root/dev/astrbot-plugin-wpp
# 1. 脱敏检查（必须无输出 — vendor 域名 / authcode / 真实 wxid / 宿主 IP 严禁公开）
#    用法: 把下面 4 个占位符替换成本机真实敏感串后再执行（不要把真实值写回本文件）
grep -rniE "<vendor-domain>|<authcode>|<real-wxid>|<host-ip>" --include="*.py" --include="*.json" --include="*.md" .
# 2. 提交
git add -A
git commit -m "desc: 更新说明"
# 3. 推送
git push origin master
```

## 故障排查

### 收不到消息 / 不回复
1. 看 vendor 日志：`docker logs knowhub-test-wechatpad-1` → 应见 `WebSocket已连接` + `增量聊天消息已分发`（WS 连接建立）
2. 看 AstrBot 日志：`docker logs astrbot` → 应见 `[WPP:账号id WS] connected`（WS 已连）+ `event_bus [wpp(wpp)]`（收到）
3. 白名单：发送者 wxid 在 `wpp_allow_users`（或账号白名单）里吗？（日志 `消息被白名单/群策略过滤`）
4. vendor push 格式：`{Data:{data:{messages:[...]}}}`（WS v1 格式）—— 如果格式变了，`_extract_all_msg_src` 要适配

### 群白名单已加，但未 @ 消息没有主动回复
1. 文件传输助手发 `/help`：确认群白名单数量、生效主动候选数量；
2. 确认只启用一个决策插件（Heartflow / 内置 `active_reply`），且其白名单
   包含 `wpp:GroupMessage:<群ID>`（`/proactive list` 可查看 UMO）；
3. Heartflow `custom` 模式下，群必须同时在 WPP 群白名单中，否则启动日志会
   出现「已跳过同步」WARNING；先用 `/group add <群ID>`；
4. 检查 Heartflow 的判断阈值/冷却（如 `reply_threshold=0.7`、
   `min_reply_interval_seconds=180`）；
5. 适配器只准入、不唤醒，未 @ 消息进入事件总线后仍需决策插件批准。

### /help 看不到新命令 / 版本不对
`/help` 从 `metadata.yaml` 读取版本并遍历命令注册表。若版本仍旧：
确认部署目录的 `metadata.yaml` 已更新、AstrBot 已重启，并检查启动日志
`Plugin astrbot-plugin-wpp (x.y.z)`。

### 进群欢迎语没有发
1. 文件传输助手发 `/welcome list`：确认已开启、模式、生效群、每群基线人数；
2. **首次同步只建基线**，不会欢迎现有成员 → 日志 `进群欢迎基线已建立 members=N`；
3. `join_welcome.enabled` 默认 false；改 `wpp_whitelist.json` 后约 3 秒热更新
   → 日志 `进群欢迎配置已热更新`；
4. 同一群 30 秒内只抓一次成员表 → 日志 `成员变动节流跳过`；
5. 一次 diff 出的人数超过 `maxBatch`（默认 3）视为基线重建，不发
   → 日志 `成员变动但不发欢迎语 added=N (…超过 maxBatch…)`；
6. 静默时段（`quietHours`）不发，但基线照常推进；
7. 基线落盘失败会放弃发送（至多一次语义）→ 日志 `保存群成员基线失败`；
8. vendor 漏推进群通知时那次进群不会有欢迎语（事件驱动，不轮询）。

### WS 连不上 / 断连
1. 确认 `wpp_ws_url` 可达：AstrBot 容器内 `curl http://<vendor>:<ws端口>/ws/sync` 应返回 401（服务在）
2. 容器内访问 vendor 用宿主机 IP 或公网域名（`127.0.0.1` 是容器自己，不通）
3. WS 断连会自动重连（指数退避 1s→30s），看日志 `连接断开/异常, Xs 后重连`

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
