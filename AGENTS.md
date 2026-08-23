# AGENTS.md — botbattle

多游戏 Bot 线上对战平台（holdem 德州扑克 / gomoku 五子棋 / pencil 点格棋）：用户上传二进制 Bot，平台在沙箱中跑对局，提供观赛、回放、Glicko-2 排行榜与组织者赛事。

## 开发规范（务必遵守）

- **先理解再动手**：任何修改前，必须先沿调用链查到根、定位到底层实现、读懂逻辑后再改。禁止"看到表层就改"。
- **worktree 隔离工作流**（硬约束——主目录、后端、数据库都不受开发影响）：
  主目录只跑 `main` 的线上服务（:50380 + 主 db + 主源码），**绝不被开发分支污染**。流程：
  1. 主目录保持 `main` 干净（只 `git pull` 同步）；50380 服务始终是 main 最新代码、线上 db 不被测试写入。
  2. `git worktree add .worktrees/<分支名> -b feat/...`（或 `fix/...`）——共享主仓库 `.git`，秒建零拷贝。`.worktrees/` 已在 `.gitignore`（不跟踪 node_modules/dist/db 等产物）。
  3. **worktree 跑完全独立的运行时栈**（CWD=worktree 是隔离关键）：
    - **后端**：`cd .worktrees/<分支名> && BZ_DB_PATH=$PWD/botzone.db BZ_INSTANCE_KEY=qa-mybranch BZ_QA_INSTANCE=1 python -m bzplat.backend.cli serve --host 127.0.0.1 --port <非50380>`（每个 worktree 替换为自己的稳定唯一 instance key）
       （CWD=worktree → 加载 worktree 源码 + worktree/botzone.db + 独立 bot_uploads/avatars/logs；与主目录源码/db 完全隔离）
     - **前端**：`cd .worktrees/<分支名>/bzplat/frontend && npm install && BZ_API_TARGET=http://127.0.0.1:<worktree端口> npm run dev`
       （vite.config.ts 的 proxy 目标读 `BZ_API_TARGET` 环境变量）
     - **严禁**前端 proxy 到 50380 线上服务（会把测试写进线上 db）；**严禁** worktree 后端用 CWD=主目录（会加载主目录源码+db）。
  3.5. **建 worktree 时把数据库带出来 + 评估对数据库的影响**（硬约束——测试要真实数据，但绝不能写主库）：
     - **带库**：worktree 新建时目录里没有 `botzone.db`（后端启动会建空库，缺数据导致测试不真实）。须从主目录**复制**一份主库到 worktree：
       ```bash
       cp /home/zzx/project/botbattle/botzone.db .worktrees/<分支名>/botzone.db
       # 副本与主库完全独立——往 worktree 库写不会影响主库（关键：是 cp 不是软链接）
       ```
     - **先评估影响再动**：复制前必须想清「这个 worktree 的改动会怎么读写数据库？」——新增表/列（需迁移测试）、写业务数据（造测试数据）、只读查询（副本即可）、清空/迁移（高风险，确认在 worktree 库操作）。评估结论写进 PR 描述的「数据库影响」一栏。
    - **起服务必须钉死 worktree 库与 Docker namespace**：`cd .worktrees/<分支名> && BZ_DB_PATH=$PWD/botzone.db BZ_INSTANCE_KEY=qa-mybranch BZ_DOCKER_HOST=unix:///var/run/docker.sock BZ_QA_INSTANCE=1 python -m bzplat.backend.cli serve --port <非50380>`——显式 `BZ_DB_PATH=$PWD/botzone.db`（绝对路径）锁死到 worktree 库，`BZ_INSTANCE_KEY` 每个并行 worktree 替换为稳定唯一小写值，杜绝 CWD 漂移、误连主库或跨实例清理容器。关联产物（`bot_uploads/`/`avatars/`/`logs/`）也跑在 worktree 下（CWD 隔离）。
     - **铁律：测试只能动 worktree 库，绝不动主分支库**——`/home/zzx/project/botbattle/botzone.db`（50380 服务）是**只读真相源**，任何写操作（造数据/迁移/清空/修复）都必须在 worktree 副本上进行。误写主库 = 污染线上，不可逆。验证某操作安全时，先在 worktree 库跑通再考虑是否适用于主库（且主库操作必须用户明确授权）。
  4. **合并必须走 GitHub Pull Request**（`gh pr create` → 评审 → 合并到 main），**禁止本地 `git merge` 直推 main**。
  5. PR 合并后**清理**：停 worktree 服务 → 主目录 `git worktree remove .worktrees/<分支名>` → 删分支（本地 + 远端）→ 主目录 `git pull` + `bash scripts/rebuild.sh`（rebuild + restart，让 50380 生效新代码）。
