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

启动脚本和 systemd 模板分别固定 `umask 077` / `UMask=0077`，避免新建的数据库、
WAL/SHM、会话相关日志和上传二进制继承交互 shell 的宽松权限。部署时仍须把既有 `.env`、
数据库和日志收紧为 `0600`、私有运行目录收紧为 `0700`；公开头像目录单独按静态只读需求配置。
生产 `platform-ctl.sh` 与 CLI 默认拒绝非回环绑定。只有主机防火墙已经限制来源后，显式
`BZ_HOST=0.0.0.0 + BZ_ALLOW_LAN_BIND=1` 才可开放受控 LAN；systemd 同样从 `.env` 经过
CLI 门禁，直接调用 systemctl 不能绕过。未经 gate 的拒绝发生在运行目录、日志和数据库创建前。

Uvicorn 的 HTTP access record 与 WebSocket accepted/rejected record 原生会把
`path?query` 放进 positional args。平台在 handler 序列化前按 Uvicorn 记录结构只保留
method、path、HTTP version/status 等字段并剥离完整 query；CLI 以 `log_config=None`
保留这组 handler，防止 Uvicorn 默认配置在启动时覆盖过滤器。因此 `app.log` 与
stdout/stderr 汇聚的 `web.log` 都不得出现请求 query。此保护不依赖参数名黑名单，旧客户端
即使请求 `/play?token=...` 后被拒绝，token 也不会落本机 Uvicorn 日志。

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
- 埋点：登录成功/失败、注册、验证邮箱、改密、重置密码、登出、Bot 上传/版本、对局创建、人类对战、私有 Bot debug 读取、赛事创建、admin 删用户/bot/赛事/赛事报名、赛事状态/时间字段修改、广播预览/批准/取消、管理员通信回复、Bug 创建/附件/状态修改、代码模板写拒绝、runtime 配置修改、改角色。debug 读取只记 actor、match、成功/拒绝和成功条数，绝不写入内容；广播审计只记受众类型/数量与 public ID，不记正文、批准令牌或地址；投递错误只记稳定脱敏码，拒绝统一记 `result=fail`。

## 密码重置边界

用户密码重置只有邮件验证码自助流程：`POST /api/auth/request-reset` 对账号是否存在返回统一
结果，把短期验证码投递交给 worker；`POST /api/auth/reset-password` 以单事务 CAS 消费验证码、
更新密码并撤销该用户全部 session。响应和日志都不返回验证码。

管理员不能生成或接收可直接改密的 credential。旧
`POST /api/auth/admin/create-reset-token` 已从路由和生产 AuthManager 中删除，固定返回 404；
`password_resets` 表只为历史数据库迁移兼容保留，HTTP 路由与 AuthManager 均没有生成或消费
该表 credential 的入口。

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

## WebSocket 会话边界

人机对战 `/api/matches/{id}/play` 只使用登录响应写入的同源
`HttpOnly` `bz_session` Cookie 鉴权。前端 WebSocket URL 不携带任何会话查询参数，
后端也不接受 `?token=` 兼容；存在 `token` query 时即使同时携带合法 Cookie 也拒绝。
本机 Uvicorn 日志会额外剥离所有 request query，但浏览器诊断和上游反代仍能看见客户端实际
请求 URL，因此去掉 URL 凭据才是第一道边界，日志过滤只是纵深防御。

由于浏览器会自动携带 Cookie，握手还必须提供 `Origin`，并与生产必配的
`BZ_PUBLIC_ORIGIN` 规范化后严格相等。缺失 Origin、错源、`null`、配置缺失/非法都在
读取 session 前 fail closed，`SameSite=Lax` 不作为 Origin 校验的替代。反代必须保留浏览器
`Origin`；生产值必须是用户实际打开站点的 HTTPS origin（例如
`https://bot.example.com`，不带路径或查询）。REST 继续兼容 Cookie 与
`Authorization: Bearer`，不改变非 WebSocket 客户端契约。

## IP 透传链路（公网必需）

```
浏览器(真实公网IP) → nginx(X-Real-IP + X-Forwarded-For) → frp(TCP透传,不改HTTP头)
→ 本机 uvicorn:50380(socket peer=127.0.0.1) → 后端校验 peer 后读 XFF → 真实 IP
```

**关键开关 `BZ_TRUST_PROXY=1`**（公网经反代必需）：
- `.env` 已设。未开启时 `request.client.host` 是 `127.0.0.1`（frp/uvicorn 对端），限流全站共一个桶（失效）、登录 IP 记录错误。
- 开启后也不会无条件信任请求头：只有原始 ASGI socket peer 命中
  `BZ_TRUSTED_PROXY_CIDRS` 才读取。缺失/空配置安全默认精确
  `127.0.0.1/32,::1/128`，生产应显式设置该值；无效 CIDR 使应用在创建 Store/运行时前启动失败。
