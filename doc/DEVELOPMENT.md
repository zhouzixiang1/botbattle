# 开发文档

> 本文档说明如何搭建开发环境、构建运行、遵守编码规范、扩展模块与部署运维。

## 1. 环境搭建

### 1.1 前置依赖
| 依赖 | 版本 | 用途 |
|------|------|------|
| Python | ≥ 3.12 | 后端 |
| Node.js | ≥ 22 | 前端构建 |
| Docker | 最新 | Bot 沙箱（必需；`BZ_BOT_LOCAL=1` 可退回本机仅测试用） |

### 1.2 后端安装
```bash
cd /home/zzx/project/botbattle
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'          # 装 bzplat 包 + pytest/httpx
```

### 1.3 前端安装
```bash
cd bzplat/frontend && npm install
```

### 1.4 配置文件
关键配置在 `.env`（**勿提交到版本库，含敏感凭据**）：

| 变量 | 说明 | 默认 |
|------|------|------|
| `BZ_HOST` / `BZ_PORT` | 绑定地址/端口 | 127.0.0.1 / 50380 |
| `BZ_DB_PATH` | SQLite 路径 | botzone.db |
| `BZ_BOT_LOCAL` | 强制本机跑 ELF（测试） | 未设 |
| `BZ_QA_INSTANCE` | 标记隔离 QA 实例；启用时启动前拒绝主 checkout/50380 写目标 | 未设 |
| `BZ_API_TARGET` | Vite REST/SSE/WS 代理目标；50380 被硬拒绝 | 127.0.0.1:50381 |
| `BZ_AVATAR_DIR` | 头像目录 | avatars |
| `BZ_RATE_LIMIT` | 启用限流 | 1 |
| `BZ_TRUST_PROXY` | 信任 X-Forwarded-For（反向代理部署时需开启，否则限流按代理 IP 失效） | 未设 |
| `BZ_LOG_LEVEL` / `BZ_LOG_DIR` | 日志级别 / 目录 | INFO / logs |
| `SMTP_HOST/PORT/USER/PASSWORD/FROM` | SMTP（邮箱验证/重置/通知） | 未配则注册/重置返回 503 |
| `SMTP_FROM_NAME` | 邮件显示的发件人名称 | Botbattle |
| `EMAIL_CODE_TTL_MINUTES` | 验证码 TTL | 30 |

> ⚠️ **敏感信息警示**：`.env` 含 SMTP 明文密码，**绝不提交**。`.gitignore` 应排除 `.env`。文档中不回写真实凭据。

邮件模块只提供一套 Botbattle 多游戏平台默认文案：邮箱验证、密码重置和验证完成欢迎信。
新库通过 `INSERT OR IGNORE` 播种这三条模板，因此管理后台已经保存的自定义模板不会在重启时
被覆盖；历史库如需恢复官方文案，必须先备份，再对这三个精确 key 做受控数据更新。

## 2. 构建与运行

### 2.1 起服务
```bash
scripts/platform-ctl.sh start     # 或：botzone serve
# 默认 127.0.0.1:50380
botzone create-admin <user> <email> '<pass>'   # 建管理员（跳过邮箱验证）
```

### 2.2 构建前端
```bash
cd bzplat/frontend && npm run build   # 产物 dist/，由后端 StaticFiles 托管
```
> **关键前端依赖**：react 19 / vite 8 / tailwindcss v4 / shadcn(new-york) / recharts。
> 视觉层另用 `gsap ^3.x`（npm 安装，2025-04 起 100% 免费商用，驱动 canvas 牌桌动画）+
> Poker.JS（vendor 副本，来源 Tairraos/Poker.JS，用于 canvas 矢量扑克牌绘制）。

### 2.3 构建三游戏样例 Bot

仓库样例只面向平台开发、回归与发布验收。统一脚本构建 Holdem、Gomoku、Pencil 的
Linux x86_64 ELF，并检查产物类型：

```bash
bash samples/build_sample.sh
file samples/{callbot,gomokubot,pencilbot}_linux_amd64
sha256sum samples/gomoku_showcase/*_linux_amd64
```

