# 安全与日志

公网暴露（nginx + frp 反代）后的日志体系、IP 透传、限流与安全响应头。

## 日志体系（三文件）

| 文件 | 内容 | 来源 |
|------|------|------|
| `logs/app.log` | 业务/系统日志（全来源执行队列/自动 producer、Docker cleanup/恢复、对局、Bot、异常） | 各模块 `logging.getLogger(__name__)` |
| `logs/access.log` | HTTP 访问日志（每请求一行，**含真实客户端 IP**） | `AccessLogMiddleware` |
| `logs/audit.log` | 安全审计日志（敏感操作：登录/上传/admin 写，含 actor+IP+结果） | `audit_log()` 辅助函数 |

格式统一 `时间 级别 [模块] 消息`，消息体含结构化字段（`ip=1.2.3.4 method=POST path=/api/auth/login status=200 dt=12ms`）。三文件各自 `RotatingFileHandler`（5MB×5 轮转）。

**配置**：`bzplat/backend/logging_config.py` 的 `setup_logging()`。access/audit 用独立 logger（`bzplat.access`/`bzplat.audit`，`propagate=False`，不污染 app.log）。

### access.log 字段
```
ip=<真实IP> method=<METHOD> path=<路径> status=<状态码> dt=<耗时ms>
```
跳过静态资源与 `/api/health`。

### audit.log 字段
```
ip=<真实IP> action=<动作> result=<ok|fail> user=<操作者> target=<目标> detail="<细节>"
```
- `result=fail` 记为 **WARNING** 级别（安全事件优先关注）。
- 埋点：登录成功/失败、注册、验证邮箱、改密、重置密码、登出、Bot 上传/版本、对局创建、人类对战、私有 Bot debug 读取、赛事创建、admin 删用户/bot/赛事/赛事报名、赛事状态/时间字段修改、广播预览/批准/取消、管理员通信回复、Bug 创建/附件/状态修改、代码模板写拒绝、runtime 配置修改、改角色、建重置令牌。debug 读取只记 actor、match、成功/拒绝和成功条数，绝不写入内容；广播审计只记受众类型/数量与 public ID，不记正文、批准令牌或地址；投递错误只记稳定脱敏码，拒绝统一记 `result=fail`。

## 私有 Bot debug 边界

Bot stdout 顶层 `debug` 是独立于动作/回放的私有 sidecar：单行传输 64 KiB 硬顶，收集阶段再做
单条 4 KiB、深度 4、容器 64 项、256 节点、每座位 512 条/128 KiB、整场
1024 条/256 KiB 的多级限制。文本先 NFC 归一化，移除 ANSI、控制字符、双向与不可见格式字符，
再脱敏 password/token/cookie/authorization/session/private-key 等敏感键和值；`Cookie`/`Set-Cookie`
从字段起整段遮蔽，避免分号后的复合 cookie 泄漏。容量已满时先做 O(1) 闸门，不再遍历 Bot 控制内容。

sidecar 只在 Bot-vs-Bot 终态后原子写独立表；写失败不回滚对局。普通双方 Bot owner 可对称读取，
赛事 organizer/admin 可在单场终态读取，参赛 Bot owner 等整赛 `finished/cancelled`；人类对战仅
admin 可审计空结果。赛事类型、`contest_id` 或赛事实体任一不一致时，非 admin 一律 fail-closed。
接口返回 `private, no-store`，拒绝不暴露记录存在性。内容不进入
`responses[]`、Bot 请求、result、REST replay、SSE/WS、通知或任何日志；前端只用文本节点/
安全 JSON 渲染，不解释 HTML、Markdown 或链接。
## IP 透传链路（公网必需）

```
浏览器(真实公网IP) → nginx(X-Real-IP + X-Forwarded-For) → frp(TCP透传,不改HTTP头)
→ 本机 uvicorn:50380 → 后端(client_ip 读 XFF) → 真实 IP
```