- **多 agent 协作避免上下文污染**（硬约束——三条铁律，违反会污染他人工作）：
  不同任务用独立 worktree（独立分支 + 独立目录 + 独立运行时栈）隔离；不要让一个 agent 的大改动串进另一个任务的上下文。每个 agent 只对自己的 worktree 负责，改完即合并即清。具体铁律：
  1. **不动别人开发一半的**：动手前必须 `git worktree list` + `git branch -a` 盘点现有 worktree/分支。遇到**非自己创建的** worktree（尤其含未提交改动 `git status` 非空的）——**那是其他 agent/人在开发的，绝不删、不改、不合并、不往里提交**。不确定归属时先问用户，不要自作主张。重启/中断恢复后尤其要重新盘点（worktree 可能在中断期间被他人新增/改动）。
  2. **端口/进程互不抢**：自己的 worktree 用 50381+ 端口（50380 是线上 main 专属），起服务前先 `ss -tlnp | grep -E '5038[0-9]|517[0-9]'` 确认端口空闲；不要 kill 非自己起的进程。
  3. **最后不留脏分支和产物**（收尾自检清单，缺一不可）：
     - 停掉自己起的所有服务进程（后端 serve / 前端 vite dev / 后台造数据脚本）——按 PID 精确 kill，`ps aux | grep -E 'bzplat.backend.cli serve|vite'` 确认无残留。
     - `git worktree remove` 自己的 worktree 目录（含 node_modules/db 等产物一并清除）。
     - **一次性临时脚本用完即删**：agent 写的数据迁移/修补/清理类脚本（`scripts/fix_*.py`、`migrate_*.py`、`cleanup_*.py`、`repair_*.py` 等）跑完必须立即删除，**不得残留在工作区/仓库**（否则会污染 `scripts/` 让后人误以为是长期运维脚本）。放 `/tmp/` 跑或跑完 `rm` 删；长期运维脚本（`platform-ctl.sh`/`rebuild.sh`/`e2e_smoke.sh`/`seed_test_accounts.py` 等）不在删除范围。
     - 删自己的分支：本地 `git branch -D <分支>` + 远端 `git push origin --delete <分支>`（**PR 用 `--delete-branch` 合并会自动删远端，但本地分支和 remote-tracking 引用仍要手动清；中断/手动合并的更要补删**）。
     - `git remote prune origin` 清理失效的 remote-tracking 引用。
     - 自检：`git worktree list`（只剩 main + 别人的）、`git branch -a`（无自己的残留）、`ps aux | grep botbattle`（无自己的进程）、`ss -tlnp`（自己的端口已释放）、`ls scripts/`（无自己的临时脚本残留）。
     - **任务被打断/会话重启时，恢复后第一件事是盘点并清理上一轮可能遗留的脏产物**（worktree/进程/分支/临时脚本），不要直接开新工作堆在上面。
- **提交前跑测试**：`pytest`（从仓库根目录），前端改了再 `npm run build`。
- **改动须同步三处**（提交前自检）：
  1. **测试**：有功能/行为变更 → 在 `bzplat/backend/tests/` 加/改测试用例，覆盖新逻辑与边界。
  2. **文档**：新增模块/接口/常量/行为 → 同步 `wiki/` 对应文档（必要时更新 `INDEX.md` 与 `AGENTS.md` 架构分层）。
  3. **记忆**（若当前会话环境提供 memory 能力）：非显而易见的项目约定/架构决策 → 写入记忆索引 + 单独 fact；**不以仓库内缺失 MEMORY.md 为阻塞**——测试与文档必须同步。
  不许只改代码不补测试/文档——功能交付缺测试或文档视为未完成。

## 文档规范（改代码必同步）

文档分三个落点，职责不重叠：

- **`doc/`** —— 面向**甲方/干系人/平台开发者**的交付与工程文档：6 份核心交付文档，另有专项与历史文档；入口 `doc/INDEX.md`。
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
- **测试/开发跳过验证码**：`export BZ_SKIP_CAPTCHA=1`（`_require_captcha` 直接放行，免验证码即可登录/注册——便于 GUI 自动化与端到端测试；**仅测试/开发环境开启，生产绝不设**）。与之对照 `BZ_TEST_CAPTCHA=1` 仍走验证码流程，只是 `/api/auth/captcha` 响应额外返回 `answer` 便于脚本读取。
- **端到端冒烟**：`bash scripts/e2e_smoke.sh`。
- **测试种子账号**：`python scripts/seed_test_accounts.py`（建 tester1/tester2，各上传 holdem/gomoku/pencil 样例 Bot；幂等，便于对战/人类对战测试）。
- **改完代码必须 rebuild + restart**：`bash scripts/rebuild.sh`（`npm run build` → `platform-ctl.sh restart`）。前端产物（`bzplat/frontend/dist`）由后端 StaticFiles 托管、后端代码由运行进程加载——不 rebuild+restart 代码不会生效（常见症状：新路由 405 Method Not Allowed）。
- **worktree 前端独立预览**（开发期，不碰主服务 50380）：先在 worktree 起独立后端 `cd .worktrees/<分支> && python -m bzplat.backend.cli serve --port 50381`，再 `BZ_API_TARGET=http://127.0.0.1:50381 npm run dev`（vite dev server，proxy 到 worktree 后端）。详见上方「worktree 隔离工作流」。
- **日志**：`logs/app.log`（`logging_config.setup_logging`，统一格式 `时间 级别 [模块] 消息`）。排查执行队列/自动排位生产、对局、Bot 崩溃和 WS 问题在此；admin「日志」Tab 可网页查看与过滤。bot EOF 会附带 stderr 末尾。