脚本还会从 `samples/gomoku_showcase/gomoku_showcase_bot.c` 构建赛事演示专用的
`tactical/steady/foundation` 三档 LongRunning ELF。三档不读时钟、不用随机数，checksum
由演示 seed manifest 锁定；它们是合成的强/中/弱矩阵，不是自然形成的 12 种独立棋力。

玩家侧跨系统构建说明不依赖仓库脚本，见 `wiki/BOT_DEV.md`。

### 2.4 改完代码必须 rebuild + restart
```bash
bash scripts/rebuild.sh   # npm run build → platform-ctl.sh restart
```
> 前端产物（`dist`）由后端 StaticFiles 托管、后端代码由运行进程加载——**不 rebuild + restart 代码不生效**（常见症状：新路由 405）。

### 2.5 worktree 隔离开发（勿碰线上 50380）

主目录 `main` 只跑线上服务（默认 `:50380` + 主库）。特性开发在 **git worktree** 中跑**独立**栈，避免污染线上 db/源码：

```bash
# 1) 主库只读复制到 worktree（必须 cp，不得软链接）
cp /home/zzx/project/botbattle/botzone.db .worktrees/<分支名>/botzone.db

# 2) 终端 A：后端（CWD=worktree，显式锁定副本并声明 QA）
cd .worktrees/<分支名>
BZ_DB_PATH="$PWD/botzone.db" BZ_QA_INSTANCE=1 BZ_BOT_LOCAL=1 BZ_SKIP_CAPTCHA=1 \
  python -m bzplat.backend.cli serve --host 127.0.0.1 --port 50381

# 3) 终端 B：播种三类角色的隔离账号，然后启前端
cd .worktrees/<分支名>
python scripts/seed_test_accounts.py --db "$PWD/botzone.db" --with-role-accounts
cd bzplat/frontend
BZ_API_TARGET=http://127.0.0.1:50381 npm run dev

# 4) 终端 C：首次安装 Chromium，再跑真浏览器回归
cd .worktrees/<分支名>/bzplat/frontend
npm run test:e2e:install
BZ_E2E_BASE_URL=http://127.0.0.1:5173 npm run test:e2e
```

- **严禁**前端 `BZ_API_TARGET` 指向 50380（测试写入线上 db）。
- **严禁**在主目录 CWD 起 worktree 后端（会加载主源码 + 主库）。
- QA CLI 会在日志 handler、SQLite、上传/头像目录创建前一次性校验端口和全部写目标；拒绝 50380、主 checkout 内任意 DB/运行时路径，以及主 `bot_uploads`/`avatars`/`logs` 的别名或子目录。当前 linked worktree 与 `/tmp` 独立目录仍允许。
- QA CLI 未显式设置目录时，`bot_uploads`、`avatars`、`logs` 均由 `BZ_DB_PATH` 的父目录派生；显式相对路径按服务 CWD 解析并在写入前钉为绝对路径。`/api/health` 只返回 `qa_instance` 标记，不公开服务器绝对路径。
- `BZ_QA_INSTANCE=1` 还会选择代码固定的 `QA_AUTO_MATCH_CONFIG(enabled=False)`，使浏览器/API 验收不受后台 ladder 抢占临时 Bot 的竞态影响；它不是可调运行参数，生产 `AUTO_MATCH_CONFIG`、并发与资源限制均不变。管理端只读诊断返回当前实例实际生效的 profile。
- 合并走 GitHub PR；详见根目录 [`AGENTS.md`](../AGENTS.md)「worktree 隔离工作流」。

## 3. 编码规范

