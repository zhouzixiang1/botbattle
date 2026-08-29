# 压测 / 大规模系统测试

`scripts/load_test.py` 是大规模系统压测入口：批量创建用户、模拟真实行为，目标覆盖 user / organizer / admin 的主要业务端点。

> **当前证据边界**：脚本的挑战、人机和依赖建局阶段已改为校验 HTTP 202 持久 request，并按
> opaque `public_id` 轮询到 claim 产生 `match_id`；容量等待由服务端队列承接，不再把 admission
> 429 当补槽协议。execution-request、match detail 与赛事状态轮询共享线程安全的 1 秒最小节拍，
> 每次都校验 HTTP/JSON，并在真实 429 时按 `Retry-After` 统一退避；不会靠关闭服务端限流绕过边界。
> 阶段 2 的 challenge POST 同样复用限流助手：只对明确的 429 用原 payload 最多尝试 3 次，
> 首个非 429（包括已接受的 202）立即返回，绝不重复提交已接受 request；重试耗尽、POST
> 异常或任一已接受 request 未在时限内取得终态，都是硬失败，不会被“大多数完成”掩盖。
> 阶段 1 的 HTTP 上传 Bot 在验证版本、启停和 owner 删除后保持软删除；阶段 2 自博弈只用 seed
> 上下文中已验证活跃的正式 Bot 对自身，不会从 DB 拾取或重新启用该临时 Bot。
> 顺序单局等待上限为 360 秒；claim 后若与另一场共享 Docker launch fence，则单局上限为
> 720 秒。阶段 2 固定 12 场（4 场 Holdem + 8 场棋类）使用一个 2880 秒绝对截止，计算为
> `4×360 + 8×180`，claim 与完成共同消耗该预算，不会为每个阶段重复叠加等待。
> 阶段 5 不再把内置 8 人瑞士→单败模板的约 19 场长赛程冒充上线必要条件：改用一届
> 4 人五子棋自定义赛事（1 轮瑞士 2 场 → 休息/重新派遣/人工恢复 → Top2 单败 1 场），
> 严格校验拓扑 3 场、600 秒共享绝对截止、两个阶段读模型、连续 1-based 正式榜、赛事
> 不计 Glicko，并逐步验证 open→published→running→rest→running→finished。阶段 7 完成后
> 还会再次要求 dispatcher 为 running/accepting、active/queued 与两类占用均为 0。
> 它证明完整状态机；12 场真实吞吐由阶段 2 独立证明。
> 该脚本也不单独覆盖 request 取消/重试或证明双资源
> 峰值，因此历史退出码/通过数不能作为当前发布证据。

## 用途

- **回归覆盖**：每次改完代码，跑一次确认全链路（鉴权/Bot/对局/观赛/人类对战/赛事/后台对局/Admin）正常。
- **多局稳定性验证**：顺序（或关闭客户端节流后快速连续）提交挑战，再用独立线程并行等待终态，检查对局编排、Bot 沙箱、DB 与 Glicko 结果；脚本不控制或证明“持续打满 8 并发”。
- **端点覆盖**：见下方矩阵；该脚本是主要业务链路压测，不声称覆盖每一个 API decorator。

## 前置

- **隔离 worktree 服务在线**：必须设置 `BZ_QA_INSTANCE=1`、显式锁定 worktree DB，并使用非 50380 端口；脚本会检查 `/api/health` 的 QA marker，拒绝 main 服务与主 checkout 写目标。
- **Bot 运行环境可用**：可使用 Docker，或在测试服务设置 `BZ_BOT_LOCAL=1` 本机运行样例 ELF。
- **样例 Bot 二进制存在**：`samples/{callbot,gomokubot,pencilbot}_linux_amd64`（仓库已带预编译 ELF）。

> 注意：本脚本**不**依赖 `BZ_TEST_CAPTCHA=1` 或 `BZ_BOT_LOCAL=1`。用户与 Bot 通过 **DB-direct 播种**绕过验证码/SMTP（避免给真实 SMTP 灌垃圾邮件），登录态用 **DB 直写 `sessions` 表**生成不透明 session token——REST 用 Bearer，人机 WebSocket 用 `bz_session` Cookie，服务端均从同一 `botzone.db` 验证。

## 运行

```bash
# 在 worktree 根启动隔离服务
export BZ_DB_PATH="$PWD/botzone.db"
BZ_INSTANCE_KEY=qa-loadtest BZ_QA_INSTANCE=1 BZ_BOT_LOCAL=1 \
  BZ_PUBLIC_ORIGIN=http://127.0.0.1:50381 \
  python -m bzplat.backend.cli serve --port 50381

# 另一终端：默认/相对 upload_root 均落到 <db.parent>/bot_uploads
python scripts/load_test.py \
  --base http://127.0.0.1:50381 --db "$BZ_DB_PATH" --users 60

# 跳过种子（假设已种过 load_* 账号，只跑 HTTP 阶段）
python scripts/load_test.py \
  --base http://127.0.0.1:50381 --db "$BZ_DB_PATH" --skip-seed

# 干净新副本：先创建/校验专用 seed，再只跑赛事到 Admin 的 5–7
python scripts/load_test.py \
  --base http://127.0.0.1:50381 --db "$BZ_DB_PATH" \
  --users 60 --start-phase 5

# 复用既有 seed：只有 clean gate 证明队列/容量为 0、dispatcher 健康，且无活动
# LoadTest 赛事时才继续；否则脚本停止，不会替操作者自动取消或删除现场
python scripts/load_test.py \
  --base http://127.0.0.1:50381 --db "$BZ_DB_PATH" \
  --skip-seed --start-phase 5
```

