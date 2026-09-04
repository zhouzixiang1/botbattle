# 安全与日志

公网暴露（nginx + frp 反代）后的日志体系、IP 透传、限流与安全响应头。

## 日志体系（三文件）

| 文件 | 内容 | 来源 |
|------|------|------|
| `logs/app.log` | 业务/系统日志（全来源执行队列/自动 producer、Docker cleanup/恢复、对局、Bot、异常） | 各模块 `logging.getLogger(__name__)` |
| `logs/access.log` | HTTP 访问日志（每请求一行，**含真实客户端 IP**） | `AccessLogMiddleware` |
| `logs/audit.log` | 安全审计日志（敏感操作：登录/上传/admin 写，含 actor+IP+结果） | `audit_log()` 辅助函数 |

格式统一 `时间 级别 [模块] 消息`，消息体含结构化字段（`ip=1.2.3.4 method=POST path=/api/auth/login status=200 dt=12ms`）。所有来自请求、异常或业务数据的字段都会先转成可打印、单行且最多 1024 字符的日志值；HTTP path 只记不含 query 的路径，避免换行伪造日志记录、无界放大或把 URL 凭据写入日志。三文件各自 `RotatingFileHandler`（5MB×5 轮转）。

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
结果，把短期验证码投递交给 worker；`POST /api/auth/reset-password` 在 `BEGIN IMMEDIATE` 中对
最新 credential 做常量时间比较，并为每个 credential 持久记录最多 5 次失败预算；耗尽即作废，
成功时用同一事务 CAS 消费验证码、更新密码并撤销该用户全部 session。跨 IP、进程重启或并发
提交不能重置这份预算。响应和日志都不返回验证码。

管理员不能生成或接收可直接改密的 credential。旧
`POST /api/auth/admin/create-reset-token` 已从路由和生产 AuthManager 中删除，固定返回 404；
`password_resets` 表只为历史数据库迁移兼容保留，HTTP 路由与 AuthManager 均没有生成或消费
该表 credential 的入口。

## 浏览器会话边界

浏览器只用同源 `HttpOnly` `bz_session` Cookie；前端不把七日 bearer、用户邮箱或实名资料写入
`localStorage`，历史 `bzplat_token` / `bzplat_user` 仅在启动时删除且永不读取。当前用户投影只驻留
页面内存。登录、成功登出和成功改密会发布一个不含凭据或 PII 的随机 auth epoch，经
`BroadcastChannel` 与 `storage` 事件通知同源标签页重新读取 `/api/auth/me`；两种通知能力都不可用
时，私有请求会逐次先对账身份而不是沿用旧投影。

身份代际在发送请求、收到响应头和完整读取响应体后都会复核；慢 JSON/错误体、解析异常和 XHR
不能在另一标签页已换号后把旧账号重新写回，也不能让旧 UI 操作借新 Cookie 提交。`logout` 只有在
服务端返回 2xx、确认 HttpOnly Cookie 已失效后才清理本地投影并跳转；网络错误或非 2xx 会保留当前
页面并明确报错，避免界面显示“已退出”而刷新后会话仍恢复。后端停用用户时在同一事务删除全部
session；重新启用账号不会复活旧或被盗 Cookie。
旧标签页发起的 `/api/auth/me` 即使在响应体读取前发生网络失败或 `AbortError`，也必须在清空投影前
复核共享 store revision；若新标签页已完成身份对账，迟到的旧失败只能丢弃，不得把新账号投影清成访客。

匿名认证命名空间的所有响应（含路由/依赖执行前的 `413/422/404`）统一使用
`private, no-store, max-age=0` 与 `Vary: Authorization, Cookie`。认证 `422` 只返回每项错误的
`loc/msg/type`，禁止回显 Pydantic 的 `input/ctx/url`，避免密码、验证码或个人信息进入浏览器、代理缓存或错误采集。

## 挑战座位授权边界

`POST /api/matches/challenge` 以 `my_bot_id` 表示发起方身份，并由 `my_seat=0/1` 声明该 Bot
落在物理座位 1 或 2。普通用户与组织者无论选择哪一侧，都只能提交自己拥有的 `my_bot_id`；
把本人 Bot 偷放进 `opponent_bot_id`、再冒用他人 `my_bot_id` 仍固定返回 403 并写拒绝审计。
管理员可为运维/验收目的从公开的 active+runnable Bot 集合中选择任意发起方 Bot。该管理员例外
只放宽 Bot owner 检查，不放宽执行边界：显式 `my_bot_version_id` 仍必须属于语义 my Bot，双方版本
会连同 environment/local-agent 依据 `my_seat` 整体映射，且当前版本仍须通过文件完整性、Linux
x86_64 ELF、运行模式、激活状态和游戏一致性校验，失败不创建 execution job 或 Match。