| 规范 | 要求 |
|------|------|
| **Python 包名** | 必须是 `bzplat`，**绝不能叫 `platform`**（会遮蔽标准库 `platform`）。所有 import 用绝对路径 `from bzplat.backend...` |
| **常量集中** | 所有状态码/对局类型/`REGISTERED_ENGINES`/`VALID_GAME_IDS`/平台 settings 键名集中在 `store/schema.py`，别散落 |
| **日志** | 后端**禁止 `print()`**，统一 `logging.getLogger(__name__)` |
| **游戏解耦** | 通用层（matches/contests/store/api_routes）**禁止 `if game_id == ...` 分支**；经 `games.registry.get(game_id)` 取 `GameSpec`；持久化实体缺失/未知 game_id 必须失败，不能猜默认游戏 |
| **资源硬顶** | 每 Bot `--cpus=1` / `--memory=512m`，半负载并发 ceiling=`max(1,cpu//4)`，全员循环 `FULL_RR_MAX_N=12`；admin 不可抬高（`runtime/limits.py`） |
| **运行参数** | `runtime/config.py` 是 action timeout、默认并发、auto-match、赛事 scheduler 等参数的代码唯一来源；修改后须评审、测试并重新发布。`BZ_MAX_CONCURRENT_MATCHES` 与 admin runtime PATCH 均不支持 |
| **前端图标** | 统一 lucide-react（**无 emoji**），按需导入 |
| **前端颜色** | 用语义 token（`bg-background`/`text-primary`），不裸 hex、不硬编码 slate/brand 颜色 |
| **前端组件** | 用 `@/components/ui/*` 共享原语，禁内联重复样式 |
| **路径别名** | 前端用 `@/` → `src/`，禁相对路径 |

## 4. Git 工作流

遵循 [`AGENTS.md`](../AGENTS.md)（权威）：
1. **分支工作流**：任何修改先从 `main` 切特性分支（`feat/...` 或 `fix/...`），在分支上完成。
2. **合并必须走 GitHub Pull Request**（`gh pr create` → 评审 → 合并）；**禁止**本地 `git merge` 直推 `main`，禁止直接在 `main` 提交。
3. **PR 合并后删除原分支**（本地 + 远端）。
4. **提交前跑测试**：`pytest`（仓库根）；前端改了再 `npm run build`。
5. **改动须同步三处**：① 测试（`bzplat/backend/tests/`）② 文档（`wiki/` 或 `doc/`，见 `AGENTS.md` 文档规范）③ 非显而易见的架构约定写入会话记忆（若环境提供 memory 目录）。
6. **多 agent 协作**：不同任务用独立分支/独立 agent 隔离，每个 agent 只对自己的分支负责。

## 5. 模块扩展指南

### 5.1 新增一款游戏（赛制/编排主流程不加游戏名分支）

通用层**不得**再加 `if game_id == ...` 分支。权威 checklist 与 [`AGENTS.md`](../AGENTS.md) / [`DESIGN.md`](./DESIGN.md) §2.3 一致：

1. 建 `games/<game>/` 子包：
   - `<game>_judge.py`（纯游戏规则，零平台依赖）
   - `engine.py`（裁判↔平台协议适配，提供 Session 并驱动纯裁判，`run_async(decide) → MatchResult`）
   - `protocol.py`（`dumps_request` / `loads_response` / `validate_response_payload` / `fail_response`；只导出本游戏 API；复用 `games/_board_protocol.py` 时在 spec 的 `shared_source_files` 声明公开源码）
   - `result.py`（**独立**定义，满足鸭子契约：`winners` + `deltas`，**不**共享基类）
   - `tiers.py`（段位曲线，查表用 `base.tier_for_in`）
   - `templates.py`（本游戏内置赛事模板）
   - `spec.py`（装配 `GameSpec`，声明 `normalize_delta` 与 `progress_from_events`）
2. `store/schema.py` 的 `REGISTERED_ENGINES` / `VALID_GAME_IDS` frozenset 各加一项；`Store._migrate()` 根据注册 ID 用同构模板创建 `matches_<game>` 表及索引，不复制静态 DDL。
3. `games/__init__.py`：`registry.register(SPEC)` 一行（启动时断言 schema 与注册表 ID 集合一致）。
4. 前端：`src/games/<game>/index.ts`（`GameViewSpec`：Board/kind/reduce/`CanvasRenderer`）+ 在 `src/games/index.ts` 注册；规则参数固定后已无 `configFields`。
5. **禁止反向依赖**：`games/<game>/` 不得反向 import 通用层；通用层不得 import 具体游戏模块（`test_import_cycles.py` 源码扫描守护）。
6. 跑测试：`pytest`（含 `test_result_contract` / `test_import_cycles` / `test_game_registry` / `test_tongyong_layer_no_game_branches`）+ `npm run build`。

> 新代码直接面向 `games` 注册表，不要在 `matches/runner.py` 加游戏分支。`run_session` 的 kwargs 仅供内部复现控制，新增或拼错规则键必须显式失败。