## 关键约束（容易踩坑）

- **Python 包名必须是 `bzplat`，绝不能叫 `platform`**（会遮蔽标准库 `platform`）。所有 import 用绝对路径 `from bzplat.backend... import ...`。
- **常量按职责集中**：状态码、对局类型、`REGISTERED_ENGINES`、`VALID_GAME_IDS`、`VALID_RUNTIME_MODES`（traditional/longrunning）及历史 `platform_settings` 键名集中在 `bzplat/backend/store/schema.py`；生产运行参数集中在 `bzplat/backend/runtime/config.py`，资源硬顶及机器 ceiling 计算集中在 `runtime/limits.py`。禁止在消费者中散落同义字面量。
- **后端禁止 `print()`**：统一用 `logging.getLogger(__name__)`（全仓 10+ 模块均如此）。
- **代码持有的运行参数**（admin 不可修改）：`runtime/config.py` 按 8 vCPU / 16 GiB 基准固定全站对局并发硬顶 2、全站 sandbox capacity 4、action timeout、全局执行 aging/用户上限、自动排位 bootstrap 目标、公开排名资格、赛事 scheduler、人类对战及 `FULL_RR_MAX_N=12`；每个 job 仍占 1 match slot，赛事共享份额 1 只是混排公平门禁，不是额外容量。自动排位只是 `source=auto` producer，仅 `execution_control.auto_enabled` 管理员总开关可变，公平策略/队列长度/退避不是运行时参数。`runtime/limits.py` 以追加式历史 registry 管理 Docker 资源档位：日常节能/自动排位/人机 Bot 侧及上传预检使用每 Bot `1 CPU / 512 MiB`，锦标赛固定每 Bot `2 CPU / 2 GiB`，`remote_local`/human 不占平台沙箱；execution job 入队时冻结环境、档位版本与资源向量，claim/Match/runner 不得降档或改绑到当前同名规格。最重两场赛事共 8 CPU / 8 GiB；队列外上传预检可短时再占 1 CPU / 512 MiB，因此双槽是饱和上限而非严格 CPU 无超售/低延迟保证。主机准入再取进程 affinity、逻辑 CPU、cgroup 祖先配额、物理内存与 cgroup 内存上限的共同最小值，显式注入只能收紧，不能把硬顶放大到 2 以上。Bot 文件上限固定 100 MiB。全员单/双循环阶段可设 `allow_large_round_robin` 旁路，但只允许白名单内置决赛模板如 `holdem_final_ranked`。

## 架构分层（编辑时切勿越界）