**关键开关 `BZ_TRUST_PROXY=1`**（公网经反代必需）：
- `.env` 已设。未开启时 `request.client.host` 是 `127.0.0.1`（frp/uvicorn 对端），限流全站共一个桶（失效）、登录 IP 记录错误。
- 开启后 `client_ip()`（`security.py`）**优先取 `X-Real-IP`**（nginx 覆盖式设置，客户端无法伪造）；无则取 `X-Forwarded-For` 的**倒数第 `BZ_TRUSTED_PROXY_HOPS` 跳**（受信代理前一跳，默认 1），而非最左可伪造段。
- **nginx 推荐配置**（覆盖式，防伪造）：
  ```nginx
  proxy_set_header X-Real-IP $remote_addr;
  proxy_set_header X-Forwarded-For $remote_addr;   # 覆盖式（非 $proxy_add_x_forwarded_for 追加）
  ```
  若必须用追加式（`$proxy_add_x_forwarded_for`），配套 `real_ip` 模块（`set_real_ip_from` + `real_ip_recursive on`）剥离可信段。
- `BZ_TRUSTED_PROXY_HOPS`：受信代理层数（默认 1=单层 nginx）。多层代理时调大（如 frp+nginx 两层设 2）。
- 本地开发（无反代）保持 `BZ_TRUST_PROXY` 不设或 `0`。

> 安全提示（审计 P1）：客户端可任意伪造 `X-Forwarded-For` 最左段。若 nginx 用追加式 `$proxy_add_x_forwarded_for` 且未配 `real_ip` 模块校验，攻击者每请求塞不同最左 IP 可绕过限流。代码侧已改为取倒数第 N 跳 + 优先 `X-Real-IP`（覆盖式不可伪造），彻底防御需运维正确配 nginx（覆盖式 XFF 或 `real_ip` 模块）。

## 限流（内存滑动窗口）

`RateLimitMiddleware`（`security.py`）按 `{真实IP}:{HTTP方法}:{路径}` 分桶；同一路径的只读 GET 不会消耗 POST 上传/写操作额度：

| 路径 | 限制 |
|------|------|
| `/api/auth/{login,register,verify-email,reset-password,request-reset,resend-verify}` | 20 次/60s |
| `/api/auth/captcha` | 60 次/60s |
| `POST /api/bots`、`POST /api/bots/{id}/versions` | 6 次/60s |
| `/api/matches/challenge` | 8 次/60s |
| `POST /api/feedback/bugs`、`POST /api/feedback/bugs/{id}/attachments` | 5 次/60s（独立反馈桶） |
| 其它 `/api/*` | 120 次/60s |
| 静态资源、`/api/health`、`/` | 不限 |

超限返回 `429` + `Retry-After`。单进程内存实现（多 worker 部署需换 Redis）。限流日志记 `rate limit: ip=... path=...`（WARNING）。

> 已知局限：WebSocket（`/api/matches/{id}/play`）与 SSE（`/api/matches/{id}/events`）不经 BaseHTTPMiddleware，当前无限流。

### Bot 上传容量边界

纯 ASGI body limiter 只精确匹配 `POST /api/bots` 与单路径段的
`POST /api/bots/{id}/versions`，在 FastAPI/Starlette 解析 multipart、创建 spool 文件之前生效。总请求体
上限为 **51 MiB**（50 MiB Bot + 1 MiB 有界 multipart 字段/边界开销）：ASGI scope 中的
`Content-Length` 只用于超限早拒绝，超限会立即返回结构化 `413 upload_body_too_large`，不调用 `receive`
或下游；缺失、错误或伪小长度不会被信任，每个 `http.request` chunk 在交给解析器前仍累计计数，
越界 chunk 不下传，后续
读取只见断开。真实 `http.disconnect` 原样透传，不伪报 413；X-Forwarded-For/X-Real-IP 等代理身份头
不参与容量判断。