## 公开单场对局日志边界

`GET /api/matches/{id}/log` 只导出一场已经进入 `completed/aborted` 的公开 canonical replay。
Store 在同一个 SQLite 读快照中核对权威 Match、原始 replay 尾项和公共投影：只有持久化数组最后
一项与状态对应为 `match_end/error` 时才返回 JSON v1；活动局、未知游戏、损坏契约或终态回放尚未
落稳均 fail closed，不把内存事件前缀或服务端合成终局伪装成完整文件。实现只读既有 Match 与
`match_replays`，不增加表、列或迁移。

文件只包含正向白名单的公开 `match` 与结构化 `replay`。事件继续经过 REST/SSE/WS 共用的
`store.public_contract`：未知诊断事件默认丢弃，历史技术故障只输出有界稳定码与脱敏说明，原始
message/path、Bot 二进制或版本路径、执行配置、令牌、stdout/stderr、应用日志和私有 debug 均不得
进入。响应为单场 `application/json` attachment，固定 `no-store`、`nosniff` 与有界 ASCII 文件名；
同一持久状态确定性序列化，不加入下载时间。此前明确下线的按月/批量 matchpacks、公开列表和
`/data` 页面不因单场导出恢复。

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
`responses[]`、Bot 请求、result、REST replay、SSE/WS、通知、公开对局日志、游戏专项棋谱或任何
应用日志；前端只用文本节点/安全 JSON 渲染，不解释 HTML、Markdown 或链接。

## WebSocket 会话边界

人机对战 `/api/matches/{id}/play` 只使用登录响应写入的同源
`HttpOnly` `bz_session` Cookie 鉴权。前端 WebSocket URL 不携带任何会话查询参数，
后端也不接受 `?token=` 兼容；存在 `token` query 时即使同时携带合法 Cookie 也拒绝。
本机 Uvicorn 日志会额外剥离所有 request query，但浏览器诊断和上游反代仍能看见客户端实际
请求 URL，因此去掉 URL 凭据才是第一道边界，日志过滤只是纵深防御。

由于浏览器会自动携带 Cookie，握手还必须提供 `Origin`，并与生产必配的
`BZ_PUBLIC_ORIGIN` 规范化后严格相等。缺失 Origin、错源、`null`、配置缺失/非法都在
读取 session 前 fail closed；随后握手还要在任何 session/Match 数据库读取前通过独立的可信
peer IP 速率与全局并发闸门（每个 peer 30 次/60 秒、全局最多 16 个并行握手、最多 2048 个活跃
peer 桶；桶满且无过期项可回收时拒绝新 peer）。缺失 Cookie 不查询 session，session 无效也不查询
Match；所有成功和拒绝路径都会释放并发位。`SameSite=Lax` 不作为 Origin 校验的替代。反代必须保留浏览器
`Origin`；生产值必须是用户实际打开站点的 HTTPS origin（例如
`https://bot.example.com`，不带路径或查询）。REST 继续兼容 Cookie 与
`Authorization: Bearer`，不改变非 WebSocket 客户端契约。

通过鉴权的人机连接另有独立的进程级配额：全局最多 32、单局最多 4、单用户最多 4；在 snapshot
或 2000 帧队列分配前同步预留，断开、取消、异常、终态与 shutdown 均幂等释放。每个入站动作先按
同一用户和可信 peer IP 共享的令牌桶计费（burst 10，之后 2 帧/秒），并在 JSON 解析或 SQLite
查询前执行 4 KiB UTF-8 硬顶；用户跨 4 个连接或重连不能获得新的独立预算。通过这两道门后，每帧
及动作真正提交前仍重新校验 session、active 用户和 Match owner，密码修改、管理员撤销或 owner
漂移都会关闭连接，而不是让已握手的旧会话继续落子。

