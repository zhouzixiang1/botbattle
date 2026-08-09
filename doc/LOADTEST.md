# 压测 / 大规模系统测试

`scripts/load_test.py` 是一个独立可跑的大规模系统压测脚本：批量创建用户、模拟真实行为，覆盖 user / organizer / admin 的主要业务端点。

## 用途

- **回归覆盖**：每次改完代码，跑一次确认全链路（鉴权/Bot/对局/观赛/人类对战/赛事/后台对局/Admin）正常。
- **多局稳定性验证**：顺序（或关闭客户端节流后快速连续）提交挑战，再用独立线程并行等待终态，检查对局编排、Bot 沙箱、DB 与 Glicko 结果；脚本不控制或证明“持续打满 8 并发”。
- **端点覆盖**：见下方矩阵；该脚本是主要业务链路压测，不声称覆盖每一个 API decorator。

## 前置

- **隔离 worktree 服务在线**：必须设置 `BZ_QA_INSTANCE=1`、显式锁定 worktree DB，并使用非 50380 端口；脚本会检查 `/api/health` 的 QA marker，拒绝 main 服务与主 checkout 写目标。
- **Bot 运行环境可用**：可使用 Docker，或在测试服务设置 `BZ_BOT_LOCAL=1` 本机运行样例 ELF。
- **样例 Bot 二进制存在**：`samples/{callbot,gomokubot,pencilbot}_linux_amd64`（仓库已带预编译 ELF）。

> 注意：本脚本**不**依赖 `BZ_TEST_CAPTCHA=1` 或 `BZ_BOT_LOCAL=1`。用户与 Bot 通过 **DB-direct 播种**绕过验证码/SMTP（避免给真实 SMTP 灌垃圾邮件），登录态用 **DB 直写 `sessions` 表**生成不透明 Bearer token——服务端从同一 `botzone.db` 读 sessions，Bearer 真正打通 `require_user/admin/organizer` 全链路。

## 运行

```bash
# 在 worktree 根启动隔离服务
export BZ_DB_PATH="$PWD/botzone.db"
BZ_QA_INSTANCE=1 BZ_BOT_LOCAL=1 \
  python -m bzplat.backend.cli serve --port 50381

# 另一终端：默认/相对 upload_root 均落到 <db.parent>/bot_uploads
python scripts/load_test.py \
  --base http://127.0.0.1:50381 --db "$BZ_DB_PATH" --users 60

# 跳过种子（假设已种过 load_* 账号，只跑 HTTP 阶段）
python scripts/load_test.py \
  --base http://127.0.0.1:50381 --db "$BZ_DB_PATH" --skip-seed
```

**退出码**：`0` = 全部通过；`1` = 用例失败或 QA 目标安全预检失败；`2` = 通过 QA marker 预检后的健康请求失败；`130` = 中断。

## 种子（不污染、幂等）

- **60 普通用户** `load_u01..load_u60`（密码固定 `LoadTest1234`，邮箱 `@loadtest.local`），每人上传 3 款游戏 Bot（`{user}_{game}`）→ 180 Bot。
- **2 组织者** `load_org1`/`load_org2`（各办 1 场赛事）。
- **admin**：仅创建/复用专用 `load_admin`；不会扫描、复用或修改 copied DB 中的
  `admin`/`adminroot` 等任意管理员。
- 所有账号/Bot 名均 `load_` 前缀、邮箱 `@loadtest.local`，可一键识别清理；**不动既有非 load 数据**。
- seed **幂等且 fail-closed**：已有账号只有在 namespace、精确用户名、邮箱、角色、
  固定密码全部匹配时才可复用并激活/验证；冲突会在任何用户/Bot/session 写入前失败。
  Bot 幂等性按当前样例 ELF 的 checksum、大小、平台元数据与磁盘内容判断：完全一致才复用，
  样例变化或文件漂移会为专用 QA Bot 发布并激活新版本。文件只写隔离 DB 旁的 uploads。
- `--skip-seed` 同样重新验证全部账号，并要求其已激活、已验证；验证完成前不会给任何
  用户（尤其是管理员）签发新 session。

`scripts/contest_stress.py` 使用相同契约：只使用 `cs_*@contest.local` 账号和专用
`cs_admin`，绝不借用隔离副本中原有的管理员。其默认 dry-run 只创建 draft 赛事、
批量指派名册并做赛制公式估算；不会 publish/start，不生成 pairings，也不验证真实排期
或吞吐。只有显式 `--run` 才启动并等待真实对局。

清理时不要用未启用 SQLite FK 的零散 `DELETE`（可留下对局/赛事/会话孤儿）。这是可丢弃 QA 副本，安全做法是停掉它的独立服务后删除副本与副本旁的运行时，然后从主库重新 `cp`。主 checkout 数据库始终只读。

## 阶段覆盖矩阵