### 5.2 新增 API 端点
- 在 `api_routes.py`（或 `auth/routes.py`）加路由，按需用 `require_user`/`require_admin`/`require_organizer` 依赖。
- 常量（新状态码/类型）加到 `schema.py`。
- **路由顺序注意**：字面量路由（如 `/api/matches/liked-top`）必须在参数路由（`/api/matches/{match_id}`）之前注册。

## 6. 部署与运维

### 6.1 systemd 部署
`deploy/botzone-platform.service` 提供 systemd unit 模板。

### 6.2 日志（三文件 + 启动日志）
- `logs/app.log`：业务/系统日志（`logging_config.setup_logging`，格式 `时间 级别 [模块] 消息`）。排查对局/Bot 崩溃/auto-match/WS 在此；Bot EOF 附 stderr 末尾。
- `logs/access.log`：HTTP 访问日志（真实 IP + 方法 + 路径 + 状态 + 耗时）。
- `logs/audit.log`：安全审计（登录/注册/改密/上传/管理操作等）。
- `logs/web.log`：uvicorn 启动 stdout。
- **admin「日志」Tab**：`GET /api/admin/logs?file={app|access|audit}`（文件参数白名单）。详见 [SECURITY.md](./SECURITY.md)。

### 6.3 测试种子账号
```bash
# 只允许隔离 DB；上传目录默认跟随 DB 到 <db.parent>/bot_uploads
python scripts/seed_test_accounts.py \
  --db "$PWD/botzone.db" --with-role-accounts
```

默认建立 `tester1/tester2` 及三游戏样例 Bot；`--with-role-accounts` 才显式建立
`qa_organizer/qa_admin`。所有固定凭据账号都按脚本 namespace、精确用户名、邮箱、
角色和密码校验；任一项不匹配即在激活、验证、提权或上传 Bot 前 fail-closed，绝不
改写未知同名账号。专用 QA Bot 只有在当前实例
`upload_root/<bot_id>/vN/bot.bin` 的精确规范路径、普通文件/执行位、Linux x86_64 ELF
元数据、checksum/大小/磁盘内容及 `bots` 当前镜像全部一致时才复用。任一项漂移（包括复制库
仍指向主 checkout）都在同一 per-Bot 锁内向当前隔离目录发布并激活新版本，绝不跨运行时执行文件。

### 6.4 长期客户演示快照

`seed_contest_showcase.py` 维护六个明确标注的合成只读赛事。数据库路径必须是已存在的绝对路径；
默认 Bot 目录是同目录的 `bot_uploads_showcase/`，与普通上传隔离。目录 basename 固定且必须含
seed 创建的 namespace marker；只允许数据库声明的 `<bot_id>/vN/bot.bin`，任何额外文件、符号链接、
普通 `bot_uploads/` 子树、仓库根、数据库父目录或 home/root 目标都会 fail-closed。开发验收先在
worktree 副本执行：

```bash
python scripts/seed_contest_showcase.py seed \
  --db "/abs/worktree/botzone.db" --yes
python scripts/seed_contest_showcase.py seed \
  --db "/abs/worktree/botzone.db" --yes       # 第二次必须全量跳过
python scripts/seed_contest_showcase.py verify \
  --db "/abs/worktree/botzone.db"
```

seed 默认从仓库 `samples/gomoku_showcase/` 读取 checksum 锁定的三档 ELF；如部署包将其放在
其他位置，只能用绝对 `--profile-dir` 指向同一组已审核产物。01–04 固定为 tactical、05–08
为 steady、09–12 为 foundation，蛇形分组使每组各一档。策略 manifest 版本变化时，已有 partial
图不会原地换版本；命令会 fail-closed 并要求先 rollback 后重新 seed，避免冻结 pairing 混用策略。

预期清单固定为：draft 4 人；open 6 人；published-manual 12 人、24 个 pending pairing、
`starts_at=NULL`、0 Match；running 12 人、真实 completed 与未绑定 pending 并存、0 active；
rest 24 场真实小组赛；finished 24 场分组双循环 + 7 场 Top 8 淘汰。完整集合共 59 个互不复用的
真实 Match，所有回放经 canonical LongRunning Linux ELF、正式 Manager/Orchestrator/GameSpec 裁判生成。
验收逐场要求 `technical_loss=0`、原因仅 `five/draw`、无故障事件，且回放只有一个与数据库胜者/原因
及结果分差一致的末尾 canonical `match_end`。rest 与 finished 的四组各自固定形成 8/4/0 分；
running/rest/finished 中同一有序 Bot 对的归一落子轨迹必须完全一致，finished 的 7 场淘汰赛必须
全部产生胜者。六个 key 已完整时二次 seed 先严格验收并跳过 provisioning；12 个
专用 Bot 最终全部 inactive（历史详情仍可按 ID 查看），不会进入五子棋榜单或自动匹配。