同一持续授权门也保护服务器到浏览器的方向：sender 每次从订阅队列取出 event 后，都在紧邻
`send_json` 前重验握手时的 exact session、active user，以及 Match 仍为 `TYPE_HUMAN` 且
`human_user_id/human_seat` 与连接冻结身份逐值一致。撤销、停用、owner/type/seat 漂移或读取异常时，
该 event（包括私有牌面）及所有后续业务帧都不得发送；连接只发送一次固定 `session_revoked` reject，
再以 1008/`session_revoked` 关闭并释放 quota。sender 与 receiver 共用同一 rejection lock，并发发现
失效的所有 caller 都等待唯一 reject + close 完成，不能让先结束的任务取消尚未完成的安全关闭。

客户端把 1008/1009/1013 和稳定的策略原因（包括 `rate_limit_exceeded`、`session_revoked`、
`forbidden`、`message_too_large`、`invalid_game_id`）视为安全终止：展示对应原因并停止自动重连。
只有没有策略原因的网络 1001/1006 才允许有界指数退避。服务端 snapshot 只用于重同步权威局面，
不能清除客户端已锁存的策略关闭，也不能让该关闭后的重连预算复活。

Local AI `/api/local-ai/connect` 使用同一个有界 gate 原语但独立计数：每个可信 peer 20 次/60 秒、
全局 16 个并行握手、最多 2048 个活跃 peer 桶。它仍按自身 Bearer、子协议与连接身份契约鉴权，
不会与人机 Cookie WebSocket 共用额度或透露某个 Bot identity 是否存在。
停用用户或 Bot 时，Store 在同一 `BEGIN IMMEDIATE` 中撤销 identity/lease，只返回本事务
newly-revoked targets 及每项 authoritative frozen `owner_id/bot_id` scope，不扫描历史 revoked
tombstone。service 严格校验 scope 后，在任何 `await` 前把完整批次登记到有界的进程内 pending
registry；逐项 `hub.revoke` 成功后才 forget 当前连接与 pending 项。若 DB 已提交但首次 transport
关闭/输出失败，重复停用只从该 scope 的有界当前连接/待收敛 registry 重试，不能用历史墓碑重建
工作列表。因此事务前快照不会漏掉并发创建的 agent，恢复工作量也不随累计撤销历史增长。

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
- **SSE 观赛流禁止缓冲**：`/api/matches/{id}/events` 响应自带 `X-Accel-Buffering: no` 与 `Cache-Control: no-store`，nginx 依据该头对本响应禁用 `proxy_buffering` 并逐帧转发；反代把首帧 snapshot 扣在缓冲里时，直播端棋盘会永久停在「加载中」。运维侧为该路径额外配置 `proxy_buffering off` 属纵深防御，不是必需项。两个失效陷阱：把 `text/event-stream` 加入 `gzip_types`（gzip 过滤会重新聚合帧）或配置 `proxy_ignore_headers X-Accel-Buffering`，都会使该头不起作用。
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

`RateLimitMiddleware`（`security.py`）先按真实 IP 扣全 API 共享桶，再按
`{真实IP}:{HTTP方法}:{规范化路径}` 扣路由桶；同一路径的只读 GET 不会消耗 POST 上传/写操作额度。
Bot 版本、反馈附件和 Local AI rotate 等动态资源 ID 会折叠为固定模板，不能靠轮换 ID 获得新额度：

| 路径 | 限制 |
|------|------|
| 全部 `/api/*`（每个真实 IP 的共享上限） | 600 次/60s |
| `/api/auth/{login,register,verify-email,reset-password,request-reset,resend-verify}` | 20 次/60s |
| `/api/auth/captcha` | 60 次/60s |
| `POST /api/bots`、`POST /api/bots/{id}/versions` | 6 次/60s |
| `/api/matches/challenge` | 8 次/60s |
| `POST /api/feedback/bugs`、`POST /api/feedback/bugs/{id}/attachments` | 5 次/60s（独立反馈桶） |
| `POST /api/local-ai/agents/{id}/rotate` | 5 次/60s |
| 其它 `/api/*` | 120 次/60s |
| 静态资源、`/api/health`、`/` | 不限 |

超限返回 `429` + `Retry-After`。最多保留 50000 个活跃桶；达到上限时先回收过期桶，仍满则
fail closed，不驱逐别人的活跃预算或为攻击者创建无界 key。单进程内存实现（多 worker 部署需换
共享存储）。限流日志记 `rate limit: ip=... path=...`（WARNING）。

WebSocket 与 SSE 不经 `BaseHTTPMiddleware`，因此使用各自的显式闸门而不是上述 HTTP 桶。公开
SSE `/api/matches/{id}/events` 最多全局 64、单局 32、单可信 IP 8 个活跃订阅，并在 2000 帧队列
分配前原子预留；response 首 body 前断开、构造失败、取消、终态和 shutdown 都会从 response scope
幂等释放。人机 WebSocket 的连接、握手和动作预算见上文「WebSocket 会话边界」。