通过 body limiter 后，新建 Bot 与上传版本再共用一个进程级上传槽。端点取得槽后只读取
`50 MiB + 1` 个字节，文件本身超出即返回 `400 invalid_size`；槽从读取开始一直持有到隐藏版本落盘、
沙箱预检、发布或回滚全部结束。因此不同 Bot ID 不能绕过单槽预检，同时在内存保留多份二进制或写入
多个待检临时目录。等待槽超过 1 秒返回 `503 upload_busy` 与 `Retry-After`。客户端中断只取消 HTTP
等待；已开始的文件读取/worker 会先收敛，随后才释放槽，避免取消造成容量提前释放。

## 安全响应头

`SecurityHeadersMiddleware`（`security.py`）：`X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY`、`Referrer-Policy`、`Permissions-Policy`。HSTS 仅 `BZ_HSTS=1` 时加。

## 验证码与投递脱敏

验证/重置码仅保存在有 TTL 的 `email_codes` 行。高优先级 delivery 仅保存模板 key/version、purpose 与 code-row 引用，不保存码、邮件正文或 HTML；也不创建 conversation/message，因而普通收件箱和 Admin thread API 无法读到码。worker 只在 SMTP 调用前从未过期、未使用且仍为最新的 code row 渲染内存正文；失效后直接取消。日志、audit、`last_error` 和旧 outbox 兼容投影均不记明文码、收件地址或供应商异常文本。

所有 SMTP 只能由 lifespan `DeliveryWorker` 调用，API/业务事务不建 SMTP 连接。每条邮件使用唯一 idempotency key 派生确定性 `Message-ID`，并有指数退避与最大尝试次数。由于 SMTP 接收后到 DB 提交前仍有崩溃窗口，这是有界 at-least-once，不是 exactly-once。

## Bug 诊断与附件隐私边界

诊断包不接受原始 User-Agent，浏览器/操作系统只允许粗粒度枚举。服务端白名单仅包含 build、去 query/fragment 的站内 route、服务端角色、viewport、locale/timezone、失败 API 模板/status/trace ID，以及公开 match/contest/queue 摘要。明确不读取或保存 cookie、session/token、email、实名、二进制路径、raw stderr、private debug、回放全文或底牌。

附件不属于 Bug JSON，只接受独立 multipart 上传的 PNG/JPEG/WebP/GIF。服务端同时核验声明 MIME、图片 magic/可解码性、像素/帧数、单文件 5 MiB 与每报告 5 个上限，再计算 SHA-256。文件以 `0700` 专属目录/`0600` 随机名保存在 DB 同级 `bug_attachments/` 隔离树；对外 API 只返回元数据，不返回内部路径。访客附件需创建时一次性返回的随机追踪令牌，库内只存其 SHA-256。

## 管理员日志查看

`/api/admin/logs?file={app|access|audit}&level=&q=&limit=`（admin only），文件参数白名单防路径穿越，响应的 `source` 仅为白名单文件名、不返回服务器绝对路径。后端先按 `时间 级别 [模块]` 首行聚合续行，再对完整记录做级别/关键字过滤，因此命中的多行 ERROR 会连同 traceback、Bot stderr 和 match 上下文一起返回；`limit` 不会截断单条记录。当前接口只读取当前轮转文件末尾最多 8000 个物理行，历史轮转文件与分页暂不在本接口范围内。前端 admin「日志」Tab 可切换三文件 + 级别/关键字过滤（如按 IP 或 action 搜）。

## 相关环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `BZ_TRUST_PROXY` | `0` | 公网经反代设 `1`（信任 XFF 取真实 IP） |
| `BZ_RATE_LIMIT` | `1` | 开启 IP 限流 |
| `BZ_HSTS` | `0` | HTTPS 部署可设 `1` 加 HSTS 头 |
| `BZ_SECURE_COOKIE` | `0` | HTTPS 部署可设 `1` 使 cookie secure |
| `BZ_LOG_DIR` | `logs` | 日志目录 |
| `BZ_LOG_LEVEL` | `INFO` | 日志级别 |
