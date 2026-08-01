# AGENTS.md — botbattle

多游戏 Bot 线上对战平台（holdem 德州扑克 / gomoku 五子棋 / pencil 点格棋）：用户上传二进制 Bot，平台在沙箱中跑对局，提供观赛、回放、Glicko-2 排行榜与组织者赛事。

## 开发规范（务必遵守）

- **先理解再动手**：任何修改前，必须先沿调用链查到根、定位到底层实现、读懂逻辑后再改。禁止"看到表层就改"。
- **分支工作流**：任何修改先从 `main` 切出特性分支（`feat/...` 或 `fix/...`），在分支上完成；修复合并回 `main`，**合并后删除原分支**。不要直接在 `main` 上提交。
- **多 agent 协作避免上下文污染**：不同任务用独立分支/独立 agent 隔离；不要让一个 agent 的大改动串进另一个任务的上下文。每个 agent 只对自己的分支负责，改完即合并即清。
- **提交前跑测试**：`pytest`（从仓库根目录），前端改了再 `npm run build`。
- **改动须同步三处**（提交前自检）：
  1. **测试**：有功能/行为变更 → 在 `bzplat/backend/tests/` 加/改测试用例，覆盖新逻辑与边界。
  2. **文档**：新增模块/接口/常量/行为 → 同步 `wiki/` 对应文档（必要时更新 `INDEX.md` 与 `AGENTS.md` 架构分层）。
  3. **记忆**：非显而易见的项目约定/架构决策 → 写入记忆文件（`MEMORY.md` 索引 + 单独 fact 文件，见会话记忆目录）。
  不许只改代码不补测试/文档——三者缺一视为未完成。

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

# 前端（React 19 + Vite 8 + Tailwind v4，浅色主题）
cd bzplat/frontend && npm install && npm run build   # 产物在 bzplat/frontend/dist/，由后端 StaticFiles 托管

# 起服务（默认 127.0.0.1:50380）
scripts/platform-ctl.sh start     # 或：botzone serve
botzone create-admin <user> <email> '<pass>'   # 建管理员，跳过邮箱验证
```

- **测试**：`pytest`（`pyproject.toml` 设 `testpaths=["bzplat/backend/tests","tests"]`，`pythonpath=["."]`），务必从仓库根运行。
- **本地无 Docker 跑 ELF**：`export BZ_BOT_LOCAL=1`（`BinaryRunner` 退回本机 subprocess，仅测试用）。
- **端到端冒烟**：`bash scripts/e2e_smoke.sh`。
- **测试种子账号**：`python scripts/seed_test_accounts.py`（建 tester1/tester2，各上传 holdem/gomoku/pencil 样例 Bot；幂等，便于对战/人类对战测试）。
- **改完代码必须 rebuild + restart**：`bash scripts/rebuild.sh`（`npm run build` → `platform-ctl.sh restart`）。前端产物（`bzplat/frontend/dist`）由后端 StaticFiles 托管、后端代码由运行进程加载——不 rebuild+restart 代码不会生效（常见症状：新路由 405 Method Not Allowed）。
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
engine/     裁判：game.py(holdem) gomoku.py pencil.py + 共享基类 result.py + registry.run_session()
            段位：engine/tiers.py（rating→段位映射，前端 lib/tiers.ts 镜像）
            数据集：GET /api/matchpacks[/download]（gzip，等级 gating）+ 站点配置 GET /api/site/info
protocol/   行协议：json_protocol.py(holdem) / board_protocol.py(gomoku,pencil)
runtime/    沙箱：BinaryRunner(docker/wine/local) + limits
store/      SQLite + schema.py(常量唯一来源)
api_routes  接口：REST + SSE(观赛 /events) + WebSocket(人类对战 /play)；用户搜索 /api/users；用户主页 /api/users/{name}/{profile,bots}；全局搜索 /api/search；admin 日志 /api/admin/logs
auth/       认证 + 资料编辑：PUT /api/auth/profile（display_name/bio）+ POST /api/auth/avatar（本地 avatars/ 托管）
logging     统一日志：logging_config.setup_logging（logs/app.log，含 bot stderr 捕获），cli serve 接入
matches/    后台对局：auto_matcher（闲时自动调度，ladder 类型，stale/placement/daily-cap 增强）
```

