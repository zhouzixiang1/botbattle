# AGENTS.md — botbattle

多游戏 Bot 线上对战平台（holdem 德州扑克 / gomoku 五子棋 / pencil 点格棋）：用户上传二进制 Bot，平台在沙箱中跑对局，提供观赛、回放、Glicko-2 排行榜与组织者赛事。

## 开发规范（务必遵守）

- **先理解再动手**：任何修改前，必须先沿调用链查到根、定位到底层实现、读懂逻辑后再改。禁止"看到表层就改"。
- **worktree 隔离工作流**（硬约束——主目录、后端、数据库都不受开发影响）：
  主目录只跑 `main` 的线上服务（:50380 + 主 db + 主源码），**绝不被开发分支污染**。流程：
  1. 主目录保持 `main` 干净（只 `git pull` 同步）；50380 服务始终是 main 最新代码、线上 db 不被测试写入。
  2. `git worktree add .worktrees/<分支名> -b feat/...`（或 `fix/...`）——共享主仓库 `.git`，秒建零拷贝。`.worktrees/` 已在 `.gitignore`（不跟踪 node_modules/dist/db 等产物）。
  3. **worktree 跑完全独立的运行时栈**（CWD=worktree 是隔离关键）：
     - **后端**：`cd .worktrees/<分支名> && python -m bzplat.backend.cli serve --host 127.0.0.1 --port <非50380>`
       （CWD=worktree → 加载 worktree 源码 + worktree/botzone.db + 独立 bot_uploads/avatars/logs；与主目录源码/db 完全隔离）
     - **前端**：`cd .worktrees/<分支名>/bzplat/frontend && npm install && BZ_API_TARGET=http://127.0.0.1:<worktree端口> npm run dev`
       （vite.config.ts 的 proxy 目标读 `BZ_API_TARGET` 环境变量）
     - **严禁**前端 proxy 到 50380 线上服务（会把测试写进线上 db）；**严禁** worktree 后端用 CWD=主目录（会加载主目录源码+db）。
  4. **合并必须走 GitHub Pull Request**（`gh pr create` → 评审 → 合并到 main），**禁止本地 `git merge` 直推 main**。
  5. PR 合并后**清理**：停 worktree 服务 → 主目录 `git worktree remove .worktrees/<分支名>` → 删分支（本地 + 远端）→ 主目录 `git pull` + `bash scripts/rebuild.sh`（rebuild + restart，让 50380 生效新代码）。
- **多 agent 协作避免上下文污染**：不同任务用独立 worktree（独立分支 + 独立目录 + 独立运行时栈）隔离；不要让一个 agent 的大改动串进另一个任务的上下文。每个 agent 只对自己的 worktree 负责，改完即合并即清。
- **提交前跑测试**：`pytest`（从仓库根目录），前端改了再 `npm run build`。
- **改动须同步三处**（提交前自检）：
  1. **测试**：有功能/行为变更 → 在 `bzplat/backend/tests/` 加/改测试用例，覆盖新逻辑与边界。
  2. **文档**：新增模块/接口/常量/行为 → 同步 `wiki/` 对应文档（必要时更新 `INDEX.md` 与 `AGENTS.md` 架构分层）。
  3. **记忆**（若当前会话环境提供 memory 能力）：非显而易见的项目约定/架构决策 → 写入记忆索引 + 单独 fact；**不以仓库内缺失 MEMORY.md 为阻塞**——测试与文档必须同步。
  不许只改代码不补测试/文档——功能交付缺测试或文档视为未完成。

## 文档规范（改代码必同步）

文档分三个落点，职责不重叠：

- **`doc/`** —— 面向**甲方/干系人/平台开发者**的交付与工程文档（需求/设计/开发/测试/总结）。共 6 份，入口 `doc/INDEX.md`。
- **`wiki/`** —— 面向 **Bot 玩家/访客**的对外文档（游戏规则、对局协议、Bot 开发指南、功能使用）。入口 `wiki/INDEX.md`。
- **`README.md`** —— 项目门面：能力一览 + 快速开始 + 指向 `doc/` 与 `wiki/` 的导航。

