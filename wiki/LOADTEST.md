# 压测 / 大规模系统测试

`scripts/load_test.py` 是一个独立可跑的大规模系统压测脚本：批量创建用户、模拟真实行为、全面覆盖每一个角色（user / organizer / admin）与每一个端点。

## 用途

- **回归覆盖**：每次改完代码，跑一次确认全链路（鉴权/Bot/对局/观赛/人类对战/赛事/后台对局/Admin）正常。
- **稳定性验证**：持续打满并发对局（8 场），测对局编排、Bot 沙箱、DB 并发、Glicko 更新的稳定性。
- **端点覆盖**：见下方矩阵，三角色 × ~70 路由均有用例。

## 前置

- **dev 服务在线**：`bash scripts/platform-ctl.sh status`（默认 `127.0.0.1:50380`，主库 `botzone.db`）。
- **Docker 可用**：对局走 dev 服务现状的 Docker 沙箱跑 Bot 二进制。
- **样例 Bot 二进制存在**：`samples/{callbot,gomokubot,pencilbot}_linux_amd64`（仓库已带预编译 ELF）。

> 注意：本脚本**不**依赖 `BZ_TEST_CAPTCHA=1` 或 `BZ_BOT_LOCAL=1`。用户与 Bot 通过 **DB-direct 播种**绕过验证码/SMTP（避免给真实 SMTP 灌垃圾邮件），登录态用 **DB 直写 `sessions` 表**生成不透明 Bearer token——服务端从同一 `botzone.db` 读 sessions，Bearer 真正打通 `require_user/admin/organizer` 全链路。

## 运行

```bash
# 默认打 127.0.0.1:50380，主库 botzone.db，60 普通用户
python scripts/load_test.py

# 自定义
python scripts/load_test.py --base http://127.0.0.1:50380 --db botzone.db --users 30

# 跳过种子（假设已种过 load_* 账号，只跑 HTTP 阶段）
python scripts/load_test.py --skip-seed
```

**退出码**：`0` = 全部通过；`1` = 有失败；`2` = dev 服务不可达；`130` = 中断。

## 种子（不污染、幂等）

- **60 普通用户** `load_u01..load_u60`（密码固定 `LoadTest1234`，邮箱 `@loadtest.local`），每人上传 3 款游戏 Bot（`{user}_{game}`）→ 180 Bot。
- **2 组织者** `load_org1`/`load_org2`（各办 1 场赛事）。
- **admin**：复用现有 admin；若无则建 `load_admin`。
- 所有账号/Bot 名均 `load_` 前缀、邮箱 `@loadtest.local`，可一键识别清理；**不动既有非 load 数据**。
- seed **幂等**：已存在的用户/Bot 跳过，可重复跑。

清理 load 数据（如需）：

```bash
sqlite3 botzone.db "DELETE FROM ratings WHERE bot_id IN (SELECT id FROM bots WHERE name LIKE 'load_%');"
sqlite3 botzone.db "DELETE FROM bots WHERE name LIKE 'load_%';"
sqlite3 botzone.db "DELETE FROM users WHERE username LIKE 'load_%';"
```

## 阶段覆盖矩阵

| 阶段 | 覆盖端点 | 角色 |
|------|----------|------|
| **0 基础** | `GET /api/{health,wiki,leaderboard,contests,contests/templates,matches,users,auth/captcha}`；`GET /api/auth/me`；`POST /api/auth/change-password`（验旧 session 失效） | 公开 + user |
| **1 Bot** | `GET /api/bots/{mine,public,{id}}`；`POST /api/bots`（HTTP 上传）；`POST /api/bots/{id}/versions`；`POST /api/bots/{id}/active` | user |
| **2 对局** | `POST /api/matches/challenge`（三游戏混跑 + 自博弈，~80 场，8 并发）；`GET /api/matches`；`GET /api/matches/{id}`；`GET /api/leaderboard`（验 Glicko 更新） | user |
| **3 SSE** | `GET /api/matches/{id}/events`（验 snapshot 首事件 + 历史列表） | 公开 |
| **4 人类 vs Bot** | `POST /api/matches/human`；WS `/api/matches/{id}/play`（holdem/gomoku/pencil，收 `your_turn` 回着至 `match_end`）；验 per-user ≤1 并发被拒、match_type=human、**Glicko 不变** | user |
| **5 赛事** | `POST /api/contests`（template）；`/{id}/{open,register,dispatch,start,resume}`；轮询到 finished；验 standings/pairings/stage_results、contest 对局不更新 Glicko | organizer + user |
| **6 自动对局** | `GET/PATCH /api/admin/settings/runtime`（催化 auto-match：enabled/min_idle=0/interval=2/reserve=0）；验 daily_count/ladder 对局增长；恢复原设置 | admin |
| **7 Admin** | `GET /api/admin/{users,stats,bots,contests,email/templates,email/outbox,judges,templates,logs,settings/runtime}`；`PATCH /api/admin/{bots,users,matches,settings/runtime,judges/params,email/templates,contests}`；`POST/PUT/DELETE /api/admin/templates`；`POST /api/admin/users/{id}/role`；`DELETE /api/admin/users/{id}/sessions`（验 token 失效）；`GET /api/admin/{users,contests}/{id}/{sessions,entries}`；`POST /api/auth/admin/create-reset-token` | admin |

## 测试

`bzplat/backend/tests/test_load_test_seed.py` 是 `seed()` 的纯单测（不依赖运行服务）：

- 幂等：seed 跑两次用户/Bot 数不变
- token 是 sessions 表合法行（可 `get_session` 验证）
- 用户名/Bot 名均 `load_` 前缀
- 每个 bot 有 rating 行（Glicko 默认 1500）
- `_rebuild_ctx`（`--skip-seed`）能从已种 DB 重建一致上下文

```bash
pytest bzplat/backend/tests/test_load_test_seed.py -v
```

## 注意

- **holdem 加速**：阶段 2/3/5 用 `hands=8`（原 70 手约 140s/局），棋类单局。全量约 10-15 分钟。
- **并发硬顶** = `cpu//4`（本机 32 核 → 8 场）；admin 不可抬高（`max_concurrent_matches` 超 ceiling 报 400）。
- **挑战限流（重要）**：dev 服务按 IP 限流，`/api/matches/challenge` = **8 req/60s**（所有请求来自 127.0.0.1 共享额度）。阶段 2 按此节流（每 ~7.5s 发一个挑战），目标 80 场需约 10 分钟；阶段 2 结束后等一个完整限流窗口再进下一阶段，避免后续零星挑战被 429。`_paced_challenge` / `_paced_human` 遇 429 自动按 `Retry-After` 重试。
- **judge 参数**：阶段 7 临时改 gomoku size→13 跑 1 场后**改回** 15，验 bb≤sb 报错。
- **资源不调高**：不改 `bot_cpus/bot_memory`（只读硬顶）。
- **Docker 跑 Bot**：dev 服务现状即 Docker；若阶段 2 大量 EOF，脚本记 warning 不硬失败（属环境问题）。

## 历史发现

本压测脚本曾发现并修复一个真实 bug：`/api/matches/challenge` 不接受 `n_dots`，pencil 对局在 `n_dots=None` 时构造棋盘崩溃（全 aborted）。**当时**在 registry 路径对 pencil 做了默认 N 兜底；**当前**默认与校验由 `games/pencil` 的 `GameSpec`（`default_match_params` / `validate_match_params`）与引擎兜底承担，通用层无 `if game_id` 分支。回归测试 `test_board_engines.py::test_run_session_pencil_n_dots_none_uses_default`。