### 请求体容量边界

纯 ASGI body limiter 在 FastAPI/Pydantic/Starlette 解析 JSON 或 multipart、创建 spool 文件之前按
method + 精确/规范化 API 路径执行硬顶：

- 认证与账号资料 JSON：**64 KiB**，超限为 `413 auth_body_too_large`；
- 其余 `POST` / `PUT` / `PATCH` / `DELETE` API：**1 MiB**，超限为 `413 api_body_too_large`；

- `POST /api/bots`、`POST /api/bots/{id}/versions`：**101 MiB** 请求体（100 MiB Bot + 1 MiB multipart 开销），超限为 `413 upload_body_too_large`；
- `POST /api/feedback/bugs/{public_id}/attachments`：**6 MiB** 请求体（5 MiB 图片 + 1 MiB multipart 开销），超限为 `413 attachment_body_too_large`；
- `POST /api/auth/avatar`：**3 MiB** 请求体（2 MiB 图片 + 1 MiB multipart 开销），超限为 `413 avatar_body_too_large`。

`Content-Length` 只用于超限早拒绝，超限时不调用 `receive` 或下游；缺失、重复、非法或伪小长度不会被信任，每个实际 `http.request` chunk 在交给解析器前仍累计计数。越界 chunk 不下传，后续读取只见断开；容量异常使用 Starlette 的 `MultiPartException` 清理路径，已 rollover 到磁盘的 spool 文件同步关闭。真实 `http.disconnect` 原样透传，不伪报 413；代理身份头不参与容量判断。请求体硬顶之外，业务层仍分别执行 100/5/2 MiB 的实际文件大小与内容校验。Bot 文件与 ASGI envelope 都引用 `runtime/limits.py::MAX_BOT_UPLOAD_BYTES`，避免新建/版本端点与 body limiter 漂移。

通过 body limiter 后，新建 Bot 与上传版本再共用一个进程级异步上传槽。端点签名不声明 `Form/File`，因此 FastAPI 先完成登录认证；取得槽后才手工解析 multipart，并在退出表单上下文时关闭 `UploadFile`。随后只读取 `100 MiB + 1` 个字节，文件本身超出即返回 `400 invalid_size`；槽从解析开始一直持有到隐藏版本落盘、沙箱预检、发布或回滚全部结束。因此不同 Bot ID 不能绕过单槽预检，同时在内存保留多份二进制或写入多个待检临时目录。等待槽使用主 ASGI 事件循环的 `asyncio.Semaphore`，不会让等待者占用默认线程池；超过 1 秒返回 `503 upload_busy` 与 `Retry-After`，且忙请求尚未读取任何 multipart 字节。客户端中断只取消 HTTP 等待；已开始的文件读取/worker 会先收敛，随后才释放槽，避免取消造成容量提前释放。

## 安全响应头

`SecurityHeadersMiddleware`（`security.py`）：`X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY`、`Referrer-Policy`、`Permissions-Policy`。HSTS 仅 `BZ_HSTS=1` 时加。

## 验证码与投递脱敏

明确运行于 `BZ_SKIP_CAPTCHA=1` 的隔离 Playwright 套件，其共享登录 helper 只精确 mock
`GET /api/auth/captcha`，登录 POST 和其余认证请求仍到真实 QA 后端。这避免大量角色登录从共享 QA IP
污染 captcha 桶，不构成绕过生产安全边界：真实 captcha 生成/校验、错误响应和 60/60s 限流由启用
限流的独立安全测试覆盖，生产入口继续无条件拒绝 `BZ_SKIP_CAPTCHA`。

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
| `BZ_PUBLIC_ORIGIN` | 未设 | Cookie 鉴权写请求与人机 WebSocket 唯一允许的 HTTP(S) origin；生产必配 |
| `BZ_RATE_LIMIT` | `1` | 开启 IP 限流 |
| `BZ_HSTS` | `0` | HTTPS 部署可设 `1` 加 HSTS 头 |
| `BZ_SECURE_COOKIE` | `0` | 公网 HTTPS 部署必须设 `1`；仅本地 HTTP 保持 `0` |
| `BZ_LOG_DIR` | `logs` | 日志目录 |
| `BZ_LOG_LEVEL` | `INFO` | 日志级别 |