**边界**：工程内容（需求/架构/设计/测试/规范）只进 `doc/`；协议/规则/Bot 开发只进 `wiki/`——两边互链不复制。

改代码时必须同步的文档（提交前自检）：

1. 新增/改模块、接口、常量、架构分层 → `doc/DESIGN.md`（必要时同步本文件「架构分层」段）。
2. 改对外协议字段、游戏规则、Bot 行为 → `wiki/`（协议/规则/功能说明）。
3. 改构建/起服务/依赖/环境变量 → `doc/DEVELOPMENT.md`。
4. 改测试策略/新增测试维度 → `doc/TESTING.md`。
5. 改对外能力/技术栈/目录结构 → `README.md` + `doc/OVERVIEW.md`。

**命名**：一律 `SCREAMING_SNAKE_CASE.md` 英文文件名（可检索、与 wiki 一致），H1 标题用中文。新增文件后回填对应 `INDEX.md`，否则视为未完成。

## 构建与运行

```bash
# 后端（Python ≥ 3.12；venv 在 .venv/）
source .venv/bin/activate
pip install -e '.[dev]'          # 装 bzplat 包 + pytest/httpx

# 前端（React 19 + Vite 8 + Tailwind v4，浅色默认 + 暗色双主题）
cd bzplat/frontend && npm install && npm run build   # 产物在 bzplat/frontend/dist/，由后端 StaticFiles 托管

# 起服务（默认 127.0.0.1:50380）
scripts/platform-ctl.sh start     # 或：botzone serve
botzone create-admin <user> <email> '<pass>'   # 建管理员，跳过邮箱验证
```

- **测试**：`pytest`（`pyproject.toml` 设 `testpaths=["bzplat/backend/tests"]`，`pythonpath=["."]`），务必从仓库根运行。
- **本地无 Docker 跑 ELF**：`export BZ_BOT_LOCAL=1`（`BinaryRunner` 退回本机 subprocess，仅测试用）。
- **端到端冒烟**：`bash scripts/e2e_smoke.sh`。
- **测试种子账号**：`python scripts/seed_test_accounts.py`（建 tester1/tester2，各上传 holdem/gomoku/pencil 样例 Bot；幂等，便于对战/人类对战测试）。
- **改完代码必须 rebuild + restart**：`bash scripts/rebuild.sh`（`npm run build` → `platform-ctl.sh restart`）。前端产物（`bzplat/frontend/dist`）由后端 StaticFiles 托管、后端代码由运行进程加载——不 rebuild+restart 代码不会生效（常见症状：新路由 405 Method Not Allowed）。
- **worktree 前端独立预览**（开发期，不碰主服务 50380）：先在 worktree 起独立后端 `cd .worktrees/<分支> && python -m bzplat.backend.cli serve --port 50381`，再 `BZ_API_TARGET=http://127.0.0.1:50381 npm run dev`（vite dev server，proxy 到 worktree 后端）。详见上方「worktree 隔离工作流」。
- **日志**：`logs/app.log`（`logging_config.setup_logging`，统一格式 `时间 级别 [模块] 消息`）。排查对局/bot 崩溃/auto-match/WS 问题在此；admin「日志」Tab 可网页查看与过滤。bot EOF 会附带 stderr 末尾。

## 关键约束（容易踩坑）

- **Python 包名必须是 `bzplat`，绝不能叫 `platform`**（会遮蔽标准库 `platform`）。所有 import 用绝对路径 `from bzplat.backend... import ...`。
- **所有常量集中在 `bzplat/backend/store/schema.py`**：状态码、对局类型、`REGISTERED_ENGINES`、`VALID_GAME_IDS`、`platform_settings` 键名。新增常量加这里，别散落。
- **后端禁止 `print()`**：统一用 `logging.getLogger(__name__)`（全仓 10+ 模块均如此）。
- **资源硬顶**（`runtime/limits.py`，admin 不可抬高）：每 Bot `--cpus=1` / `--memory=512m`；半负载并发 ceiling = `max(1, cpu//4)`；全员单/双循环人数上限 `FULL_RR_MAX_N=12`。

## 架构分层（编辑时切勿越界）