```
contests/   赛制：templates(阶段模板+计分) → stages(对阵生成) → manager(阶段状态机) → ranking(正式名次/破同分) + scheduler(时间调度器，到点自动推进阶段)；presentation(逐阶段排名/晋级读模型)；showcase/showcase_seed(长期只读演示快照及真实裁判数据生成)
matches/    编排：execution_queue(全来源持久 job/attempt、双资源 claim、唯一 dispatcher、恢复/公开投影)
            + orchestrator(只启动已 claim attempt、SSE/评分/判胜/人类对战) + runner(起Bot进程,按game_id路由)
            + result_contract(持久化结果唯一 builder：rounds_played/deltas/normalized_delta)
            人类对战：orchestrator.challenge_human/_run_human_match + runner.run_bot_vs_human
            （人类侧经 _human_turns Future + WebSocket /api/matches/{id}/play 回传落子；与其他来源共享
            全局 match slots，固定占 1 slot + 1 sandbox unit，不计 Glicko）
            评分副作用：_apply_ratings 通过 match_rating_settlements 对每场 match 恰好一次结算，
            在同一事务更新双方 ratings + rating_history（评分趋势）+ pair_stats；启动时补算 completed 未结算场次
            通知副作用：对局完成（非 contest）经 orch.notifier.notify_both_owners 通知双方 owner
communications/ 平台通信真相：conversation/participant/message + delivery 异步投影；用户/admin 收发箱、固定快照广播、
            Bug 反馈/诊断白名单/图片附件；DeliveryWorker 在 main lifespan 批量展开广播并异步 SMTP 重试
notifications/ 旧业务门面：NotificationManager 全部委托 communications；notifications 表仅作旧 API 兼容投影，
            notification_prefs 继续决定普通通知是否排队邮件，业务请求不得直接 SMTP
            经验/等级：award_xp 在对局完成/赛事报名/评论/被关注时触发（users.xp/level/last_active_at）
games/      游戏注册表（赛制/编排契约解耦的单一入口）：base.py(GameSpec 接口 + GameRegistry 单例
            + MatchResult/RoundResult 平台契约基类，仅类型提示/测试用) + __init__.py(注册表
            单例 + run_session/normalize_game_id/preflight_bot/default_match_config/GAME_LABELS
            等模块级便捷函数) + _board_protocol.py(棋类共享行协议唯一实现，随公开裁判源码提供；
            gomoku/pencil 的 protocol.py 各自只导出本游戏 API)
            + 每游戏集中放置的子包 games/<game>/：<game>_judge.py(纯裁判=游戏规则，0 平台依赖，可独立审计/复用)
            + engine.py(适配层：裁判↔平台协议桥接，调 decide→驱动裁判→emit 事件) + protocol.py(行协议)
            + result.py(结果，独立定义不共享基类) + templates.py(赛事模板)
            + spec.py(装配 GameSpec)。GameSpec 集中声明一款游戏的全部固有属性。
            三层分离：**裁判**(<game>_judge.py，纯游戏规则/0 依赖) ↔ **适配层**(engine.py Session，平台协议桥接) ↔ **平台层**(spec/protocol/orchestrator/runner/FE)。
            holdem 的 Card 也在裁判模块（holdem_judge.py）——cards.py 已删。
            通用层经 registry.get(game_id) 取 spec 调用其能力，**禁止 if game_id== 分支**
            新增游戏 = 建 games/<game>/ 包 + 注册一行 + schema 加一项
            站点配置：GET /api/site/info
runtime/    沙箱与代码配置：config.py(生产运行参数唯一真相源)+ Linux x86_64 ELF BinaryRunner(docker/local) + limits(资源硬顶/机器 ceiling)；生产只连接本机 canonical Docker socket，execution/preflight 共享 supervisor 与跨进程 launch flock，create 先持久化 token/host-boot journal，再按 instance/job/attempt/slot/launch label 精确清理；PE/Mach-O/ARM64/脚本在上传时拒绝；Docker 镜像在 Bot 计时前完成 linux/amd64 检查/拉取，实际运行固定 `--pull=never --entrypoint /app/bot`
store/      SQLite + schema.py(常量唯一来源；fresh 实体 game_id 必填且无 DB 默认值) + execution.py(通用 execution_jobs/attempts/control、公平 producer、原子 claim/恢复)；matches 拆每游戏表（match_config+result 双 JSON 列，游戏无关）+ matches_index + ratings per-game（原始分差累计列 delta_total）
api_routes  接口：REST + SSE(观赛 /events) + WebSocket(人类对战 /play)；用户搜索 /api/users；用户主页 /api/users/{name}/{profile,bots}；全局搜索 /api/search；admin 日志 /api/admin/logs
auth/       认证 + 资料编辑：PUT /api/auth/profile（display_name/bio）+ POST /api/auth/avatar（本地 avatars/ 托管）
logging     统一日志：logging_config.setup_logging（logs/app.log，含 bot stderr 捕获），cli serve 接入
store/      自动排位：仅作为 `source=auto` producer 写入全局执行队列；每个 owner/game 只消费当前唯一 `is_ranked` 排位代表，游戏/lane/owner/pair/座位轮转与永久 decision 审计不形成第二套 admission、dispatcher 或物理 fence，唯一开关是 `execution_control.auto_enabled`
```

