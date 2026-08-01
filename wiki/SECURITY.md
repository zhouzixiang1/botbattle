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
- 埋点：登录成功/失败、注册、验证邮箱、改密、重置密码、登出、Bot 上传/版本、对局创建、人类对战、赛事创建、admin 删用户/bot/赛事、改角色、建重置令牌。

## IP 透传链路（公网必需）

```
浏览器(真实公网IP) → nginx(X-Real-IP + X-Forwarded-For) → frp(TCP透传,不改HTTP头)
→ 本机 uvicorn:50380 → 后端(client_ip 读 XFF) → 真实 IP
```

**关键开关 `BZ_TRUST_PROXY=1`**（公网经反代必需）：
- `.env` 已设。未开启时 `request.client.host` 是 `127.0.0.1`（frp/uvicorn 对端），限流全站共一个桶（失效）、登录 IP 记录错误。
- 开启后 `client_ip()`（`security.py`）按 `X-Forwarded-For` 取第一段（原始客户端），回退 `X-Real-IP`。
- **nginx 已设** `proxy_set_header X-Real-IP $remote_addr; proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;`；frp 是纯 TCP 代理透传 HTTP 头不改。
- 本地开发（无反代）保持 `BZ_TRUST_PROXY` 不设或 `0`。

> 安全提示：`X-Forwarded-For` 可被客户端伪造。当前取第一段（最左 = 原始客户端），适用于 nginx 是唯一可信入口的场景。若中间有多层不可信代理，需在 nginx 用 `real_ip` 模块校验。

## 限流（内存滑动窗口）

`RateLimitMiddleware`（`security.py`）按 `{真实IP}:{路径}` 分桶：

| 路径 | 限制 |
|------|------|
| `/api/auth/{login,register,verify-email,reset-password,request-reset,resend-verify}` | 20 次/60s |
| `/api/auth/captcha` | 60 次/60s |
| `/api/bots/upload`、`*/upload` | 6 次/60s |
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

`/api/admin/logs?file={app|access|audit}&level=&q=&limit=`（admin only），文件参数白名单防路径穿越。前端 admin「日志」Tab 可切换三文件 + 级别/关键字过滤（如按 IP 或 action 搜）。

## 相关环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `BZ_TRUST_PROXY` | `0` | 公网经反代设 `1`（信任 XFF 取真实 IP） |
| `BZ_RATE_LIMIT` | `1` | 开启 IP 限流 |
| `BZ_HSTS` | `0` | HTTPS 部署可设 `1` 加 HSTS 头 |
| `BZ_SECURE_COOKIE` | `0` | HTTPS 部署可设 `1` 使 cookie secure |
| `BZ_LOG_DIR` | `logs` | 日志目录 |
| `BZ_LOG_LEVEL` | `INFO` | 日志级别 |
