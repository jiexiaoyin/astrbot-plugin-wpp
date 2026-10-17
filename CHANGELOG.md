# Changelog

## v0.5.0 (2026-10-17) — 出站引用回复 / 链接卡片 / 位置 / AtAll

### 新增
- **引用回复**：出站 `Reply` 组件 + 后续短文本合并为 appmsg `type=57`，走
  `/Msg/ShareLink`（vendor `/Msg/Quote` 不可用的 gewe 范式）；`title/des` 为回复正文，
  `refermsg` 携带 `svrid`（Reply.id）与 `fromusr`（Reply.sender_id）
  - 正文超过 500 字符时降级为普通文本，避免截断长回复
- **链接卡片**：`Share(url, title, content, image)` → `type=5` appmsg XML
- **位置**：`Location(lat, lon, title, content)` → `/Msg/ShareLocation`（X=纬度, Y=经度）
- **AtAll**：`AtAll` 不再被丢弃，`At` 字段传 `all`
- 新增 `build_quote_reply_xml` / `build_share_link_xml` 纯函数（XML 转义）

### 测试
- 新增 `tests/test_send.py`（7 个用例）
- 测试总数 70

## v0.4.1 (2026-10-17) — 语音缺失转写时接入 AstrBot STT

### 新增
- **语音 STT 兜底**：vendor 没有 `voice.transcript` 时，从 `voice.download_context` 调用
  `/Tools/DownloadVoiceBinary` 下载 silk 原文件，注入 AstrBot `Record` 组件；
  由核心 preprocess 阶段 `ensure_wav`（内置 silk→wav）+ STT Provider 转写
  - 与 NapCat 等平台路径一致，**不重复造 STT**
  - 下载失败保留 `[语音] (下载失败)` 占位，不影响其他消息
  - 20MB 上限；`to_user_name` / `from_user_name` 按 vendor v1 契约传参

### 测试
- 新增 3 个语音用例（download_context 提取、vendor 转写优先、silk→Record）
- 测试总数 63

## v0.4.0 (2026-10-17) — appmsg 结构化解析 + 全类型卡片可读

### 新增：`wpp_appmsg.py`（纯函数卡片解析器）
- 微信 appmsg / XML 卡片不再把**原始 XML 直接塞给 LLM**，统一解析为结构化摘要：
  - **链接**（type 5）→ `[链接] 标题 | 描述 | URL`
  - **小程序**（type 33/36）→ `[小程序] 标题 | 页面路径 | appid`
  - **合并转发/聊天记录**（type 19）→ `[合并转发]` + 逐条 `发送人: 内容`（最多 20 条）
  - **引用回复**（type 57）→ AstrBot 原生 `Reply` 组件
  - **位置**（msgType 48）→ `[位置] 名称 | 纬度,经度`
  - **名片**（type 42）→ `[名片] 昵称 | wxid`
  - **接龙/群公告**（`app_message` / `#接龙`）→ `[接龙]` + 正文
  - **文件**（type 6/8）→ 文件名 + 附件上下文
  - **视频号/公众号卡片**、未知卡片 → `[卡片] 标题 | 描述` 兜底
- 空 `content` 的卡片不再被整条丢弃（旧版直接 `return None`）
- XML 安全清洗：去 BOM/控制字符、`&lt;` 反转义、裸 `&` 修复、256KB 上限、解析失败降级为纯文本

### 修复
- **卡片内容泄漏原始 XML**：历史消息/未知类型中的 `<msg><appmsg>...` 现在会被清洗成可读文本
- **文件卡片附件丢失**：文件卡片统一从 `app.file.download_context` / `appattach` 提取 `attach_id`，旧版只看 `file` 字段
- **文件名为 XML**：标题缺失时不再把整段 XML 当文件名
- **支付卡片误判**：`红包/转账` 判定改为严格整句匹配 + 卡片结构校验；接龙正文含“红包”不误杀，链接卡片标题含“红包”不误杀
- **群会话 ID 误取发送者**：`src_chatroom()` 不再把 `sender_id` 当群 ID（避免私聊被误判成群聊）
- **@ 判断**：优先使用 vendor 结构化 `at_user_list` / `<atuserlist>`，不再要求正文含 `@`；`atbot` 模式不再因 `recipient_id` 启发式误放行
- **媒体富化丢组件**：下载图片/文件/视频时保留 `At` / `Reply` / `Json` 等非媒体组件
- **消息类型兼容**：字符串类型（`"image"`/`"voice"`/`"app"`）与仅 `kind` 字段的推送均可识别
- **direction 兼容**：`in` / `recv` / `received` / `true` 均视为入站

### 测试
- 新增 `tests/test_appmsg.py`（21 个用例），覆盖链接/小程序/位置/名片/合并转发/引用/接龙/文件/支付/系统消息/@ 列表/原始 XML 防泄漏
- 测试总数 60（原 39）

## v0.3.1 (2026-10-17) — 系统消息静默 + 私聊黑名单

### 修复
- **"全部放行"模式下系统消息触发 AI 回复**：公众号、微信团队、微信支付、服务通知、财付通、微信安全中心、订阅号消息等系统/服务会话，现在**任何模式**都在进入 AstrBot 之前静默
  - 判据统一收敛到 `wpp_messages.skip_reason()`（`to_astrbot_message` 与账号处理入口共用，避免两处漂移）
  - 识别 sender/conversation/recipient 中的 `gh_` 前缀与系统 username；兼容服务通知 sender 为空、仅 `conversation_id=notifymessage` 的形态
  - 同时静默：vendor `kind=status/system`、`msgType=51`（状态）、`10000`（系统消息）、`10002`（撤回）、`<sysmsg>` XML