**前端架构（bzplat/frontend，React 19 + Vite 8 + Tailwind v4 + shadcn/ui）**：
```
src/index.css              设计 token：shadcn v4 OKLCH 双主题（:root 浅 / .dark 暗）emerald 品牌色系 + @theme inline 桥接
src/components/ui/         共享 UI 原语库（shadcn new-york：Button/Input/Card/Table/Tabs/Badge/Dialog/DropdownMenu/Select/Command/Popover/Tooltip/Slider/Switch/Separator/Sheet/Skeleton/Sonner/Avatar/Label/ScrollArea/MetricCard/Chart...）—— 全项目唯一组件抽象层
src/components/ui/status.tsx   EmptyState/Loading/ErrorMsg/RefreshBtn/StatusBadge（前台+管理端共用）
src/components/ui/select.tsx   shadcn Select（Radix）—— 全站下拉框唯一实现，禁裸用原生 <select>
src/components/shell/      全局 Shell：AppShell（lg+ 侧栏——登录与访客均显示；auth 页除外；窄屏顶栏含登录注册 + 导航 + 页脚）+ nav-config + GlobalSearch（Cmd+K Command 面板）
src/games/                 前端游戏注册表：GameViewSpec 集中声明 reducer/canvas（含交互 canvas 的 keyboardPicks 合法动作）、胜者/事件描述、humanPlay 动作控件与唯一 WS 信封（含 request 驱动的画布启停/行动标签）、replay HUD/摘要/进度/分段导航；页面不得 import 具体游戏 ViewModel
theme-provider/toggle      next-themes 暗色（class 策略，light 默认 + system）+ 太阳/月亮切换
src/pages/                 顶层路由全部用 React.lazy 代码分割（每页独立 chunk，recharts 等重依赖隔离）
路径别名 @/ → src/          新代码一律用 @/，禁相对路径；图标统一 lucide-react（无 emoji）
```
改前端务必遵循 [doc/DESIGN.md](doc/DESIGN.md) §5 前端架构：用 `@/components/ui/*` + 语义 token（bg-background/text-primary 等），不裸 hex 不硬编码 slate/brand 颜色。
**下拉框统一规范**（硬约束）：所有下拉框一律用 `@/components/ui/select`（shadcn Radix Select）+ `SelectTrigger/SelectValue/SelectContent/SelectItem`，**禁止裸用原生 `<select>`**（跨设备/浏览器展开样式不统一）。迁移注意 4 点：
1. 受控 API：`<Select value onValueChange>`（非 `onChange(e.target.value)`）。
2. **空值哨兵**：表"全部/不过滤"的空 value `''` 不能直接传 Radix（`value=""` 被当未选/placeholder）——统一用哨兵 `'all'`：`value={x || 'all'}` + `onValueChange={(v) => setX(v === 'all' ? '' : v)}`。
3. **number value 转 string**：Radix value 只接受 string，`speedIdx`/座位号等 number 需 `value={String(n)}` + `onValueChange={(v) => setN(Number(v))}`；动态实体 id（number）的 `<SelectItem value={String(id)}>`。
4. **label 包裹**：SelectTrigger 是 `<button>` 不支持 `htmlFor`——表单内用 `<div className="space-y-1.5"><Label>…</Label><Select>…</Select></div>`；inline 行内用 `<div className="flex items-center gap-2"><span>…</span><Select>…</Select></div>`。

**表单控件统一规范**（硬约束——禁止裸用以下浏览器原生控件，跨设备/浏览器渲染不一致）：
- **确认对话框**：禁止原生 `confirm()`（阻塞主线程 + OS 样式）。用 `@/hooks/use-confirm` 的 `useConfirm()`：`const [confirm, dialog] = useConfirm()` → `if (!await confirm({ title, desc, danger: true })) return` → 组件 JSX 末尾渲染 `{dialog}`。删除/中止/移除等危险操作设 `danger: true`（红色按钮）。
- **操作成功提示**：禁止原生 `alert()`。用 `import { toast } from 'sonner'` → `toast.success('...')`（Toaster 已挂在 App.tsx，非阻塞、自动消失、跨设备一致）。
- **滑块**：禁止原生 `<input type="range">`。用 `@/components/ui/slider`（Radix Slider）：`<Slider value={[n]} onValueChange={(v)=>setN(v[0])} min max step disabled />`（单值用数组包裹）。
- **开关**：禁止原生 `<input type="checkbox">`（布尔开关语义）。用 `@/components/ui/switch`（Radix Switch）：`<Switch checked onCheckedChange={setBool} />`。
- **tooltip**：禁止原生 `title=`（触屏/移动端不可用）。用 `@/components/ui/tooltip`（Radix Tooltip，`TooltipProvider` 已挂 App.tsx 顶层）：`<Tooltip><TooltipTrigger asChild><X/></TooltipTrigger><TooltipContent>提示</TooltipContent></Tooltip>`。
- **number input spinner**：`@/components/ui/input` 已统一隐藏跨浏览器 spinner（`appearance-none` + webkit spin button 隐藏）；admin 裸 `<input>` 用 `pages/admin/ui.tsx` 导出的共享 `inp` 常量（含 spinner 隐藏），不内联 className。