| 阶段 | 覆盖端点 | 角色 |
|------|----------|------|
| **0 基础** | `GET /api/{health,wiki,leaderboard,contests,contests/templates,matches,users,auth/captcha}`；`GET /api/auth/me`；`POST /api/auth/change-password`（验旧 session 失效） | 公开 + user |
| **1 Bot** | `GET /api/bots/{mine,public,{id}}`；`POST /api/bots`（HTTP 上传）；`POST /api/bots/{id}/versions`；`POST /api/bots/{id}/active` | user |
| **2 对局** | `POST /api/matches/challenge`（三游戏混跑 + 自博弈，目标 `TARGET_MATCHES=12` 场；客户端顺序提交、线程并行等待终态）；`GET /api/matches`；`GET /api/matches/{id}`；`GET /api/leaderboard`（验 Glicko 更新） | user |
| **3 SSE snapshot** | `GET /api/matches/{id}/events`（只验首个非 ping 帧为 snapshot，且含 match + 历史列表；不覆盖后续实时增量） | 公开 |
| **4 人类 vs Bot** | `POST /api/matches/human`（固定座位 2）；WS `/api/matches/{id}/play`（holdem/gomoku/pencil，按 snapshot/move 维护已占位置，收 `your_turn` 只回合法未占动作直至 `match_end`，收到 `error` 即失败）；结束后再 GET 断言持久化 `status=completed`，并验 per-user ≤1、match_type=human、**Glicko 不变** | user |
| **5 赛事** | `POST /api/contests`（template）；`/{id}/{open,register,dispatch,start,resume}`；轮询到 finished；验 standings/pairings/stage_results、contest 对局不更新 Glicko | organizer + user |
| **6 代码配置边界** | `GET /api/admin/settings/runtime` 验 `source=code/mutable=false`；确认 runtime PATCH 与 admin template CRUD 均 404；公开模板列表标记代码只读 | admin + 公开 |
| **7 Admin** | `GET /api/admin/{users,stats,bots,contests,email/templates,email/outbox,logs,settings/runtime}`；`PATCH /api/admin/{bots,users,matches,email/templates,contests}`；`POST /api/admin/users/{id}/role`；`DELETE /api/admin/users/{id}/sessions`（验 token 失效）；`GET /api/admin/{users,contests}/{id}/{sessions,entries}`；`POST /api/auth/admin/create-reset-token` | admin |

## 测试

`bzplat/backend/tests/test_load_test_seed.py` 是 `seed()` 的纯单测（不依赖运行服务）：

- 幂等：样例内容不变时 seed 跑两次用户/Bot/版本数不变；样例或现有文件不一致时只新增并激活一个正确版本
- token 是 sessions 表合法行（可 `get_session` 验证）
- 用户名/Bot 名均 `load_` 前缀
- 每个 bot 有 rating 行（Glicko 默认 1500）
- `_rebuild_ctx`（`--skip-seed`）能从已种 DB 重建一致上下文
- 同名但邮箱/角色/密码不匹配时不改状态、不签发 session；任意既有 admin 不会被复用

```bash
pytest bzplat/backend/tests/test_load_test_seed.py -v
```

## 注意

- **固定规则**：holdem 始终跑 70 手且每手固定 20000 筹码、50/100 盲注，gomoku 固定 15×15，pencil 固定 N=6；请求中传规则字段不能改变规则。阶段 2 目标 `TARGET_MATCHES=12`（三游戏×4），需为真实 70 手对局预留足够时间。
- **并发硬顶** = `cpu//4`；代码默认并发为 2，实际取二者较小值。管理端、旧 settings 与环境变量均不可覆盖。
- **挑战限流（重要）**：dev 服务按 IP 限流，`/api/matches/challenge` = **8 req/60s**（所有请求来自 127.0.0.1 共享额度）。阶段 2 按此节流；`_paced_challenge` / `_paced_human` 遇 429 自动按 `Retry-After` 重试。
- **验收失败策略**：缺少 Python `websockets` 依赖会让阶段 4 失败；阶段 6 验证配置来源和写入口封闭，不通过临时改配置催化后台任务。auto-match 行为由 pytest 的注入配置测试覆盖。
- **资源不调高**：不改 `bot_cpus/bot_memory`（只读硬顶）。
- **Bot 运行失败不豁免**：可由隔离服务选择 Docker 或 `BZ_BOT_LOCAL=1`；阶段 2 要求三游戏各有 completed，且 completed 多于 aborted，不会把大量 EOF/aborted 只记 warning 后冒充通过。

## 固定规则回归

Pencil 规则已钉死为 N=6：`games/pencil` 的 `GameSpec.validate_match_params` 只接受空对象，Session 始终使用 `DEFAULT_N=6`；直接入口传 `n_dots`（包括 `None`）会明确抛错，不能静默忽略。通用层无 `if game_id` 分支。回归测试 `test_board_engines.py::test_run_session_pencil_rejects_removed_rule_params`。