**前端架构（bzplat/frontend，React 19 + Vite 8 + Tailwind v4 + shadcn/ui）**：
```
src/index.css              设计 token：shadcn v4 OKLCH 双主题（:root 浅 / .dark 暗）emerald 品牌色系 + @theme inline 桥接
src/components/ui/         共享 UI 原语库（shadcn：Button/Input/Card/Table/Tabs/Badge/Dialog/Command/Chart...）—— 全项目唯一组件抽象层
src/components/ui/status.tsx   EmptyState/Loading/ErrorMsg/RefreshBtn/StatusBadge（前台+管理端共用）
src/components/shell/      全局 Shell：AppShell（顶栏+导航+页脚）+ nav-config + GlobalSearch（Cmd+K Command 面板）
theme-provider/toggle      next-themes 暗色（class 策略，light 默认 + system）+ 太阳/月亮切换
src/pages/                 22 个顶层路由，全部用 React.lazy 代码分割（每页独立 chunk，recharts 等重依赖隔离）
路径别名 @/ → src/          新代码一律用 @/，禁相对路径；图标统一 lucide-react（无 emoji）
```
改前端务必遵循 [doc/DESIGN.md](doc/DESIGN.md) §5 前端架构：用 `@/components/ui/*` + 语义 token（bg-background/text-primary 等），不裸 hex 不硬编码 slate/brand 颜色。

**核心解耦契约 —— `engine/result.py` 的 `RoundResult`/`MatchResult`**：裁判（engine）产出 `winners`(座位号,空=平局) + `deltas`(长2零和)；编排层与赛制层**只依赖这两个字段**，绝不触碰扑克的 pot/board/holes 或棋类的棋盘。这是赛制代码能对三款游戏通用的根本。

**新增一款游戏的成本**（赛制/编排层零改动）：实现一个 `XxxSession.run_async(decide)→MatchResult` + 一套协议 → 在 `registry.run_session` 加分支，并在 `schema.REGISTERED_ENGINES` / `VALID_GAME_IDS` 各加一项。

**引擎路由入口**：`registry.run_session(game_id, decide, ...)` —— 按 `game_id` 分流到对应 Session；`MatchRunner` 再按 `game_id` 分流协议（`_dumps/_loads/_fail_response`）。

**人类 vs Bot**（`match_type=human`）：引擎 `decide(player_idx, request)` 每回合阻塞；`run_bot_vs_human` 把 bot 侧接 BinaryRunner、人类侧接一个等待 `asyncio.Future` 的协程。orchestrator 的 `_human_turns` 注册 pending 回合并广播 `your_turn`，WebSocket `/play` 收到落子即 `resolve_human_turn`。人类对局走独立 `_human_sem`（默认 4，不占 bot 对局槽）、`human_action_timeout`（默认 120s）、**不计 Glicko**、per-user 同时 ≤ 1。自博弈（同 owner 两个不同 bot 对战）走普通 `/api/matches/challenge`。

**赛制阶段状态机**：`draft→open→running→(rest)→finished`。`ContestManager.maybe_finish` 是对局完成回调入口，负责瑞士补轮 / 淘汰晋级 / 休息期换 Bot / 进入下一阶段。

## 动手前必读文档

- `wiki/PROTOCOL.md` —— 德州紧凑 JSON 行协议（字段、raise=raise-to-total、卡牌编码）。
- `wiki/BOT_DEV.md` —— Bot 开发指南（编译、上传、调试）。
- `wiki/INDEX.md` —— 文档总入口（含 vs Botzone 差异表：本平台**整场长驻**、一行一条 JSON）。
- `contracts/` —— 协议 JSON Schema。
- `samples/` —— 三款游戏样例 Bot 源码。