**核心游戏契约层**（赛制/编排主流程经统一契约接入游戏；违反契约会在运行时崩）：
- **结果契约**：各游戏 `result.py` 独立定义裁判鸭子类型，产出 `winners`(座位号,空=平局) + `deltas`(长2零和)；编排层不触碰扑克 pot/board/holes 或棋盘细节。平台持久化/API 的公共结果则只允许 `rounds_played`、`deltas`、`normalized_delta` 三字段，由 `matches/result_contract.py` 唯一构造；复式可额外带 `legs`，技术终局可额外带有界故障摘要，但不得覆盖公共字段。Holdem 的 `normalized_delta` 是整场筹码差除以大盲，不是每 100 手统计量；复式 `rounds_played` 累加全部 leg（两条固定 70 手即 140）。Holdem 多手权威胜者是按累计净筹码计算的 `result.winner`；原始 engine `match_end.winner=null` 不得覆盖它。**测试守护**：`tests/test_result_contract.py` 与 runtime/迁移回归守护该契约。
- **GameSpec 接口**（`games/base.py`）：每款游戏须声明全部字段——`game_id`/`label`、`ruleset_id`/`protocol_version`/`rating_pool_id`、`session_factory`/`protocol`、`default_match_params`/`validate_match_params`、`normalize_delta`/`progress_from_events`/`eta_for_match`、`templates`/`default_scoring`、`code_path`/`summary`、`source_files`/`shared_source_files`、`preflight_check`、`build_match_plan`、`time_budget_per_side`、可选 `record_exporter`。这些字段均有生产消费者；禁止添加仅作说明但无人读取的契约字段。`normalize_delta` 把座位 0 原始分差换算为本游戏展示单位；`progress_from_events` 供无引擎结果的技术终局计算已完成轮数，通用层不得按游戏名计数；`record_exporter=None` 表示没有稳定单场导出格式，通用 `/api/matches/{id}/record` 只按能力调用，并只传公开 match 白名单、canonical public replay 与快照时间。`ProtocolSpec.validate_response_payload` 只校验从唯一标准信封提取出的 `response` 形状/类型；格式正确但规则非法的动作必须留给裁判。传输层要求顶层对象包含 `response`，只消费并保存该字段；可选顶层 `debug` 仅在正式 Bot-vs-Bot 终局后经限额、清洗、脱敏进入独立私有 sidecar，绝不进入 `responses[]`、游戏请求、result、公共 replay/SSE/WS 或日志，预检直接丢弃；其他额外顶层字段忽略。顶层整数、裸坐标和缺少 `response` 的旧 `{a}` 仍拒绝。LongRunning 缺失精确握手即技术负，绝不回退；上传预检须按所选 runtime_mode 使用与正式首回合相同的信封和握手，Holdem 首请求的 `max_hand` 固定为 70。`source_files` 是游戏包内公开源码白名单；`shared_source_files` 声明必须同时公开的 games 包根目录共享实现。`build_match_plan` 承载 duplicate 多 leg 计划；`time_budget_per_side` 是每方累计棋钟，`None` 表示使用通用单步超时，Gomoku/Pencil 固定 `900.0`。游戏规则全部使用每游戏代码常量：Holdem 固定 70 手/20000 筹码/50-100 盲注，Gomoku 固定 15×15 + 26 种指定开局 + 三手交换 + **五手二打（开局 v2 wire 继续发送 `n_range=[2,2]`，响应 `n` 与黑 5 候选数均固定为 2）** + 黑方禁手/每方 900 秒，Pencil 固定 N=6/每方 900 秒；admin、match_config 与直接 `run_session` 都不能覆盖，session_factory 对非内部 `rng`/`deal_sequence` 参数明确报错。规则变化但 wire 协议不变时也必须启用新的 `ruleset_id` 与 `rating_pool_id`，经停服 `game-rule-cutover` 归档旧评分池并保留历史 Bot 版本和回放，禁止把不同规则静默混入同一评分池。赛事阶段按 type 严格校验 allowed keys，未知/错拼/其他阶段字段一律拒绝。Bot 非法 JSON/信封/response 与超时首次发生即技术判负（`protocol_error`/`timeout`），平台故障仍 aborted 且不评分；human WebSocket 输入不得包装为 Bot 协议故障。持久化实体缺失或包含未知 `game_id` 时必须 fail closed，禁止猜成 Holdem；只有产品创建入口可明确提供默认游戏。通用层经 `registry.get(game_id)` 取 spec，**禁止 `if game_id==` 分支**；架构守护测试覆盖该约束。
- **公开数值排名**：每个 `(owner_id,game_id)` 至多一个 `bots.is_ranked=1` 排位代表，由 partial unique index 强制；首个通过预检并激活的 Bot 在空席时自动派遣，owner 可原子切换或退出，停用/版本更新不隐式改席位，历史 Rating/RD/history 不复制、不重置。`RANKING_MIN_RATED_MATCHES` 是当前代表公开排名资格唯一阈值，与 `AUTO_MATCH_BOOTSTRAP_TARGET_MATCHES` 的队列冷启动目标独立。排行榜与 Bot profile 只输出 Rating、RD、95% 置信区间、1-based 名次/百分位、计分场次、不同对手数、资格进度、变化量与胜负平等客观数据；未派遣或不足阈值的样本 `rank=null`，不参与公开名次。