**退出码**：`0` = 全部通过；`1` = 用例失败或 QA 目标安全预检失败；`2` = 通过 QA marker 预检后的健康请求失败；`130` = 中断。

## 种子（不污染、幂等）

- **60 普通用户** `load_u01..load_u60`（密码固定 `LoadTest1234`，邮箱 `@loadtest.local`），每人上传 3 款游戏 Bot（`{user}_{game}`）→ 180 Bot。
- **2 组织者** `load_org1`/`load_org2`（覆盖角色与权限；阶段 5 由 `load_org1` 主办一届有界赛事）。
- **admin**：仅创建/复用专用 `load_admin`；不会扫描、复用或修改 copied DB 中的
  `admin`/`adminroot` 等任意管理员。
- 所有账号/Bot 名均 `load_` 前缀、邮箱 `@loadtest.local`，可一键识别清理；**不动既有非 load 数据**。
- seed **幂等且 fail-closed**：已有账号只有在 namespace、精确用户名、邮箱、角色、
  固定密码全部匹配时才可复用并激活/验证；冲突会在任何用户/Bot/session 写入前失败。
  Bot 幂等性还要求当前版本精确位于本实例
  `upload_root/<bot_id>/vN/bot.bin`、具备执行位，且 `bots` 镜像与版本元数据一致；只有
  checksum、大小、平台元数据、磁盘内容和上述归属全部一致才复用。样例变化、复制库外部路径、
  权限或镜像漂移都会在 per-Bot 锁内发布并激活当前隔离目录的新版本。
- `--skip-seed` 同样重新验证全部账号，并要求其已激活、已验证；三款正式 Bot 必须齐全且保持活跃。
  验证完成前不会给任何用户（尤其是管理员）签发新 session。

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
| **2 对局** | `POST /api/matches/challenge` 精确接收 202 request；按 `public_id` 查询直到 claim 出现 `match_id`，三游戏混跑 + 自博弈，目标 `TARGET_MATCHES=12`；并行等待终态后 GET Match/排行榜核对 Glicko。不再期待 admission 429，也不据此声称测得服务端峰值并发 | user |
| **3 SSE snapshot** | `GET /api/matches/{id}/events`（只验首个非 ping 帧为 snapshot，且含 match + 历史列表；不覆盖后续实时增量） | 公开 |
| **4 人类 vs Bot** | `POST /api/matches/human` 接收 202 request（固定展示座位 2），轮询取得 `match_id` 后才连接 WS `/api/matches/{id}/play`；结束后断言 completed、per-user 活跃 ≤1、match_type=human、**Glicko 不变**。共享 match slot + 1 sandbox unit 由 execution queue 单测与浏览器队列验收另行证明 | user |
| **5 赛事** | `POST /api/contests`（4 人五子棋自定义 Swiss1→rest→Top2 KO，精确 3 场）；`/{id}/{open,register,dispatch,publish,start,resume}`；硬断言 published 后才 start，轮询到 finished；验服务端 estimate、两个阶段、全部 pairing/Match、连续 1-based 正式榜、contest 不更新 Glicko | organizer + user |
| **6 代码配置边界** | `GET /api/admin/settings/runtime` 验 `source=code/mutable=false`；确认 runtime PATCH 与旧 admin template POST 写入口均 404；公开模板列表标记代码只读 | admin + 公开 |
| **7 Admin** | `GET /api/admin/{users,stats,bots,contests,email/templates,email/outbox,logs,settings/runtime}`；`PATCH /api/admin/{bots,users,matches}` 与 `PATCH /api/admin/settings/site`；`GET /api/admin/bots/{id}/versions`；`POST /api/admin/users/{id}/role`；`DELETE` 后再 `GET /api/admin/users/{id}/sessions`（验 token 失效）；`GET /api/admin/contests/{id}/entries`；`PUT /api/admin/email/templates/welcome` 断言代码模板以 409 拒写；旧 `POST /api/auth/admin/create-reset-token` 严格断言 404，确认管理员不能取得重置 credential 且不会触发邮件 | admin |

## 测试

`bzplat/backend/tests/test_load_test_seed.py` 是 `seed()` 的纯单测（不依赖运行服务）：