```
contests/   赛制：templates(阶段模板+计分) → stages(对阵生成) → manager(阶段状态机)
matches/    编排：orchestrator(入队/SSE/评分/判胜/人类对战) + runner(起Bot进程,按game_id路由)
            人类对战：orchestrator.challenge_human/_run_human_match + runner.run_bot_vs_human
            （人类侧经 _human_turns Future + WebSocket /api/matches/{id}/play 回传落子，独立 _human_sem，不计 Glicko）
            评分副作用：_apply_ratings 在更新 ratings 时顺带落 rating_history（段位趋势）+ 累积 pair_stats 胜负（Bot 详情对手战绩）
            通知副作用：对局完成（非 contest）经 orch.notifier.notify_both_owners 通知双方 owner
notifications/ 通知管理器：NotificationManager（写站内通知 + 按 prefs 复用 Mailer 发邮件）；表 notifications/notification_prefs
            经验/等级：award_xp 在对局完成/赛事报名/评论/被关注时触发（users.xp/level/last_active_at）
games/      游戏注册表（全面解耦的单一真相）：base.py(GameSpec 接口 + GameRegistry 单例
            + MatchResult/RoundResult 平台契约基类，仅类型提示/测试用) + __init__.py(注册表
            单例 + run_session/normalize_game_id/tier_for/tier_dict/all_tiers/GAME_LABELS
            等模块级便捷函数) + _board_protocol.py(棋类共享行协议工具)
            + 每游戏完全自包含的子包 games/<game>/：engine.py(裁判) + protocol.py(行协议)
            + result.py(结果，独立定义不共享基类) + tiers.py(per-game 段位) + cards.py(holdem)
            + templates.py(赛事模板) + spec.py(装配 GameSpec)。GameSpec 集中声明一款游戏的全部固有属性。
            通用层经 registry.get(game_id) 取 spec 调用其能力，**禁止 if game_id== 分支**
            新增游戏 = 建 games/<game>/ 包 + 注册一行 + schema 加一项
            （engine/ + protocol/ + _compat/ 三层冗余 shim 已删——真实现全在 games/）
            数据集：GET /api/matchpacks[/download]（gzip，等级 gating）+ 站点配置 GET /api/site/info
runtime/    沙箱：BinaryRunner(docker/wine/local) + limits
store/      SQLite + schema.py(常量唯一来源)；matches 拆每游戏表 + matches_index + ratings per-game
api_routes  接口：REST + SSE(观赛 /events) + WebSocket(人类对战 /play)；用户搜索 /api/users；用户主页 /api/users/{name}/{profile,bots}；全局搜索 /api/search；admin 日志 /api/admin/logs
auth/       认证 + 资料编辑：PUT /api/auth/profile（display_name/bio）+ POST /api/auth/avatar（本地 avatars/ 托管）
logging     统一日志：logging_config.setup_logging（logs/app.log，含 bot stderr 捕获），cli serve 接入
matches/    后台对局：auto_matcher（闲时自动调度，ladder 类型，stale/placement/daily-cap 增强）
```

