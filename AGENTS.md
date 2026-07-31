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
engine/     裁判：game.py(holdem) gomoku.py pencil.py + 共享基类 result.py + registry.run_session()
protocol/   行协议：json_protocol.py(holdem) / board_protocol.py(gomoku,pencil)
runtime/    沙箱：BinaryRunner(docker/wine/local) + limits
store/      SQLite + schema.py(常量唯一来源)
api_routes  接口：REST + SSE(观赛 /events) + WebSocket(人类对战 /play)；用户搜索 /api/users
```

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