- Uvicorn `proxy_headers=False`，不会在应用校验前用 XFF 改写 `scope.client`。因此从
  `192.168.1.0/24` 直连的客户端即使伪造 `X-Real-IP/XFF`，限流、登录与审计仍使用真实 LAN peer；
  **绝不能**为了 LAN 直连把客户端网段加入 `BZ_TRUSTED_PROXY_CIDRS`。
- 对受信 peer，`client_ip()` 优先取合法 IP 形式的 `X-Real-IP`；无则取合法 XFF 的**倒数第
  `BZ_TRUSTED_PROXY_HOPS` 跳**（默认 1），而非最左可伪造段。值非法或链长不足时回退 socket peer。
- **nginx 推荐配置**（覆盖式，防伪造）：
  ```nginx
  proxy_set_header X-Real-IP $remote_addr;
  proxy_set_header X-Forwarded-For $remote_addr;   # 覆盖式（非 $proxy_add_x_forwarded_for 追加）

  # access_log 必须显式使用不含 args 的 $uri；默认 $request/$request_uri 会记录 query。
  log_format botbattle_no_args '$remote_addr "$request_method $uri $server_protocol" $status';
  access_log /var/log/nginx/botbattle.access.log botbattle_no_args;
  ```
  若必须用追加式（`$proxy_add_x_forwarded_for`），配套 `real_ip` 模块（`set_real_ip_from` + `real_ip_recursive on`）剥离可信段。
- `BZ_TRUSTED_PROXY_HOPS`：实际写入/追加 HTTP 转发头的受信代理层数（默认
  `1`=当前单层 nginx）。frp 是 TCP 透传、不写 HTTP 头，不能因此把该值改成 2；只有新增另一层
  会处理 XFF 的 HTTP 代理时才按真实拓扑调整。
- 本地开发（无反代）保持 `BZ_TRUST_PROXY` 不设或 `0`。

> `frp` 只是 TCP 透传，本身不会覆盖客户端伪造的 HTTP 头。把 loopback 列为 trusted peer 的硬前提是：
> 远端 FRPS 映射端口不能被公网客户端直接访问，所有进入该 tunnel 的流量必须先经过会覆盖身份头的
> nginx。若公网仍可直连 FRPS 的原始 50380，攻击者抵达本机时同样表现为 loopback peer，本 CIDR
> 边界无法区分；必须先用远端安全组/防火墙关闭该旁路，不能用扩大或缩小 hops 代替。

> 安全提示（审计 P1）：客户端可任意伪造 `X-Forwarded-For` 最左段。若 nginx 用追加式 `$proxy_add_x_forwarded_for` 且未配 `real_ip` 模块校验，攻击者每请求塞不同最左 IP 可绕过限流。代码侧已改为取倒数第 N 跳 + 优先 `X-Real-IP`（覆盖式不可伪造），彻底防御需运维正确配 nginx（覆盖式 XFF 或 `real_ip` 模块）。

> Uvicorn 的 query 过滤不会修改远端 nginx 日志。上线时必须核对实际 `access_log`
> 使用上述 `$uri` 格式，不能使用包含完整请求行的 `$request` 或包含参数的 `$request_uri`。

## 受控 LAN 直连

`0.0.0.0` 会监听机器的所有 IPv4 网卡，不等于“只监听 LAN”。开放前先审计现有规则、VPN/公网
网卡和路由器端口转发，确保 TCP 50380 仅允许源 `192.168.1.0/24`；不存在防火墙证明时保持
`127.0.0.1`。`BZ_ALLOW_LAN_BIND` 只是一道应用绑定门，不检测或证明主机防火墙已经生效；防火墙必须
由部署者单独配置和验收。例如使用 UFW 的主机可在确认没有更早的宽泛 allow 后执行：

```bash
sudo ufw allow proto tcp from 192.168.1.0/24 to any port 50380
sudo ufw deny 50380/tcp
sudo ufw status numbered     # allow LAN 必须先匹配；删除任何更宽的 50380 allow
```

然后才修改 `.env` 并更新/重启 systemd unit：

```bash
BZ_HOST=0.0.0.0
BZ_PORT=50380
BZ_ALLOW_LAN_BIND=1
BZ_TRUST_PROXY=1
BZ_TRUSTED_PROXY_CIDRS=127.0.0.1/32,::1/128
```