**新增一款游戏的成本**（赛制/编排主流程不加游戏名分支）——checklist：
1. 建 `games/<game>/` 子包：`<game>_judge.py`(纯裁判=游戏规则，0 平台依赖) + `engine.py`(适配层：裁判↔平台协议桥接) + `protocol.py`(仅导出本游戏行协议 API) + `result.py`(独立结果，满足鸭子契约) + `templates.py`(赛事模板) + `spec.py`(装配 GameSpec，明确 `time_budget_per_side`；无累计棋钟用 `None`)。若提供稳定的单场公开记录，在游戏包内实现只消费公开投影的 exporter 并赋给 `record_exporter`；否则保持 `None`，不得由通用层猜格式。若复用 `games/_board_protocol.py`，须在 `shared_source_files` 声明以随公开裁判源码提供，且不得导出其他游戏的 builder。
2. `schema.py` 的 `REGISTERED_ENGINES`/`VALID_GAME_IDS` frozenset 各加该项；`Store._migrate()` 会按注册 ID 用同构模板自动建立 `matches_<game>` 表与索引，无需复制静态 DDL。
3. `games/__init__.py`：`registry.register(SPEC)` 一行（启动断言 schema 与注册表一致）。
4. 前端 `src/games/<game>/`：`index.ts` 装配 GameViewSpec（Board/kind/reduce + `winner`/`describeEvent` + `humanPlay` 动作控件与序列化；协议特殊回合经 `canPickBoard(request)`/`turnLabelForRequest(request)` 声明，禁止通用页判断游戏名 + `replay` HUD/摘要/进度/分段导航）+ `canvas.ts`（CanvasRenderer；需要键盘等价操作时以 `keyboardPicks(scene)` 暴露与 pointer pick 同源的合法动作，供键盘/读屏操作）+ `reducer.ts`（事件归约，自包含对标后端 engine.py；启用棋钟时消费 `time_used/time_out`）+ 所需的游戏专属 UI 文件，再在 `src/games/index.ts` 注册一行。规则参数已固定，`configFields` 已删除。`RawEvent`/人类动作/HUD 公共类型在 `src/games/base.ts`；`normalizeGameId` 只规整字符串，`findGame` 对未知 id 返回空并由页面显示 unsupported，禁止回退 Holdem。
5. **不得**反向：`games/<game>/` 不得 import `bzplat.backend.engine`/`_compat`（循环依赖，`test_import_cycles.py` 守护）；通用层（matches/contests/store/api_routes）不得 import 具体游戏模块（经注册表）。
6. 跑测试：`pytest`（含 `test_result_contract`/`test_import_cycles`/`test_game_registry`，时限行为加 runner 回归）+ `npm run build` + `npm run test:e2e`；`screenshot_verify.py` 仅作补充。

**引擎路由入口**：`games.registry.get(game_id)` 取 `GameSpec` → `spec.run_session(decide, **params)` 构造并运行该游戏 Session；`spec.protocol.dumps_request/loads_response/validate_response_payload/fail_response` 处理行协议。`matches/runner.py` 经 games 注册表路由，不再有 if-chain。

**人类 vs Bot**（`match_type=human`）：引擎 `decide(player_idx, request)` 每回合阻塞；`run_bot_vs_human` 把 bot 侧接 BinaryRunner、人类侧接一个等待 `asyncio.Future` 的协程。orchestrator 的 `_human_turns` 注册 pending 回合并广播 `your_turn`，WebSocket `/play` 收到游戏动作即 `resolve_human_turn`。人类对局与 manual/contest/auto 共用全局执行队列和 match slots，claim 后固定占 `1 match slot + 1 sandbox unit`；`human_action_timeout` 默认 120s 逐回合防挂机，**不计 Glicko**，人工/人机请求 per-user 同时活跃 ≤ 1。若 spec 定义累计棋钟，Bot 与真人两侧都计入各自总预算；Gomoku/Pencil 为每方 900s，runner 发出 `time_used/time_out`，与 120s 逐回合保护叠加，较早触发的限制生效。Gomoku 的人类固定座位 1，但棋色由其在三手交换阶段的选择决定。