- **红包/转账/支付卡片静默**：只按卡片结构（`msgType=49` + app 元数据/XML）判定，**不按正文"红包"关键词**，避免误杀自带"红包"字样的群接龙（历史事故）
- 每分钟 INFO 汇总新增被忽略消息分类计数（`system=` / `payment_card=` / ...），排查时无需开 debug

### 新增
- **私聊黑名单**：`wpp_block_users`（单账号）/ `wpp_accounts[].block_users`（多账号），全部放行模式下可排除指定 wxid；群聊里也会屏蔽该成员
- filehelper 命令 `/block add|del|list`，运行时生效并持久化（与 `/user` 白名单互斥：加入黑名单会自动移出白名单）
- 测试增至 39 个

## v0.3.0 (2026-10-17) — 贴合 AstrBot 规范的重构 + 稳定性修复

### 架构重构（模块拆分）
- 新增 `wpp_messages.py`：vendor 消息解析/转换纯函数（事件过滤、@/引用、媒体元数据）
- 新增 `wpp_policy.py`：白名单/群回复策略纯函数（可单测）
- 新增 `wpp_media.py`：图片/文件/视频下载与消息链富化
- 新增 `wpp_send.py`：出站消息链发送（Plain/At/Image/File/Record/Video）+ 长文本分片
- 新增 `wpp_commands.py`：filehelper 命令注册表
- 新增 `wpp_storage.py`：运行时数据持久化（原子写 + 旧目录迁移）
- `wpp_account.py` / `wpp_adapter.py` / `main.py` 相应重写精简

### 稳定性修复
- **WS 收包不再被媒体下载阻塞**：`asyncio.Queue`（上限 1000）+ 单 worker 顺序处理；队列满时丢弃最旧并计数告警
- **日志刷屏根因修复**：vendor 的 `friend_circle_update` 等非消息事件同样带 `items`，旧版误当消息处理（72h 内 4805 条 WARNING）。现按 `Data.type` 白名单过滤，过滤日志降为 debug + 每分钟 INFO 汇总
- **去重落盘**：消息 ID 环形缓冲（500）持久化到 `dedup.json`，重启后不再重复处理历史消息
- **群 @ 判断修正**：`require_at_mention` 必须 @ 机器人本人（旧版 @ 任意群友都放行）
- **群图片下载目标修正**：群聊 `DownloadImg` 的 `to_wxid` 使用群 ID（与 wpp-openclaw 一致）
- **临时文件管理**：媒体文件用唯一文件名，并 `event.track_temporary_local_file()` 交给 AstrBot 事件结束清理
- **文件/视频发送**：新增 `SendFile` / `SendVideo` / `SendVoice` 端点支持
- **发送重试安全**：发消息类非幂等接口网络错误不重试，避免重复发送；文件下载改为流式 + 200MB 上限

### AstrBot 规范对齐
- 平台元数据缓存在 `self.metadata`，`support_streaming_message=False`
- 新增 `create_event()`；`send_by_session()` 复用 `send_chain` 并调用基类上报 metric
- 会话 → 账号绑定（`bind_session`），多账号下主动推送也能路由到正确账号
- 运行时数据迁移到 `data/plugin_data/astrbot-plugin-wpp/`（不再写插件源码目录）
- 移除废弃的 `@register` 装饰器（类名 `WppPlugin` 自动发现）
- `config_metadata` 修正：`isSecret` → `secret`，`wpp_webhook_enabled` 使用 `bool` 类型
- 修复 `/wpp_status` 引用已移除的 `platform._api`（现遍历各账号）；Web API 的 `base_url` 增加配置白名单校验（防 SSRF）

### 测试
- 新增 `tests/test_core.py`（事件过滤/消息转换/策略/分片/持久化）；总计 32 个测试通过

## v0.2.0 (2026-08-26) — WS 实时主通道 + 单实例多账号

### WS 实时通道（主通道，webhook 退役）
- **WS 实时主通道**：连 vendor `/ws/sync`（`wpp_ws_url`），连接即拉全量离线 + 实时消息，自动重连（指数退避 1s→30s）
- **根因修复**：之前 vendor 长连接断无人连 WS → 消息进离线队列 → webhook 收不到。WS 客户端连上即恢复
- **webhook 退役**：`wpp_webhook_enabled` 默认 false（WS 断时 webhook 也收不到，无保留价值）

### 单实例多账号（v0.2.1 完善）
- **`WppAccount` 类**：单实例内管理多个微信账号，同一 vendor 地址通过不同 `authcode` 隔离
- **每账号独立**：WS 连接 / 白名单 / 群策略 / 黑名单 / 消息去重 / 机器人身份 / filehelper 命令
- **`wpp_accounts` JSON 配置**：多账号模式；`wpp_auth_token` 单账号向后兼容
- **白名单按账号隔离**：`accounts/<account_id>/wpp_whitelist.json`
- **测试**：`tests/` 7 个 pytest（账号解析/隔离/回退）

### 新增文件
- `wpp_account.py` — WppAccount 类（账号级 WS + 白名单 + 消息处理）
- `tests/test_multi_account.py` — 多账号架构测试

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
