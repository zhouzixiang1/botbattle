# AGENTS.md — botbattle

多游戏 Bot 线上对战平台（holdem 德州扑克 / gomoku 五子棋 / pencil 点格棋）：用户上传二进制 Bot，平台在沙箱中跑对局，提供观赛、回放、Glicko-2 排行榜与组织者赛事。

## 开发规范（务必遵守）

- **先理解再动手**：任何修改前，必须先沿调用链查到根、定位到底层实现、读懂逻辑后再改。禁止"看到表层就改"。
- **分支工作流**：任何修改先从 `main` 切出特性分支（`feat/...` 或 `fix/...`），在分支上完成；修复合并回 `main`，**合并后删除原分支**。不要直接在 `main` 上提交。
- **多 agent 协作避免上下文污染**：不同任务用独立分支/独立 agent 隔离；不要让一个 agent 的大改动串进另一个任务的上下文。每个 agent 只对自己的分支负责，改完即合并即清。
- **提交前跑测试**：`pytest`（从仓库根目录），前端改了再 `npm run build`。

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

## 关键约束（容易踩坑）

- **Python 包名必须是 `bzplat`，绝不能叫 `platform`**（会遮蔽标准库 `platform`）。所有 import 用绝对路径 `from bzplat.backend... import ...`。
- **所有常量集中在 `bzplat/backend/store/schema.py`**：状态码、对局类型、`REGISTERED_ENGINES`、`VALID_GAME_IDS`、`platform_settings` 键名。新增常量加这里，别散落。
- **后端禁止 `print()`**：统一用 `logging.getLogger(__name__)`（全仓 10+ 模块均如此）。
- **资源硬顶**（`runtime/limits.py`，admin 不可抬高）：每 Bot `--cpus=1` / `--memory=512m`；半负载并发 ceiling = `max(1, cpu//4)`；全员单/双循环人数上限 `FULL_RR_MAX_N=12`。

## 架构分层（编辑时切勿越界）

```
contests/   赛制：templates(阶段模板+计分) → stages(对阵生成) → manager(阶段状态机)
matches/    编排：orchestrator(入队/SSE/评分/判胜) + runner(起2个Bot进程,按game_id路由)
engine/     裁判：game.py(holdem) gomoku.py pencil.py + 共享基类 result.py + registry.run_session()
protocol/   行协议：json_protocol.py(holdem) / board_protocol.py(gomoku,pencil)
runtime/    沙箱：BinaryRunner(docker/wine/local) + limits
store/      SQLite + schema.py(常量唯一来源)
```

**核心解耦契约 —— `engine/result.py` 的 `RoundResult`/`MatchResult`**：裁判（engine）产出 `winners`(座位号,空=平局) + `deltas`(长2零和)；编排层与赛制层**只依赖这两个字段**，绝不触碰扑克的 pot/board/holes 或棋类的棋盘。这是赛制代码能对三款游戏通用的根本。

**新增一款游戏的成本**（赛制/编排层零改动）：实现一个 `XxxSession.run_async(decide)→MatchResult` + 一套协议 → 在 `registry.run_session` 加分支，并在 `schema.REGISTERED_ENGINES` / `VALID_GAME_IDS` 各加一项。

**引擎路由入口**：`registry.run_session(game_id, decide, ...)` —— 按 `game_id` 分流到对应 Session；`MatchRunner` 再按 `game_id` 分流协议（`_dumps/_loads/_fail_response`）。

**赛制阶段状态机**：`draft→open→running→(rest)→finished`。`ContestManager.maybe_finish` 是对局完成回调入口，负责瑞士补轮 / 淘汰晋级 / 休息期换 Bot / 进入下一阶段。

## 动手前必读文档

- `wiki/PROTOCOL.md` —— 德州紧凑 JSON 行协议（字段、raise=raise-to-total、卡牌编码）。
- `wiki/BOT_DEV.md` —— Bot 开发指南（编译、上传、调试）。
- `wiki/INDEX.md` —— 文档总入口（含 vs Botzone 差异表：本平台**整场长驻**、一行一条 JSON）。
- `contracts/` —— 协议 JSON Schema。
- `samples/` —— 三款游戏样例 Bot 源码。
