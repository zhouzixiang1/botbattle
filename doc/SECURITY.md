# 安全与日志

公网暴露（nginx + frp 反代）后的日志体系、IP 透传、限流与安全响应头。

## 日志体系（三文件）

| 文件 | 内容 | 来源 |
|------|------|------|
| `logs/app.log` | 业务/系统日志（对局、bot、auto-match、异常） | 各模块 `logging.getLogger(__name__)` |
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
- 埋点：登录成功/失败、注册、验证邮箱、改密、重置密码、登出、Bot 上传/版本、对局创建、人类对战、赛事创建、admin 删用户/bot/赛事/赛事报名、赛事状态/时间字段修改、runtime 配置修改、改角色、建重置令牌。赛事与 runtime 管理写同时记录拒绝原因（`result=fail`），便于按 contest id 或 action 追溯。

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

`RateLimitMiddleware`（`security.py`）按 `{真实IP}:{路径}` 分桶：

| 路径 | 限制 |
|------|------|
| `/api/auth/{login,register,verify-email,reset-password,request-reset,resend-verify}` | 20 次/60s |
| `/api/auth/captcha` | 60 次/60s |
| `POST /api/bots`、`POST /api/bots/{id}/versions` | 6 次/60s |
| `/api/matches/challenge` | 8 次/60s |
| 其它 `/api/*` | 120 次/60s |
| 静态资源、`/api/health`、`/` | 不限 |

超限返回 `429` + `Retry-After`。单进程内存实现（多 worker 部署需换 Redis）。限流日志记 `rate limit: ip=... path=...`（WARNING）。

> 已知局限：WebSocket（`/api/matches/{id}/play`）与 SSE（`/api/matches/{id}/events`）不经 BaseHTTPMiddleware，当前无限流。

## 安全响应头

`SecurityHeadersMiddleware`（`security.py`）：`X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY`、`Referrer-Policy`、`Permissions-Policy`。HSTS 仅 `BZ_HSTS=1` 时加。

## 验证码脱敏

SMTP 未配置时，验证码日志脱敏（`code` 只记前 2 位 + `***`，完整验证码存 DB outbox 表可查），避免明文泄漏。

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