验收必须同时证明：本机 `127.0.0.1:50380` 健康、LAN 内不同来源可访问、LAN 伪造转发头时日志仍记
LAN peer、非 LAN 来源被防火墙拒绝、remote nginx/frp 路径仍记公网真实 IP。不要在路由器做 50380
公网端口转发。

LAN HTTP 直连的支持范围是静态页面、公开 API，以及客户端显式携带登录响应中 Bearer token 的 REST
请求。它不是完整的生产认证入口：生产 `BZ_SECURE_COOKIE=1` 时，浏览器不会向 HTTP IP 地址发送
Secure session Cookie；人机 WebSocket 又只接受 session Cookie，且 Origin 必须与单值
`BZ_PUBLIC_ORIGIN` 严格相等。因此 `http://192.168.1.13:50380` 不能使用人机 WebSocket，仍须通过与
`BZ_PUBLIC_ORIGIN` 一致且证书有效的正式 HTTPS 名称。不能为方便直连而关闭 Secure Cookie、放宽
Origin，或把该边界描述为 LAN 全功能访问。

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

### multipart 上传容量边界

纯 ASGI body limiter 在 FastAPI/Starlette 解析 multipart、创建 spool 文件之前按精确 POST 路径执行三档硬顶：

- `POST /api/bots`、`POST /api/bots/{id}/versions`：**51 MiB** 请求体（50 MiB Bot + 1 MiB multipart 开销），超限为 `413 upload_body_too_large`；
- `POST /api/feedback/bugs/{public_id}/attachments`：**6 MiB** 请求体（5 MiB 图片 + 1 MiB multipart 开销），超限为 `413 attachment_body_too_large`；
- `POST /api/auth/avatar`：**3 MiB** 请求体（2 MiB 图片 + 1 MiB multipart 开销），超限为 `413 avatar_body_too_large`。

`Content-Length` 只用于超限早拒绝，超限时不调用 `receive` 或下游；缺失、重复、非法或伪小长度不会被信任，每个实际 `http.request` chunk 在交给解析器前仍累计计数。越界 chunk 不下传，后续读取只见断开；容量异常使用 Starlette 的 `MultiPartException` 清理路径，已 rollover 到磁盘的 spool 文件同步关闭。真实 `http.disconnect` 原样透传，不伪报 413；代理身份头不参与容量判断。三档请求体硬顶之外，业务层仍分别执行 50/5/2 MiB 的实际文件大小与内容校验。

通过 body limiter 后，新建 Bot 与上传版本再共用一个进程级异步上传槽。端点签名不声明 `Form/File`，因此 FastAPI 先完成登录认证；取得槽后才手工解析 multipart，并在退出表单上下文时关闭 `UploadFile`。随后只读取 `50 MiB + 1` 个字节，文件本身超出即返回 `400 invalid_size`；槽从解析开始一直持有到隐藏版本落盘、沙箱预检、发布或回滚全部结束。因此不同 Bot ID 不能绕过单槽预检，同时在内存保留多份二进制或写入多个待检临时目录。等待槽使用主 ASGI 事件循环的 `asyncio.Semaphore`，不会让等待者占用默认线程池；超过 1 秒返回 `503 upload_busy` 与 `Retry-After`，且忙请求尚未读取任何 multipart 字节。客户端中断只取消 HTTP 等待；已开始的文件读取/worker 会先收敛，随后才释放槽，避免取消造成容量提前释放。

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
| `BZ_ALLOW_LAN_BIND` | `0` | 主机防火墙已限制受信 LAN 后，才允许 `BZ_HOST=0.0.0.0` |
| `BZ_TRUST_PROXY` | `0` | 公网经反代设 `1`，但仍只接受受信 socket peer 的代理头 |
| `BZ_TRUSTED_PROXY_CIDRS` | `127.0.0.1/32,::1/128` | 可写代理身份头的 peer；不能填直连客户端 LAN |
| `BZ_TRUSTED_PROXY_HOPS` | `1` | 实际写入 XFF 的受信 HTTP 代理层数；frp 不计 |
| `BZ_PUBLIC_ORIGIN` | 未设 | 人机 WebSocket 唯一允许的 HTTP(S) origin；生产必配 |
| `BZ_RATE_LIMIT` | `1` | 开启 IP 限流 |
| `BZ_HSTS` | `0` | HTTPS 部署可设 `1` 加 HSTS 头 |
| `BZ_SECURE_COOKIE` | `0` | 公网 HTTPS 部署必须设 `1`；仅本地 HTTP 保持 `0` |
| `BZ_LOG_DIR` | `logs` | 日志目录 |
| `BZ_LOG_LEVEL` | `INFO` | 日志级别 |
