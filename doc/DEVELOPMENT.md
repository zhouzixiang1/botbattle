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
| `BZ_RATE_LIMIT` | 启用限流 | 1 |
| `BZ_TRUST_PROXY` | 信任 X-Forwarded-For（反向代理部署时需开启，否则限流按代理 IP 失效） | 未设 |
| `BZ_LOG_LEVEL` / `BZ_LOG_DIR` | 日志级别 / 目录 | INFO / logs |
| `SMTP_HOST/PORT/USER/PASSWORD/FROM` | SMTP（邮箱验证/重置/通知） | 未配则注册/重置返回 503 |
| `EMAIL_CODE_TTL_MINUTES` | 验证码 TTL | 30 |

> ⚠️ **敏感信息警示**：`.env` 含 SMTP 明文密码，**绝不提交**。`.gitignore` 应排除 `.env`。文档中不回写真实凭据。

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
> Poker.JS（vendor 副本，来源 Tairraos/Poker.JS，经 botzone 使用，canvas 矢量扑克牌绘制）。

### 2.3 改完代码必须 rebuild + restart
```bash
bash scripts/rebuild.sh   # npm run build → platform-ctl.sh restart
```
> 前端产物（`dist`）由后端 StaticFiles 托管、后端代码由运行进程加载——**不 rebuild + restart 代码不生效**（常见症状：新路由 405）。

### 2.4 worktree 隔离开发（勿碰线上 50380）

主目录 `main` 只跑线上服务（默认 `:50380` + 主库）。特性开发在 **git worktree** 中跑**独立**栈，避免污染线上 db/源码：

```bash
# 1) 后端：CWD=worktree，端口勿用 50380
cd .worktrees/<分支名>
python -m bzplat.backend.cli serve --host 127.0.0.1 --port 50381

# 2) 前端：proxy 必须指向 worktree 后端
cd .worktrees/<分支名>/bzplat/frontend
BZ_API_TARGET=http://127.0.0.1:50381 npm run dev
```

- **严禁**前端 `BZ_API_TARGET` 指向 50380（测试写入线上 db）。
- **严禁**在主目录 CWD 起 worktree 后端（会加载主源码 + 主库）。
- 合并走 GitHub PR；详见根目录 [`AGENTS.md`](../AGENTS.md)「worktree 隔离工作流」。

## 3. 编码规范

| 规范 | 要求 |
|------|------|
| **Python 包名** | 必须是 `bzplat`，**绝不能叫 `platform`**（会遮蔽标准库 `platform`）。所有 import 用绝对路径 `from bzplat.backend...` |
| **常量集中** | 所有状态码/对局类型/`REGISTERED_ENGINES`/`VALID_GAME_IDS`/平台 settings 键名集中在 `store/schema.py`，别散落 |
| **日志** | 后端**禁止 `print()`**，统一 `logging.getLogger(__name__)` |
| **游戏解耦** | 通用层（matches/contests/store/api_routes）**禁止 `if game_id == ...` 分支**；经 `games.registry.get(game_id)` 取 `GameSpec` |
| **资源硬顶** | 每 Bot `--cpus=1` / `--memory=512m`，半负载并发 ceiling=`max(1,cpu//4)`，全员循环 `FULL_RR_MAX_N=12`；admin 不可抬高（`runtime/limits.py`） |
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

### 5.1 新增一款游戏（赛制/编排层零改动）

通用层**不得**再加 `if game_id == ...` 分支。权威 checklist 与 [`AGENTS.md`](../AGENTS.md) / [`DESIGN.md`](./DESIGN.md) §2.3 一致：

1. 建 `games/<game>/` 子包：
   - `engine.py`（裁判 Session，`run_async(decide) → MatchResult`）
   - `protocol.py`（`dumps_request` / `loads_response` / `fail_response`；棋类可 re-export `games/_board_protocol.py`）
   - `result.py`（**独立**定义，满足鸭子契约：`winners` + `deltas`，**不**共享基类）
   - `tiers.py`（段位曲线，查表用 `base.tier_for_in`）
   - `templates.py`（本游戏内置赛事模板）
   - `spec.py`（装配 `GameSpec`）
2. `store/schema.py`：新增 `matches_<game>` 表（仿现有 per-game 表，FK 用 `ON DELETE SET NULL`）+ 索引；`REGISTERED_ENGINES` / `VALID_GAME_IDS` frozenset 各加一项。
3. `games/__init__.py`：`registry.register(SPEC)` 一行（启动时断言 schema 与注册表 ID 集合一致）。
4. 前端：`src/games/<game>/index.ts`（`GameViewSpec`：Board/reducer/`CanvasRenderer`/configFields）+ 在 `src/games/index.ts` 注册。
5. **禁止反向依赖**：`games/<game>/` 不得反向 import 已删的 `engine`/`_compat`/`protocol` shim 或通用层；通用层不得 import 具体游戏模块（`test_import_cycles.py` 源码扫描守护，forbidden 含已删 shim 作"防回退"哨兵）。
6. 跑测试：`pytest`（含 `test_result_contract` / `test_import_cycles` / `test_game_registry` / `test_tongyong_layer_no_game_branches`）+ `npm run build`。

> `engine/`/`protocol/`/`_compat/` shim 已删除（真实现全在 `games/`）。新代码直接面向 `games` 注册表，不要在 `matches/runner.py` 加游戏分支。

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
- **admin「日志」Tab**：`GET /api/admin/logs?file={app|access|audit}`（文件参数白名单）。详见 [wiki/SECURITY.md](../wiki/SECURITY.md)。

### 6.3 测试种子账号
```bash
python scripts/seed_test_accounts.py   # 建 tester1/tester2，各上传三游戏样例 Bot（幂等）
```

### 6.4 关键脚本
| 脚本 | 用途 |
|------|------|
| `scripts/platform-ctl.sh` | 启停：start/stop/restart/status/logs |
| `scripts/rebuild.sh` | npm build + restart |
| `scripts/e2e_smoke.sh` | 端到端冒烟（独立 DB + 端口 50381） |
| `scripts/load_test.py` | 8 阶段大规模压测（60 用户） |
| `scripts/browser_verify.py` | Playwright 浏览器验收 |
| `scripts/screenshot_verify.py` | 关键页截图验收 |
| `scripts/api_full_test.py` | HTTP API 全量业务正确性测试（鉴权/上传/挑战/SSE/并发） |
| `scripts/contest_stress.py` | 赛事大规模对阵压测 |
| `scripts/seed_test_accounts.py` | 种子测试账号（tester1/tester2 + 三游戏样例 Bot） |

> 返回 [doc/INDEX.md](./INDEX.md)