部署到主库属于显式运维写操作，只能在代码已评审、主库已备份且 50380 已停服后执行；独立 seed
进程不能与线上 orchestrator 叠加并发：

```bash
bash scripts/platform-ctl.sh stop
cp /home/zzx/project/botbattle/botzone.db \
  /abs/approved-backup/botzone-before-showcase.db
python scripts/seed_contest_showcase.py seed \
  --db /home/zzx/project/botbattle/botzone.db \
  --allow-primary --primary-service-stopped --yes
python scripts/seed_contest_showcase.py verify \
  --db /home/zzx/project/botbattle/botzone.db \
  --allow-primary --primary-service-stopped
bash scripts/platform-ctl.sh start
```

seed 中断后只恢复专用 marker 赛事，不调用全平台 orphan/reconcile；已绑定的 pending/running 中断局
会先精确解绑并删除 match/index/replay，随后只经正常排期闸门重派，未来 `scheduled_at` 不会提前启动；
非演示活动赛事不会被接管。管理员统计和最近趋势会排除快照关联的 6 赛事、59 对局、13 用户与 12 Bot。
回滚必须同样停服、先备份，再执行下列白名单命令。它会在删除前整体核对 6 个 key/marker、专用
账号邮箱与角色、每个 Bot 的版本路径、全游戏对局归属及目录白名单，并先冻结精确删除计划再开始
写操作。rollback scope 故意不调用展示质量门禁：坏积分、缺回放、缺少预期二进制、partial key 或
Bot 的 active 标志不会阻塞恢复；但任何 active Match、额外文件/目录、符号链接、外部赛事/对局引用
或非 canonical 路径仍会拒绝，避免误删真实数据：

```bash
python scripts/seed_contest_showcase.py rollback \
  --db /home/zzx/project/botbattle/botzone.db \
  --allow-primary --primary-service-stopped --yes
```

确认展示验收和备份保留策略后再清理旧备份；seed/rollback 都不会处理历史 0808/0809 赛事。

### 6.5 关键脚本
| 脚本 | 用途 |
|------|------|
| `scripts/platform-ctl.sh` | 启停：start/stop/restart/status/logs |
| `scripts/rebuild.sh` | npm build + restart |
| `scripts/e2e_smoke.sh` | 端到端冒烟（`mktemp` 独立 DB/uploads/avatars/logs + 随机非 50380 端口） |
| `scripts/load_test.py` | 8 阶段大规模压测（60 用户）；只使用可验证的专用 `load_admin` |
| `scripts/browser_verify.py` | Playwright 浏览器验收 |
| `scripts/screenshot_verify.py` | 关键页截图验收 |
| `scripts/api_full_test.py` | HTTP API 关键链路集成测试；SSE 只核对终态 snapshot 与 replay；隔离 DB 播种专用账号 |
| `scripts/contest_stress.py` | 默认验证赛事 draft 名册容量与静态赛制估算；`--run` 才真跑；只使用专用 `cs_admin` |
| `scripts/seed_test_accounts.py` | 种子测试账号（tester1/tester2 + 按内容同步的三游戏样例 Bot） |
| `scripts/seed_contest_showcase.py` | 生成/验收/白名单回滚六个长期只读赛事快照；绝对 DB 路径必填，主运行时另需停服确认 |
| `bzplat/frontend/e2e/*.spec.ts` | Chromium 真浏览器回归（当前静态收集 5 spec / 56 条：访客/用户/组织者/admin，Console+Network+SSE+WS、多视口、长文本滚动、排行榜密度及 3 条 Holdem HUD/复式/真人公开信息回归；最终同一代码 HEAD 全量执行真值见 `TESTING.md`） |

> 返回 [doc/INDEX.md](./INDEX.md)