- 幂等：同一隔离 upload root 且路径/权限/元数据/镜像/内容全部一致时重复 seed 不增版本；跨 root、无执行位、样例或 bots 镜像漂移时只新增并激活一个当前实例的正确版本
- token 是 sessions 表合法行（可 `get_session` 验证）
- 用户名/Bot 名均 `load_` 前缀
- 每个 bot 有 rating 行（Glicko 默认 1500）
- `_rebuild_ctx`（`--skip-seed`）能从已种 DB 重建一致上下文
- 同名但邮箱/角色/密码不匹配时不改状态、不签发 session；任意既有 admin 不会被复用

`bzplat/backend/tests/test_qa_script_artifacts.py` 还用假响应和零真实等待验证阶段 2 challenge：
429 严格按 `Retry-After` 退避、重试次数有硬上限、复用同一 payload，且 202 后不再 POST；
另外守护 429 耗尽和 waiter 超时均使整轮退出非零，以及阶段 1 软删除 extra Bot 后固定
12 场仍只使用活跃正式 Bot；赛事 QA 的自定义阶段拓扑固定 3 场、绝对截止固定 600 秒，
且 `--start-phase 5` 只选择 5/6/7 三阶段。`--start-phase` 是阶段后缀选择器，不是
阶段内部 checkpoint：复用状态时会在任何 seed/session 写入前执行只读 clean gate；发现
暂停调度器、活跃/排队任务、资源占用或 open/published/running/rest 的 `LoadTest %` 赛事即拒绝。

```bash
pytest bzplat/backend/tests/test_load_test_seed.py \
  bzplat/backend/tests/test_qa_script_artifacts.py -v
```

## 注意

- **固定规则**：holdem 每个计分场始终跑 70 手且每手固定 20000 筹码、50/100 盲注；普通记录运行一场，复式记录正常运行两场。gomoku 固定 15×15，pencil 固定 N=6；请求中传规则字段不能改变规则。阶段 2 目标 `TARGET_MATCHES=12`（三游戏×4），需按实际普通/复式运行成本预留足够时间。
- **双资源硬顶**：`max_match_slots=6`，`max_sandbox_units=12`；每个 job 占 1 slot，平台 Bot-vs-Bot 占 2 units，人机占 1，本地 Bot/真人座位占 0。job 的 CPU/内存/unit 向量在入队时冻结，claim 再受 affinity/cgroup/物理预算约束，所以实际并发依组合为 1–6；例如 8 CPU 预算只容纳 2 场赛事或 4 场双低配任务，足够资源的真人/本地组合才可用满六槽。显式启动值、管理端、旧 settings 与环境变量均不可把 6/12 代码硬顶抬高；赛事份额 1 只是混排门禁，不增加物理槽。
- **公平与身份门禁**：容量压测须用不同 Bot 填槽；同一非 human Bot 跨两个 `starting/running/settling` job 必须保持后一条 queued。多个 contest 的可运行 job 只在不跨 manual/human 行的连续 contest 队列段内按持久 claim 历史轮转，测试必须在重启前后分别证明 A/B 赛事交替，并用 `contest A → manual/human → contest B` 反例证明不会跨前台排序边界。
- **大赛程与估算门禁**：全员及分组循环均无人数硬上限，压测先在隔离副本 dry-run 核对精确基础对局/job、计分 session、Swiss 有效轮数与基础 ETA，再决定是否真实运行。17 人 Gomoku 双循环基线为 272 个 pairing/job；新 Holdem/Gomoku KO 的两场换座决胜组无次数上限，必须单独断言基础估算排除加赛并返回 unbounded 风险。Pencil 单场 ETA 固定按双方各 900 秒合计 1800 秒保守估算。
- **挑战限流（重要）**：dev 服务按 IP 限流，`/api/matches/challenge` = **8 req/60s**（所有请求来自 127.0.0.1 共享额度）。这里的 429 只表示 HTTP 限流；执行容量不足应返回/保持 202 queued。阶段 0 与阶段 2 统一只对精确 429 按 `Retry-After` 有界重试；首个 202 即停止，避免重复已接受请求。
- **验收失败策略**：缺少 Python `websockets` 依赖，或服务端 `BZ_PUBLIC_ORIGIN` 与 `--base` 不一致，都会让阶段 4 失败；阶段 6 验证配置来源和写入口封闭，不通过临时改配置催化后台任务。自动 producer/唯一开关/混合来源容量由 `test_execution_queue.py` 与 `test_runtime_settings.py` 覆盖。
- **资源不调高**：不改 `bot_cpus/bot_memory`（只读硬顶）。
- **Bot 运行失败不豁免**：可由隔离服务选择 Docker 或 `BZ_BOT_LOCAL=1`；阶段 2 要求三游戏各有 completed，且 completed 多于 aborted，不会把大量 EOF/aborted 只记 warning 后冒充通过。

## 固定规则回归

Pencil 规则已钉死为 N=6：`games/pencil` 的 `GameSpec.validate_match_params` 只接受空对象，Session 始终使用 `DEFAULT_N=6`；直接入口传 `n_dots`（包括 `None`）会明确抛错，不能静默忽略。通用层无 `if game_id` 分支。回归测试 `test_board_engines.py::test_run_session_pencil_rejects_removed_rule_params`。