**前端架构（bzplat/frontend，React 19 + Vite 8 + Tailwind v4 + shadcn/ui）**：
```
src/index.css              设计 token：shadcn v4 OKLCH 双主题（:root 浅 / .dark 暗）emerald 品牌色系 + @theme inline 桥接
src/components/ui/         共享 UI 原语库（shadcn new-york：Button/Input/Card/Table/Tabs/Badge/Dialog/DropdownMenu/Select/Command/Popover/Tooltip/Slider/Switch/Separator/Sheet/Skeleton/Sonner/Avatar/Label/ScrollArea/MetricCard/Chart...）—— 全项目唯一组件抽象层
src/components/ui/status.tsx   EmptyState/Loading/ErrorMsg/RefreshBtn/StatusBadge（前台+管理端共用）
src/components/ui/select.tsx   shadcn Select（Radix）—— 全站下拉框唯一实现，禁裸用原生 <select>
src/components/shell/      全局 Shell：AppShell（已登录 lg+ 侧栏 / 访客全断点顶栏含登录注册 + 导航 + 页脚）+ nav-config + GlobalSearch（Cmd+K Command 面板）
theme-provider/toggle      next-themes 暗色（class 策略，light 默认 + system）+ 太阳/月亮切换
src/pages/                 22 个顶层路由，全部用 React.lazy 代码分割（每页独立 chunk，recharts 等重依赖隔离）
路径别名 @/ → src/          新代码一律用 @/，禁相对路径；图标统一 lucide-react（无 emoji）
```
改前端务必遵循 [doc/DESIGN.md](doc/DESIGN.md) §5 前端架构：用 `@/components/ui/*` + 语义 token（bg-background/text-primary 等），不裸 hex 不硬编码 slate/brand 颜色。
**下拉框统一规范**（硬约束）：所有下拉框一律用 `@/components/ui/select`（shadcn Radix Select）+ `SelectTrigger/SelectValue/SelectContent/SelectItem`，**禁止裸用原生 `<select>`**（跨设备/浏览器展开样式不统一）。迁移注意 4 点：
1. 受控 API：`<Select value onValueChange>`（非 `onChange(e.target.value)`）。
2. **空值哨兵**：表"全部/不过滤"的空 value `''` 不能直接传 Radix（`value=""` 被当未选/placeholder）——统一用哨兵 `'all'`：`value={x || 'all'}` + `onValueChange={(v) => setX(v === 'all' ? '' : v)}`。
3. **number value 转 string**：Radix value 只接受 string，`speedIdx`/座位号等 number 需 `value={String(n)}` + `onValueChange={(v) => setN(Number(v))}`；动态实体 id（number）的 `<SelectItem value={String(id)}>`。
4. **label 包裹**：SelectTrigger 是 `<button>` 不支持 `htmlFor`——表单内用 `<div className="space-y-1.5"><Label>…</Label><Select>…</Select></div>`；inline 行内用 `<div className="flex items-center gap-2"><span>…</span><Select>…</Select></div>`。

**核心解耦契约层**（全面解耦后，游戏对平台暴露统一契约；违反这些契约的游戏会在运行时崩）：
- **结果鸭子类型**（result.py 独立定义，不共享基类）：裁判产出 `winners`(座位号,空=平局) + `deltas`(长2零和)；编排层与赛制层**只依赖这两个字段**（+ `rounds_played`/`rounds`/`events`/`winner`），绝不触碰扑克的 pot/board/holes 或棋类的棋盘。**`winner` 在引擎内权威化**（PR4）：棋类单轮取胜者；holdem 多手按累计净筹码（`final_chips`/net）比较——编排层只读 `result.winner`（+ ea/eb 平局兜底），不再有 match_end 事件三层兜底 / holdem 特例注释（隐性 if-game_id 已消除）。**测试守护**：`tests/test_result_contract.py` 断言三游戏 result 都满足此契约（防 drift）。
- **GameSpec 接口**（`games/base.py`）：每款游戏须声明全部字段——`session_factory`(裁判,`async __call__(decide,*,on_event,**params)→MatchResult`，Protocol 与 `run_session` 唯一调用点对齐)、`protocol`(`dumps_request/loads_response/fail_response`)、`default_match_params`/`validate_match_params`、`rounds_per_match`/`normalize_earnings`/`eta_for_match`(编排特化)、`judge_params`(`field` 是 `run_session` 形参名，须与 session_factory kwarg 一致，否则 admin 设置静默失效)、`tiers`(段位曲线，查表算法共享 `base.tier_for_in`，无需各游戏再包一层 `tier_for`)、`templates`、`default_scoring`(默认计分，通用层从 spec 读不得硬编码 poker_3_1_0)、`num_seats`(座位数，当前全 2，预留 N 人扩展)、`code_path`/`summary`(元信息)、`preflight_check`(上传预检)。**每个字段都被通用层真正消费**（无死字段；曾删 `eta_per_match_sec`/`frontend_module`/`tier_for` 三个死字段）。通用层经 `registry.get(game_id)` 取 spec，**禁止 `if game_id==` 分支**（`tests/test_import_cycles.py` 源码扫描守护 games/<game>/ 不反向 import engine；`tests/test_tongyong_layer_no_game_branches.py` 源码扫描守护通用层无 game-name 分支/硬编码 3-game 列表/直接 import 具体游戏模块——含 `==`/`!=`/`in`/`startswith`/`.get("holdem")` 各变体，合法兜底用 `# allow-game-fallback` 注释豁免）。
- **段位 per-game**：`games/<game>/tiers.py` 声明该游戏段位曲线（查表算法共享 `base.tier_for_in`，曲线数据独立可调）；`registry.tier_for(game_id, rating)` 统一经 `tier_for_in(rating, spec.tiers)`（各游戏无需再声明 `tier_for` 字段）；`/api/tiers?game_id=` 返回对应曲线；前端 `lib/tiers.ts` 的 `useGameTiers(gameId)` 按游戏拉取着色。