**挑战对战**（统一入口）：挑战页一个入口，两个座位——座位 1（Gomoku 为开局提案方，棋色待交换决定）只能选 Bot；Bot-vs-Bot 时普通用户/组织者只能选自己的，admin 可选全站 active+runnable Bot，后端仍严格校验版本归属、完整性与游戏一致性。座位 2（Gomoku 为交换决策方）可选 Bot **或「我亲自上场」（人类，`human_seat=1`）**。座位 1 vs 座位 2 都选 Bot → `POST /api/matches/challenge`（`my_bot_id`/`opponent_bot_id` + 可选版本 `my_bot_version_id`/`opponent_bot_version_id`，**自博弈允许**——同 bot 同/不同版本均可）；座位 2 = 人类 → `POST /api/matches/human`（`bot_id`=座位1 bot，`human_seat=1` 固定）。挑战选择器不隐藏练习 Bot；只有不同 owner 且双方都是各自 owner/game 当前 `is_ranked` 代表时计入平台排行，任一未派遣 Bot 固定 `rating_reason=ranked_bot_not_selected`。两个 POST 都返回 **HTTP 202 的持久 execution request**（`public_id`、排队位置/双容量/动态 ETA），不是立即返回 Match；前端持久化 `public_id` 并轮询 `GET /api/execution-requests/{public_id}`，只有 claim 后出现 `match_id` 才跳转对局。本人可 `DELETE` 取消 manual/human；可重试的 `interrupted` 通过 `POST .../retry`（202）把同一 job 重新排队，下次 claim 才创建新的不可复活 attempt，旧 Match 保留为不可变审计。显式版本或当前激活版本在 job 创建时冻结，claim 时复制到 `match_config._bot_a/b_version_id`；赛事版本来自 pairing 快照。排队期间上传/回滚不改变 runner 路径；排位代表切换会原子取消旧代表尚未 claim 的计分请求，claim 还会复核冻结双方仍是代表。无 `bot_versions` 行的 legacy Bot 才回退 `bots.binary_path`。`GET /api/bots/{id}/versions` 对非 owner 返回脱敏版本列表。

**座位编号约定**：**展示层从 1 开始**（座位 1/2），**内部 0-indexed**（后端 `winner`/`human_seat` 为 0/1，DB CHECK `winner IN (0,1)`）。前端显示 `+1`（Challenge/HumanPlay/MatchViewer/match-seats/canvas 共 7 处）。

**赛制阶段状态机**：`draft→open→published→running→(rest)→finished`。`ContestManager.maybe_finish` 是对局完成回调入口，负责瑞士补轮 / 淘汰晋级 / 休息期换 Bot / 进入下一阶段。`published` 是「排期已发布、等待开赛」中间态（报名截止→出排期→到点开打的两阶段）；`starts_at=NULL` 明确表示等待组织者手动开始，任何 scheduler/reconcile 路径都不得偷换为立即开赛。`ContestScheduler`（`contests/scheduler.py`，挂 main.py lifespan）后台周期扫描赛事 `*_at` 字段，到点自动推进阶段（开放报名/截止报名出排期/到点 enqueue pairing/rest 恢复）；组织者手动按钮始终可提前触发。逐场排期：运行态 `contest_pairings.scheduled_at=NULL` 才表示立即可排队，published 还必须先通过赛事级 `starts_at` 闸门。赛事只把 pairing 作为 `source=contest` job 写入全局执行队列；Match 及 replay/policy 只在 claim 时创建并原子绑定 pairing，其余 pairing 保持 `pending + match_id=NULL`。单场完成立即回写 pairing 并补下一条可排 job。新阶段首批 pairing（版本快照/bye/排期）与 `current_stage_idx/status` 必须经 Store 单事务批量提交；正式榜清旧/全量写入/`official_results_ready=1` 也必须同事务，启动对账负责补算 `finished+ready=0`。

**组织者实名 + 导出**：`require_real_name` 赛事只允许参赛者本人经 `/register` 报名，并在报名时由 entry 冻结 `real_name/phone/school/student_id`、采集时间与来源；普通 organizer 不得代报名，admin 显式 override 必须写无 PII 审计。legacy entry 不伪造快照，只在授权私有读取中标为 `current_profile_legacy` 并回退当前资料。`contest_entries_named` 与私有导出在同一 SQL 行内 JOIN 赛事门禁：非实名赛即使组织者/admin 请求也返回零 PII；详情继续返回顶层 `is_organizer`，`my_entry` 使用正向白名单。公开 `/official-results` JSON/CSV 永不含 PII。私有 `GET /api/contests/{id}/export?format=csv` 由组织者/admin gated；无 `schema` 的 16 列 CSV v1 保持兼容，`schema=2` 提供 29 列双语表头、稳定 entry/user/Bot ID、显示名、身份来源和阶段/成绩状态（UTF-8 BOM，文本与公式注入安全）。前端赛程：BracketTree（SVG 连接线，`bracket_slot//2` 拓扑）+ ScheduleTable（一览表）+ 阶段 Tab 显示中文标签 + 进度。

## 动手前必读文档

- `wiki/PROTOCOL.md` —— **唯一现行通信协议**（严格信封、两种进程生命周期、response payload、握手与故障语义）。
- `wiki/BOT_DEV.md` —— Bot 开发指南（编译、上传、调试、运行模式选择）。
- `wiki/INDEX.md` —— 文档总入口（固定 70 手 / 15×15 / N=6+900 秒，以及统一信封）。
- `contracts/` —— 协议 JSON Schema。
- `samples/` —— 三款游戏样例 Bot 源码。