**新增一款游戏的成本**（通用层零改动）——checklist：
1. 建 `games/<game>/` 子包：`engine.py`(裁判) + `protocol.py`(行协议) + `result.py`(独立结果，满足鸭子契约) + `tiers.py`(段位曲线) + `templates.py`(赛事模板) + `spec.py`(装配 GameSpec)。棋类协议可 re-export `games/_board_protocol.py`。
2. 建 `schema.py`：`matches_<game>` 表（仿 matches_holdem，FK 用 `ON DELETE SET NULL`）+ 索引；`REGISTERED_ENGINES`/`VALID_GAME_IDS` frozenset 各加该项。
3. `games/__init__.py`：`registry.register(SPEC)` 一行（启动断言 schema 与注册表一致）。
4. 前端 `src/games/<game>/`：`index.ts`（GameViewSpec：Board/kind/configFields）+ `canvas.ts`（CanvasRenderer）+ `reducer.ts`（事件归约，自包含对标后端 engine.py）+ `src/games/index.ts` 注册一行。`RawEvent` 公共类型在 `src/games/base.ts`；`normalizeGameId` 从注册表 `GAMES` 派生（不硬编码游戏名三选一）。
5. **不得**反向：`games/<game>/` 不得 import `bzplat.backend.engine`/`_compat`（循环依赖，`test_import_cycles.py` 守护）；通用层（matches/contests/store/api_routes）不得 import 具体游戏模块（经注册表）。
6. 跑测试：`pytest`（含 `test_result_contract`/`test_import_cycles`/`test_game_registry`）+ `npm run build` + `screenshot_verify.py`。

**引擎路由入口**：`games.registry.get(game_id)` 取 `GameSpec` → `spec.run_session(decide, **params)` 构造并运行该游戏 Session；`spec.protocol.dumps_request/loads_response/fail_response` 处理行协议。`matches/runner.py` 经 games 注册表路由（`run_session`/`GAME_HOLDEM`/`normalize_game_id` 都 import 自 `bzplat.backend.games`），不再有 if-chain。

**人类 vs Bot**（`match_type=human`）：引擎 `decide(player_idx, request)` 每回合阻塞；`run_bot_vs_human` 把 bot 侧接 BinaryRunner、人类侧接一个等待 `asyncio.Future` 的协程。orchestrator 的 `_human_turns` 注册 pending 回合并广播 `your_turn`，WebSocket `/play` 收到落子即 `resolve_human_turn`。人类对局走独立 `_human_sem`（默认 4，不占 bot 对局槽）、`human_action_timeout`（默认 120s）、**不计 Glicko**、per-user 同时 ≤ 1。自博弈（同 owner 两个不同 bot 对战）走普通 `/api/matches/challenge`。

**赛制阶段状态机**：`draft→open→running→(rest)→finished`。`ContestManager.maybe_finish` 是对局完成回调入口，负责瑞士补轮 / 淘汰晋级 / 休息期换 Bot / 进入下一阶段。

## 动手前必读文档

- `wiki/PROTOCOL.md` —— 德州紧凑 JSON 行协议（字段、raise=raise-to-total、卡牌编码）。
- `wiki/BOT_DEV.md` —— Bot 开发指南（编译、上传、调试）。
- `wiki/INDEX.md` —— 文档总入口（含 vs Botzone 差异表：本平台**整场长驻**、一行一条 JSON）。
- `contracts/` —— 协议 JSON Schema。
- `samples/` —— 三款游戏样例 Bot 源码。
