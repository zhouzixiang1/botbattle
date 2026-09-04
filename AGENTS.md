# AGENTS.md — botbattle

多游戏 Bot 线上对战平台（holdem 德州扑克 / gomoku 五子棋 / pencil 点格棋）：用户上传二进制 Bot，平台在沙箱中跑对局，提供观赛、回放、Glicko-2 排行榜与组织者赛事。

## 0. 规则作用域与优先级

- 本文件的精确文件名是根目录 **`AGENTS.md`**，对整个仓库始终生效；不要另建 `agent.md`、`AGENT.md`、大小写不同的副本或把同一套全仓规范再复制到 `.cursor/rules/`。若未来某个子目录增加更近的 `AGENTS.md`，只能补充该目录的专属规则，不得放宽本文件的安全、隔离、测试和发布门禁。
- 在系统、平台与开发者上位指令允许的范围内，仓库规则优先级依次为：用户本次明确要求 > 本文件 > `doc/` / `wiki/` 的领域细则 > 代码附近注释。发现互相矛盾或实现已漂移时，先停下来核对真实代码、测试和运行状态，再同步修正规则与文档；不得自行挑选最宽松解释。
- `AGENTS.md` 是开发规范唯一总入口；`doc/DEVELOPMENT.md`、`doc/DESIGN.md`、`doc/TESTING.md`、`doc/RUNTIME.md` 和 `wiki/PROTOCOL.md` 是对应领域的展开说明。这里记录必须执行的流程和不变量，不复制会频繁漂移的历史测试数字或临时发布状态。
- 所有“已完成”“已通过”“已部署”结论必须来自当前 checkout、当前命令和当前运行时证据。旧会话、旧提交、缓存报告或口头百分比只能作线索，不能作本次交付证明。

## 1. 强制开发生命周期

产生仓库变更的实现任务下面 9 个阶段缺一不可。纯只读诊断至少执行 §1.1–1.2 和证据化交付，不得为了满足流程擅自创建提交、PR 或生产写入；若诊断需要运行测试/构建/服务或生成隔离数据，则进入独立 worktree 并遵守 §1.3–1.6 与 §1.9，但不执行提交/PR/部署。诊断一旦产生仓库修改，就转为完整实现流程。

### 1.1 开工或恢复：先盘点真实现场

1. 从主目录只读执行并记录：
   ```bash
   git worktree list --porcelain
   git branch -a --no-color
   git status --short --branch
   git rev-parse HEAD && git rev-parse origin/main
   ps -eo pid,ppid,etime,args | grep -E 'bzplat.backend.cli serve|vite|pytest|playwright'
   ss -tlnp | grep -E ':(5038[0-9]|517[0-9])\b'
   ```
2. 产生仓库变更或必须判断远端新鲜度时，先运行 `git fetch --prune origin`，再要求主目录的 tracked 文件与 index 干净且 `main` 与 `origin/main` 同步。纯只读诊断可不刷新共享 remote-tracking refs，但必须注明结论基于当前本地 refs。既存未跟踪冷备、运行产物或别人留下的目录不等于可删除垃圾，全部原样保留。若 tracked/index 已脏，先查明归属，不得 `reset --hard`、`checkout --`、覆盖或顺手提交。
3. 非自己创建的 worktree、分支、进程、端口和未提交改动都属于别人。绝不删除、修改、提交、合并或终止；不确定归属时先询问。
4. 任务被打断、上下文压缩或会话重启后，第一件事仍是重新盘点。磁盘、Git、进程、端口、数据库和测试证据优先于旧进度描述。

### 1.2 明确问题：沿调用链理解后再改

1. 先把用户目标拆成可验收行为、明确不做项、风险和可能影响的数据/接口/运行时；只有真正改变产品方向的歧义才提问，其余在不越界的前提下自主推进。
2. 搜索优先使用 `rg` / `rg --files`。沿“入口/API/页面 → manager/service → Store/事务 → schema/运行时 → 读模型/前端 → 测试/文档”追到权威实现，复现或取得证据后再动手。禁止看到表层报错就加特判。
3. 改动前读取目标目录、调用方、数据模型、既有回归和本文件相应契约。修复应落在共享真相源，而不是让多个消费者各自猜测。
4. 诊断请求默认只读并说明根因，不擅自实现；实现请求则完成代码、测试、文档、审查和交付门禁。任何生产写入、外部消息、部署、删除或权限扩张都必须在用户授权范围内。

### 1.3 建立独立 worktree 与分支

1. 每个任务使用独立分支、worktree、数据库副本、端口、instance key 和运行产物目录。分支按性质使用 `feat/`、`fix/`、`refactor/`、`docs/`、`test/` 或 `chore/` 前缀，名称须能唯一对应任务：
   ```bash
   git worktree add .worktrees/<任务名> -b <类型>/<任务名> main
   cp /home/zzx/project/botbattle/botzone.db .worktrees/<任务名>/botzone.db
   ```
2. 数据库必须是 `cp` 得到的不同 inode 普通文件，禁止软链接、bind mount 或指回主库。复制时记录来源提交、时间、路径和数据库影响评估；副本只是复制时刻快照，不能假定一直等于当前主库。
3. 主目录 `/home/zzx/project/botbattle` 只运行 `main` 的 `50380`、主数据库和主源码。所有开发编辑、依赖安装、测试、构建和 QA 服务均在本任务 worktree 内进行。
4. 即便是纯文档任务也要在 worktree 修改；数据库副本只作隔离基线，不需要打开。若磁盘或权限使复制不可行，先报告阻塞，不能退回主库开发。

### 1.4 数据库、端口与运行时隔离

1. `/home/zzx/project/botbattle/botzone.db` 是线上只读真相源。测试、迁移、造数、修复、清理和性能演练只能写 worktree 副本或由它创建的临时隔离 DB；主库写操作必须得到用户对精确目标和动作的明确授权，并走维护、冷备、预演、验收和回滚流程。
2. 启动 QA 后端必须从 worktree CWD 锁定绝对数据库路径、唯一 instance key、canonical Docker socket 和非 50380 端口：
   ```bash
   cd /home/zzx/project/botbattle/.worktrees/<任务名>
   BZ_DB_PATH="$PWD/botzone.db" \
   BZ_INSTANCE_KEY=qa-<唯一小写标识> \
   BZ_DOCKER_HOST=unix:///var/run/docker.sock \
   BZ_QA_INSTANCE=1 \
   python -m bzplat.backend.cli serve --host 127.0.0.1 --port <50381-50389空闲端口>
   ```
3. 前端只代理本任务后端：`BZ_API_TARGET=http://127.0.0.1:<任务端口> npm run dev`。严禁代理到 50380；人机 WebSocket QA 还必须把 `BZ_PUBLIC_ORIGIN` 精确设为浏览器实际 origin。
4. 启动前用 `ss` 查空闲端口；进程 PID、CWD、命令、端口和日志路径都要记录。只停止自己启动且身份已核对的进程，禁止模糊 `pkill` 或跨 instance 清理 Docker。
5. `.env`、密码、令牌、cookie、真实 PII、Bot 私有调试和环境特有的数据库路径不得写入代码、测试快照、文档、日志或 PR；本文件与开发文档中用于阐明隔离边界的 canonical 主路径/占位路径除外。测试账号和验证码能力仅允许隔离 QA。

### 1.5 实现与协作

1. 变更保持最小、可回滚、单一职责；优先复用已有模块、Store 事务、组件、注册表和契约，不复制第二套状态机或同义常量。
2. 保护用户与其他 agent 的改动。文件修改使用精确 patch，提交前逐文件审阅 diff；禁止 `git reset --hard`、未经授权的 `git checkout --`、宽范围删除和把无关格式化混入功能提交。
3. 数据更新优先走正式 API、Manager 和 Store。不得用临时 SQL 绕过权限、CAS、审计、版本冻结或生命周期；一次性诊断/迁移脚本放 `/tmp`，用完删除。需要长期保留的运维脚本必须有文档、测试、fail-closed 路径和明确参数。
4. Schema 变更必须追加式、幂等、可从 fresh 与真实 legacy 形状升级；在同一事务里校验类型、身份、版本、拓扑和 CAS。禁止启动迁移静默重释历史业务数据，禁止把损坏值用 `bool()` / `int()` 等强转吞掉。
5. API/读模型使用正向白名单、共享严格解析器和一致 fail-closed 语义；未知、缺失、错类型、身份漂移或结果矛盾不得被默认值猜成正常。公开接口不泄露 PII、原始 result/events、私有 seed、文件路径或内部错误细节。
6. 前后端契约、详情/直播/列表/回放和生命周期必须共用同一权威语义；不可只修一个页面或让前端推断后端不知道的状态。
7. 多 agent 只用于边界清楚、可并行的子任务。同一任务共享 worktree 时必须预先分配互不重叠的文件所有权，公共文件由主负责人统一合并；审查 agent 默认只读。不同任务始终使用不同 worktree，禁止借用、清理或提交他人的工作区。
8. 非显而易见的架构约定需要同步测试、仓库文档和本文件。会话记忆只有在用户明确要求且当前环境允许时才更新；缺少记忆能力不能阻塞代码、测试和文档交付。

### 1.6 测试、浏览器验收与证据

1. 先跑最小复现和直接受影响测试，再扩到模块矩阵，最后按变更风险跑完整门禁。修测试只能修陈旧夹具/契约，不能为了变绿放宽正确的生产门禁。
2. 测试环境要显式、可复现：清除无关的 `BZ_*` 注入，使用本任务 DB/instance/端口/TMPDIR，记录完整命令、退出码、通过/失败/跳过数和警告。测试运行中不得编辑同一候选；代码变化后旧结果作废。
3. Web QA 必须实际打开隔离应用，覆盖要求的身份、路由、视口和交互，并检查页面、Console、Network、SSE/WebSocket、QA 后端日志与写目标。源码阅读、单元测试、截图或 `playwright --list` 不能替代真浏览器执行。
4. 选择器和 fixture 必须确定性、自包含，不依赖 spec 顺序、共享 QA 库恰好有历史行、固定 sleep 或宽泛错误白名单。只允许对精确 method + pathname + 动态 ID + 浏览器错误文本的已证明客户端取消做有界处理。
5. 长测结束或被中止后必须确认无残留 pytest/Playwright/服务进程；失败要保留 nodeid、关键 traceback、日志与 artifact，并区分生产缺陷、测试夹具、环境污染和已知外部写入。

### 1.7 提交、审查与 Pull Request

1. 提交前检查 `git status --short`、`git diff --stat`、完整 diff、未跟踪文件、敏感信息、运行产物、`git diff --check` 和 `git diff --cached --check`。只纳入本任务产品文件；数据库、备份、日志、上传、构建目录、截图报告、DB 邻接 flock、PID/运行时锁绝不提交。`package-lock.json` 等依赖锁文件是产品文件，依赖变化时必须与 manifest 一起提交。
2. 提交应按可审查主题组织，消息说明结果而非过程。禁止直接在 `main` 提交、直接 push `main`、把本地 feature 分支 merge 到 `main` 或绕过评审。§1.8 在 PR 已合并后把 main **仅 fast-forward 到已 fetch 且已审阅的 `origin/main` 精确 SHA**，属于受控发布推进，不是本地合并开发分支。
3. 所有合并走 GitHub PR：`push feature branch → gh pr create → 自动/人工审查 → 修复 finding → 重跑受影响门禁 → 合并`。若仓库配置了 GitHub checks，必须用 `gh pr checks` 确认全部通过；没有 checks 时如实记录“未配置 CI”，以本文件的本地验证矩阵和独立 review 为门禁。安全、权限、PII、事务、迁移、调度、运行时、协议和公开契约变更必须安排独立只读审查。
4. PR 描述至少包含：问题与用户影响、实现摘要、明确不做项、依赖/数据库影响、测试与浏览器证据、部署步骤、回滚配对、兼容边界和仍待验证项。没有执行的门禁写“未运行/待验证”，不得借用旧证据。
5. 审查顺序固定为：功能正确性 → 数据一致性/并发/幂等/恢复 → 安全/权限/PII → API 与历史兼容 → 查询/资源性能 → UI/无障碍 → 测试/文档。finding 必须有可复现调用链、严重级别和具体修法；修复后由独立审查确认。未解决 P0/P1/P2、测试红、diff-check 红、候选漂移或未知运行产物都是合并 No-Go。

### 1.8 合并后发布

1. “纯文档/规则”必须按主目录**实际待推进的完整 fast-forward 区间**判定，不能只看本 PR。先保持 main tracked/index 干净，只执行一次 `git fetch --prune origin`，冻结 `base_sha=$(git rev-parse HEAD)` 与 `target_sha=$(git rev-parse origin/main)`，验证 base 是 target 的祖先，并审阅 `git log --oneline "$base_sha..$target_sha"`、`git diff --name-status "$base_sha..$target_sha"`（有疑点再逐提交审阅）。只有整个区间都不影响运行代码、依赖、静态产物、配置或 schema 时，才可用不联网的 `git merge --ff-only "$target_sha"` 精确推进，并核对新 HEAD 等于 target；严禁在审阅后执行会再次 fetch 的普通 `git pull`。不为形式打断线上对局，不执行 rebuild/restart，并记录“无需运行时发布”。远端随后新增的提交留待下一轮审阅；区间只要夹带任何运行时变更或无法证明纯文档，就必须在推进工作树前转入下述完整计划部署。
2. 后端、前端、依赖、配置或 schema 变更在旧 release 运行期间不得先 pull 覆盖工作目录，必须走计划部署，禁止直接 restart 抢断对局：
   - 请求 deployment maintenance，确认 `running`，一次性开启 drain；
   - 轮询到 `maintenance.ready=true`，核对 active job、上传、Local AI lease、owned task、未跟踪 Match、Docker launch journal 和恢复任务全部静默；
   - 停服并核对 PID、50380 与本 instance 容器；保留邻接 flock 文件，确认主 DB 的 `-wal` / `-shm` / `-journal` sidecar 均不存在；
   - 制作不同 inode 的逐字节冷备，记录 release/DB SHA-256，并通过 `cmp`、`PRAGMA integrity_check`、`PRAGMA foreign_key_check`。该冷备封存只读、绝不由目标代码打开；
   - 涉及迁移时，从封存冷备再 `cp` 到第三个不同 inode 的临时演练 DB，只对该临时库做首次升级、二次幂等 reopen、schema/业务摘要核对和回滚预演，完成后回收演练库；
   - 到此才执行一次 `git fetch --prune origin`，冻结并审阅精确 `base_sha` / `target_sha`，核对 main tracked/index 状态与 fast-forward 关系，再用不联网的 `git merge --ff-only "$target_sha"` 推进并核对 HEAD；严禁普通 `git pull` 在停服窗夹带未审提交。若 `package.json` / `package-lock.json` 变化，在仍停服/drain 状态执行 `(cd bzplat/frontend && npm ci)`，只按 target SHA 对应 lock 安装并保存输出，禁止无关升级；
   - **Python 依赖变更是独立发布 No-Go 门**：当前仓库只有 `pyproject.toml` 的 `>=` 下限，生产 systemd 又固定使用 `.venv/bin/python`，不能靠原地 `pip install` 得到可复现回滚。此类 PR 必须同时引入并审核精确生产 lock/constraints、构建并验证并行的新 venv、更新受控启动指向，并保留旧 release + 旧 lock + 旧 venv 的原子切回路径；缺任一项不得部署，禁止修改正在服务的生产 `.venv`；
   - 执行 `bash scripts/rebuild.sh`，确认 health、版本、依赖、队列、关键 API/页面、日志和只读业务摘要；依赖安装/构建失败时保持 drain，不得恢复接单；
   - 验收后显式结束 maintenance 恢复 admission。自动排位保持关闭，除非用户另行授权开启。
3. 主库修复、规则代际 cutover、评分重建和不可逆运维必须遵循 `doc/RUNTIME.md` 的更严格停服/digest/CAS 门禁。旧代码 release、对应依赖 lock/venv 与匹配的迁移前冷备只能在仍处于 drain、尚未恢复 admission 的同一发布窗内成对回滚，禁止只回滚一边。恢复接单后若再发现问题，必须重新进入维护、先封存当前故障库并评估新增业务写入；未经用户对数据损失的明确授权，禁止自动恢复旧冷备。
4. 部署或会话中断后先重新读取 maintenance 与真实进程/端口/DB 状态，不能假定 drain 已解除。离线 apply 输出丢失时，只能按 `doc/RUNTIME.md` 使用同一冷备、同一 digest/cutover id 和已证明幂等的同一命令重试，不能猜测成功或换参数重跑。
5. 部署结果必须说明实际 release、备份路径、迁移/重建输出、烟测结果、maintenance/admission/auto 最终状态；任何一步不满足即保持 drain 或在上述安全窗口内回滚，不得带病恢复接单。

### 1.9 清理与交接

1. 按 PID 精确停止自己启动的后端、Vite、Playwright、pytest、造数和临时任务，随后重新查询进程与端口确认已退出；不要终止 main 50380 或别人的进程。
2. 合并并拉取 main 后，从主目录移除**自己的** worktree，删除自己的本地/远端分支并 prune：
   ```bash
   git worktree remove .worktrees/<任务名>
   git branch -D <类型>/<任务名>
   git push origin --delete <类型>/<任务名>  # 仅远端尚存在时
   git remote prune origin
   ```
3. 一次性脚本、worktree 数据库、上传/头像/日志、node_modules、dist、测试报告和本任务 PID/运行时锁随本任务清理；不得删除主目录冷备、DB 邻接 flock、主锁文件或其他 worktree 的任何产物。
4. 最终重新核对 worktree、分支、Git 状态、进程、端口和 `scripts/`。未完成或未合并的工作不得擅自销毁：保留 worktree 并准确交接分支、diff、进程、数据库副本、已跑测试和下一步。

## 2. 变更对应的最低验证矩阵

- **纯文档/规则**：相关链接与术语检查、被文档守护的定向 pytest、`git diff --check`；不需要启动服务或重启生产。
- **后端逻辑**：最小反例 + 受影响文件/模块矩阵 + 仓库根完整 `pytest`；另跑 `python -m compileall -q bzplat/backend` 与 `git diff --check`。
- **前端逻辑/UI**：`npm run test:unit`、`npm run build`、目标 Playwright；涉及用户主链路、响应式布局或发布候选时，再跑从目标提交静态收集出的完整三浏览器矩阵。
- **API/权限/隐私**：至少覆盖访客、普通用户、owner/组织者、admin 的正反例，响应字段白名单、横向越权、PII/路径/原始结果不泄漏以及 malformed 数据 fail-closed。
- **数据库/schema/迁移**：fresh DB、主库副本、典型旧 schema、二次 reopen/幂等、故障回滚、并发 CAS、`integrity_check=ok`、`foreign_key_check` 零行；绝不在主库试跑。
- **事务/调度/运行时**：成功、并发竞争、取消、进程重启、错误恢复、资源不足、Bot 互斥、身份/版本漂移与持久公平回归；必要时在隔离实例真实跑一局并核对 job/attempt/Match/日志/Docker 参数。
- **游戏规则/协议/评分**：纯裁判、engine、result contract、runner、Traditional/LongRunning、上传预检、历史回放、rating pool/cutover 与公开 Wiki 同步验证。
- **发布候选**：完整 `pytest`、前端 unit/build、目标提交完整 Playwright、隔离 `e2e_smoke.sh`、真实 Console/Network/WS/日志/多视口检查、迁移预演和部署只读烟测。少一项只能写“待验证”，不能写“已验收”。

测试数字会随仓库演进，最终以命令输出为准；不要在本文件硬编码易漂移的 passed 数或 spec 数。仓库当前没有单独的 lint 脚本，不得虚构 `npm run lint` 门禁。

## 3. 交付报告与完成定义

最终交付必须先给结果，再给证据，至少明确：

- 改了什么、为何在权威层修、哪些文件/接口/数据受影响；
- 数据库是只读、迁移、写业务数据还是无影响；生产主库是否完全未触碰；
- 实际运行的测试命令与结果，哪些未运行及原因；浏览器/运行时证据是否来自真实隔离实例；
- PR、review、merge、main pull、部署、烟测与清理各自的真实状态；
- 已知限制、兼容边界、回滚方法和需要用户决定的剩余事项。

只有同时满足以下条件，任务才可标为完成：

1. 用户要求与验收行为全部实现，没有用测试或文档掩盖产品缺口；
2. 代码位于正确架构层，安全/权限/事务/隐私/历史兼容门禁未放宽；
3. 行为变更已有边界回归，文档与对外语义已同步；
4. 风险对应的测试、构建、浏览器和迁移门禁全绿，候选在测试期间冻结；
5. diff 经过自审和独立审查，无未解决 P0/P1/P2，无敏感信息和运行产物；
6. 产生仓库变更的任务已通过 GitHub PR 合并；需要运行时发布的改动已安全部署并烟测，不需要发布的改动已明确说明。纯只读诊断则明确保持零仓库内容变更、零生产/外部写入、零 PR；若为复现生成过隔离数据库、构建产物或进程，还要如实列出并证明已清理；
7. 自己的 worktree、分支、进程、端口和临时产物已清理，main 及其他任务未被污染。

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

以下开发命令只允许在本任务 worktree 中执行：

```bash
# 必须先进入本任务 worktree
cd /home/zzx/project/botbattle/.worktrees/<任务名>

# 后端（Python ≥ 3.12；仅依赖首次安装或变更时创建私有 venv）
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'          # 装 bzplat 包 + pytest/httpx

# 前端（React 19 + Vite 8 + Tailwind v4，浅色默认 + 暗色双主题）
(cd bzplat/frontend && npm install && npm run build)
```

- **worktree Python**：新 worktree 默认没有 `.venv`。依赖不变时可从 worktree CWD 使用 `/home/zzx/project/botbattle/.venv/bin/python` 作为只读工具链；不得在任务中修改共享虚拟环境。依赖变更时在 worktree 创建自己的 `.venv` 并同时更新依赖声明/文档。
- **生产控制**：`scripts/platform-ctl.sh` 是 50380 唯一控制入口；对既有生产实例的 stop/restart 只能在 §1.8 maintenance 门禁内从主目录使用，`status`/`logs` 可随时只读调用，首次安装或故障恢复按 `doc/DEVELOPMENT.md` / `doc/RUNTIME.md` 对应 runbook 执行。禁止 raw `botzone serve` 绕过。`botzone create-admin` 是主库写操作，只能在用户对精确账号明确授权后执行。
- **测试**：`pytest`（`pyproject.toml` 设 `testpaths=["bzplat/backend/tests"]`，`pythonpath=["."]`），务必从本任务 worktree 根运行。
- **本地无 Docker 跑 ELF**：`export BZ_QA_INSTANCE=1 BZ_BOT_LOCAL=1`（`BinaryRunner` 退回本机 subprocess，仅隔离 QA 使用）。CLI `serve` 发现 `BZ_BOT_LOCAL`、`BZ_SKIP_CAPTCHA` 或 `BZ_TEST_CAPTCHA` 任一为真而 `BZ_QA_INSTANCE` 未启用时，必须在日志、数据库和运行时目录创建前拒绝启动；生产 `scripts/platform-ctl.sh` 无论是否误设 QA marker 都直接拒绝这三项测试开关。
- **测试/开发验证码开关**：隔离 QA 可在 `BZ_QA_INSTANCE=1` 下设置 `BZ_SKIP_CAPTCHA=1`（登录/注册跳过验证码）或 `BZ_TEST_CAPTCHA=1`（仍验证、但 `/api/auth/captcha` 额外返回 `answer`）。生产二者必须未设或为假。
- **端到端冒烟**：只从本任务 worktree 根运行 `bash scripts/e2e_smoke.sh`。
- **测试种子账号**：只在 worktree 根执行 `python scripts/seed_test_accounts.py --db "$PWD/botzone.db" --with-role-accounts`（建隔离角色与三游戏样例 Bot；幂等，便于对战/人类对战测试）。
- **运行时代码发布**：前端产物由后端 StaticFiles 托管、后端代码由运行进程加载，因此运行时代码要通过 `bash scripts/rebuild.sh` 才能生效；但生产必须先完成 §1.8 的 maintenance 排空，禁止在开发 worktree 或未排空的 main 直接运行该脚本。纯文档/规则改动无需 restart。
- **worktree 前端独立预览**：先严格按 §1.4 的完整环境变量模板启动隔离后端，再在 worktree 的 `bzplat/frontend` 执行 `BZ_API_TARGET=http://127.0.0.1:<任务端口> npm run dev`。禁止使用省略 `BZ_DB_PATH`、`BZ_INSTANCE_KEY`、`BZ_QA_INSTANCE` 的后端简写命令。
- **日志**：`logs/app.log`（`logging_config.setup_logging`，统一格式 `时间 级别 [模块] 消息`）。排查执行队列/自动排位生产、对局、Bot 崩溃和 WS 问题在此；admin「日志」Tab 可网页查看与过滤。bot EOF 会附带 stderr 末尾。

## 关键约束（容易踩坑）

- **Python 包名必须是 `bzplat`，绝不能叫 `platform`**（会遮蔽标准库 `platform`）。所有 import 用绝对路径 `from bzplat.backend... import ...`。
- **常量按职责集中**：状态码、对局类型、`REGISTERED_ENGINES`、`VALID_GAME_IDS`、`VALID_RUNTIME_MODES`（traditional/longrunning）及历史 `platform_settings` 键名集中在 `bzplat/backend/store/schema.py`；生产运行参数集中在 `bzplat/backend/runtime/config.py`，资源硬顶及机器 ceiling 计算集中在 `runtime/limits.py`。禁止在消费者中散落同义字面量。
- **后端生产代码禁止 `print()`**：统一用 `logging.getLogger(__name__)`。测试夹具、子进程样例和 CLI 明确面向 stdout 的机器可读输出可以使用 `print()`，但测试诊断优先用 assertion/capture，日志不得泄露敏感数据。
- **代码持有的运行参数**（admin 不可修改）：`runtime/config.py` 固定全站 **6 match slots / 12 sandbox units** 的代码硬顶，以及 action timeout、前台 execution aging/用户上限、自动排位 300 秒空闲与冷却门禁/单场单候选上限/bootstrap 目标、公开排名资格、赛事 scheduler 与人类对战参数；每个 job 固定占 1 match slot，赛事共享份额 1 只在 manual/human 与 contest 前台之间生效，不是额外容量。自动排位只是严格闲时的 `source=auto` 后台 producer，仅 `execution_control.auto_enabled` 管理员总开关可变；开启不等于立即运行，auto 不参与跨来源 aging、不计入前台 ETA，并在运行时保留至少 1 个 match slot。`runtime/limits.py` 以追加式历史 registry 管理 Docker 资源档位：日常节能/自动排位/人机 Bot 侧及上传预检使用每 Bot `1 CPU / 512 MiB`，锦标赛固定每 Bot `2 CPU / 2 GiB`，`remote_local`/human 不占平台沙箱；execution job 入队时冻结环境、档位版本以及 sandbox/CPU/内存资源向量，claim/Match/runner 不得降档或改绑到当前同名规格。claim 再按进程 affinity、逻辑 CPU、cgroup 祖先配额、物理内存与 cgroup 内存上限的共同最小预算逐维准入，因此可运行并发按 job 组合动态落在 1–6，六槽不等于六场最重赛事；显式注入只能收紧，不能放大 6/12 硬顶。任一非 human Bot 在全局最多参与一个 `starting/running/settling` job（同一 job 的自博弈仍只占一个 active job）。contest 公平轮转只发生在既有优先级排序中不跨 manual/human 行的连续 contest 队列段，顺序依据持久 `claimed_at`/attempt 历史，不能把低优先级赛事拉过前台边界。Bot 文件上限固定 100 MiB。全员及分组单/双循环均不设人数硬上限，完整 O(n²) 排期只扩展持久队列；历史 `allow_large_round_robin` 继续按严格布尔读取，但仅作兼容 no-op。

- **认证与 HTTP 边界**：浏览器登录只使用同源 HttpOnly `bz_session`，不得把 bearer 或完整用户/PII 写入 `localStorage`；跨标签只传播不含身份信息的随机 auth epoch，收到新代际的标签页须清空旧投影并经 `/api/auth/me` 对账，旧页面发起的私有动作不得直接落到新 cookie 身份。登出失败必须保留当前身份和页面，仅服务端 2xx 才清理内存态、广播代际并导航。非浏览器客户端仍可显式使用 Bearer。带 cookie 的 unsafe 请求必须精确匹配 canonical `BZ_PUBLIC_ORIGIN`，显式 Bearer 优先且不使用 ambient cookie 身份。所有携 Authorization 或 session cookie 的 `/api` 成功与错误响应都须 `private, no-store` 并合并 `Vary: Authorization, Cookie`；认证 JSON 64 KiB、其余写 API 1 MiB，Bot/反馈附件/头像 multipart 使用独立有界信封且按 ASGI chunk 累计。动态 ID 限流键必须规范化，另有单 IP 全局 API 桶和有界桶表；当前内存限流与 SSE/WS 配额只承诺单进程，部署多 worker 前必须改为共享后端。SPA 文件命中须在解析 symlink 后仍位于 `dist`，访问/审计字段必须单行转义、有界且不记录 query。

- **会话撤销与凭据消费**：停用账号必须在一个 `BEGIN IMMEDIATE` 内同时写 `is_active=0`、删除全部 session、撤销 Local AI identity 并释放 lease；登录发 session 在同类写锁内同时复核 active 与刚验证的精确 `password_hash` 代际。改密以旧 hash CAS 更新并在同一事务删除 sessions，旧密码登录不得在改密/重置撤销之后迟到签发。密码重置只绑定最新邮箱码，错误尝试以持久 CAS 计数并在预算耗尽后失效；验证码消费、更新密码和删除全部 session 必须同事务完成，任一步失败整体回滚。

- **实时连接配额与授权**：匿名观赛 SSE 单进程总数/单局/单 IP 分别硬顶 `64/32/8`；人类对战 WebSocket 在 Origin/query-token 门后、任何 session/Match 数据库读取前先按可信 peer 执行 `30/60s` 握手桶与全局 16 个 inflight，peer 桶最多 2048 项且饱和时 fail closed，缺 Cookie 不查询 session、无效 session 不查询 Match。鉴权后单进程总数/单局/单用户分别硬顶 `32/4/4`，且 user ID 必须是精确正整数。额度必须在 snapshot/队列分配前同步预留，所有错误、首个 SSE body 前断开、普通断开、终态、取消与 shutdown 都要在完整 response scope 中幂等释放；WS 超限用稳定 `1013 + connection_limit`。人类动作帧还须在 JSON 与任何数据库读取前执行 4 KiB 硬顶，并按同一用户跨连接及可信 peer IP 共用 `burst=10/refill=2s⁻¹` 的有界 4096 项 token 桶；消息超限/速率超限分别以 1009/1008 关闭，且每个已准入 frame 及 resolve 前仍复核最初 session、active 用户与 Match owner，撤销后不得提交动作。

- **本地 Bot 两阶段计时**：连接器必须协商当前 WebSocket 子协议；缺少版本能力的旧客户端在 durable/online registration 前拒绝，不能取得对局后才静默失败。正常回合先下发不含局面的有界 `prepare_turn`，客户端启动 Traditional 进程并回送强绑定 `prepared` 后，hub 才冻结游戏 deadline 并交付完整 `turn`；启动、平台锁/队列和输入准备不计游戏棋钟。响应或故障到达时在 hub 内冻结 elapsed，准备与决策重连分别沿用原 deadline，取消、超时和故障竞态不得泄漏 pending、内部 Future 或子进程。

- **闲时自动排位收口与迁移**：任一 manual/human/contest 前台成功入队/重试（尤其人类对局入队）会在同一 `BEGIN IMMEDIATE` 中取消 queued auto、让在途 auto 以 `auto_yield_foreground` 安全收口；真实赛事 guard 由 dispatcher 下一次 reconcile 事务做同样收口，auto claim 自身事务会重查 guard 以防穿透。Docker 物理启动再以 create intent 的 `BEGIN IMMEDIATE` 作为最终线性化边界：execution attempt 必须在写 intent 的同一事务内仍为当前 `starting/running` attempt 且 `cancel_requested=0`，而且 host-wide launch journal 必须先证明为 `idle`。前台/yield 先提交时，必须在 `docker create` 前拒绝启动并按普通任务取消路径清理，属于 benign cancellation，不得暂停 dispatcher；launch intent 先提交时，后到的 yield 必须沿 token/name/label/journal 做 exact cleanup，不能提前释放容量。单纯关闭管理员开关不抢占在途局。`auto_match_fair_state` 追加 `dispatch_policy_version/next_eligible_at/gate_reason` 三列以幂等升级且持久冷却；首次 `idle-only-v1` 对账取消遗留 queued auto、让在途 auto 以专用 `auto_idle_policy_cutover` 收口，并重新计 300 秒空闲窗，不得误记为有前台到达。

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
store/      自动排位：仅作为严格闲时的 `source=auto` 后台 producer 写入全局执行队列；前台与所有 active 槽清空并连续 300 秒后至多生成 1 个候选/运行 1 场，结束后重新冷却，auto 不参与跨来源 aging 或前台 ETA；每个 owner/game 只消费当前唯一 `is_ranked` 排位代表，游戏/lane/owner/pair/座位轮转与永久 decision 审计不形成第二套 dispatcher 或物理容量池，唯一开关是 `execution_control.auto_enabled`
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
路径别名 @/ → src/          跨目录/跨层 import 使用 @/；同目录内部可用相对路径；图标统一 lucide-react（无 emoji）
```
改前端务必遵循 [doc/DESIGN.md](doc/DESIGN.md) §5 前端架构：用 `@/components/ui/*` + 语义 token（bg-background/text-primary 等），不裸 hex 不硬编码 slate/brand 颜色。

**前端页面与无障碍规范**：新页面复用 `PageFrame → PageHeader → StickyToolbar/DataRegion`，全局 `<main>` 是默认唯一纵向滚动 owner；宽表只允许一个横向 scroll owner，长实体名/标识符复用 `EntityName`、`Identifier`、`OverflowText`。权限只增加操作和数据范围，不为访客/用户/组织者/admin 复制四套页面骨架。新增或重做 UI 至少验证 1440×900、390×844、浅/暗主题、键盘可达、触控目标不小于 44px、根级无横向溢出；避免与正文重复的 SummaryStrip、“数据概览”、步骤卡和模板化文案。TypeScript 必须继续通过 `npm run build` 所含的 strict、unused 与 side-effect import 检查。
**下拉框统一规范**（硬约束）：所有下拉框一律用 `@/components/ui/select`（shadcn Radix Select）+ `SelectTrigger/SelectValue/SelectContent/SelectItem`，**禁止裸用原生 `<select>`**（跨设备/浏览器展开样式不统一）。迁移注意 4 点：
1. 受控 API：`<Select value onValueChange>`（非 `onChange(e.target.value)`）。
2. **空值哨兵**：表"全部/不过滤"的空 value `''` 不能直接传 Radix（`value=""` 被当未选/placeholder）——统一用哨兵 `'all'`：`value={x || 'all'}` + `onValueChange={(v) => setX(v === 'all' ? '' : v)}`。
3. **number value 转 string**：Radix value 只接受 string，`speedIdx`/座位号等 number 需 `value={String(n)}` + `onValueChange={(v) => setN(Number(v))}`；动态实体 id（number）的 `<SelectItem value={String(id)}>`。
4. **label 包裹**：SelectTrigger 是 `<button>` 不支持 `htmlFor`——表单内用 `<div className="space-y-1.5"><Label>…</Label><Select>…</Select></div>`；inline 行内用 `<div className="flex items-center gap-2"><span>…</span><Select>…</Select></div>`。

**表单控件统一规范**（硬约束——禁止裸用以下浏览器原生控件，跨设备/浏览器渲染不一致）：
- **确认对话框**：禁止原生 `confirm()`（阻塞主线程 + OS 样式）。用 `@/hooks/use-confirm` 的 `useConfirm()`：`const [confirm, dialog] = useConfirm()` → `if (!await confirm({ title, desc, danger: true })) return` → 组件 JSX 末尾渲染 `{dialog}`。删除/中止/移除等危险操作设 `danger: true`（红色按钮）。对“全员指派”等不可由触发器重复点击误取消的二次确认，可显式传 `dismissOnOutside: false`，但必须保留 Escape 与取消按钮；禁止用固定 sleep 猜测双击时序。
- **操作成功提示**：禁止原生 `alert()`。用 `import { toast } from 'sonner'` → `toast.success('...')`（Toaster 已挂在 App.tsx，非阻塞、自动消失、跨设备一致）。
- **滑块**：禁止原生 `<input type="range">`。用 `@/components/ui/slider`（Radix Slider）：`<Slider value={[n]} onValueChange={(v)=>setN(v[0])} min max step disabled />`（单值用数组包裹）。
- **开关**：禁止原生 `<input type="checkbox">`（布尔开关语义）。用 `@/components/ui/switch`（Radix Switch）：`<Switch checked onCheckedChange={setBool} />`。
- **tooltip**：禁止原生 `title=`（触屏/移动端不可用）。用 `@/components/ui/tooltip`（Radix Tooltip，`TooltipProvider` 已挂 App.tsx 顶层）：`<Tooltip><TooltipTrigger asChild><X/></TooltipTrigger><TooltipContent>提示</TooltipContent></Tooltip>`。
- **number input spinner**：`@/components/ui/input` 已统一隐藏跨浏览器 spinner（`appearance-none` + webkit spin button 隐藏）；admin 裸 `<input>` 用 `pages/admin/ui.tsx` 导出的共享 `inp` 常量（含 spinner 隐藏），不内联 className。

**核心游戏契约层**（赛制/编排主流程经统一契约接入游戏；违反契约会在运行时崩）：
- **结果契约**：各游戏 `result.py` 独立定义裁判鸭子类型，产出 `winners`(座位号,空=平局) + `deltas`(长2零和)；编排层不触碰扑克 pot/board/holes 或棋盘细节。平台持久化结果由 `matches/result_contract.py` 唯一构造，基础字段为 `rounds_played`、`deltas`、`normalized_delta`；复式额外带 `legs`，每条新 leg 持久化自己的 `rounds_played`，技术终局可带有界故障摘要。Holdem 每个 70 手 session 独立按本场净筹码判胜；复式正常含两场同牌换座 session，顶层 `winner=NULL` 表示没有组合整体胜者，绝不是平局，组合 delta 只作后置破同分。赛事/list/detail 只公开统一 allowlisted `outcome` 摘要，不把原始 result/events 扩散到赛事投影。**测试守护**：`tests/test_result_contract.py`、outcome 与 runtime/迁移回归守护该契约。
- **赛事模板与淘汰决胜契约**：注册表共 21 个代码模板，其中 20 个允许新建（Holdem 8/7、Gomoku 7/7、Pencil 6/6）；`holdem_final_ranked` 仅供历史读取。创建入口公开 `recommended_min/recommended_max/purpose/time_class` 并显示基础对局、基础计分场、基础 ETA 与风险，但推荐只作指导，不得阻断自由选择。Gomoku 三个 Swiss 模板用通用 `swiss_round_bands` 在发布时冻结 `effective_rounds`：13–15 人 7 轮、16–20 人 9 轮、21 人以上 11 轮，通用层不得写游戏名分支。新 Holdem/Gomoku 单败只有显式冻结 `tiebreak="paired_swap_until_decided"` 才启用决胜：原局平后追加一组两场换座局，按原 stage scoring 比较该组积分；仍平则继续追加下一组，不设次数上限，不用 margin/delta/seed 兜底。Holdem 同组共享实际 seed 以保证同牌换座；Gomoku 只冻结共同 seed 并交换开局提案方/交换决策方，开局由 Bot 选择，绝不宣称同开局。历史无 marker 单败仍在平局处阻断，运行中/历史快照不得静默迁移；只有 draft/open、零 pairing/job/Match/赛果的赛事可经既有 CAS 更新。
- **GameSpec 接口**（`games/base.py`）：每款游戏须声明全部字段——`game_id`/`label`、`ruleset_id`/`protocol_version`/`rating_pool_id`、`session_factory`/`protocol`、`default_match_params`/`validate_match_params`、`normalize_delta`/`progress_from_events`/`eta_for_match`、`templates`/`default_scoring`、`fixed_rounds_per_match`、`code_path`/`summary`、`source_files`/`shared_source_files`、`preflight_check`、`build_match_plan`、`time_controls`、`default_time_control_id`、可选 `contest_source_candidate_kind`/`record_exporter`。这些字段均有生产消费者；禁止添加仅作说明但无人读取的契约字段。`contest_source_candidate_kind` 只允许 `protected_seed`、`navigation` 或 `None`，并须与本游戏模板的来源能力双向一致；通用来源候选 API 只能从注册表读取，禁止枚举游戏名。`normalize_delta` 把座位 0 原始分差换算为本游戏展示单位；`progress_from_events` 供无引擎结果的技术终局计算已完成轮数，通用层不得按游戏名计数；`fixed_rounds_per_match` 是固定场长游戏为历史 leg 回填进度的权威值，无固定场长则为 `None`；`record_exporter=None` 表示没有稳定单场导出格式，通用 `/api/matches/{id}/record` 只按能力调用，并只传公开 match 白名单、canonical public replay 与快照时间。`ProtocolSpec.validate_response_payload` 只校验从唯一标准信封提取出的 `response` 形状/类型；格式正确但规则非法的动作必须留给裁判。传输层要求顶层对象包含 `response`，只消费并保存该字段；可选顶层 `debug` 仅在正式 Bot-vs-Bot 终局后经限额、清洗、脱敏进入独立私有 sidecar，绝不进入 `responses[]`、游戏请求、result、公共 replay/SSE/WS 或日志，预检直接丢弃；其他额外顶层字段忽略。顶层整数、裸坐标和缺少 `response` 的旧 `{a}` 仍拒绝。LongRunning 缺失精确握手即技术负，绝不回退；上传预检须按所选 runtime_mode 使用与正式每个计分场首回合相同的信封和握手，Holdem 每个计分场首请求的 `max_hand` 固定为 70。`source_files` 是游戏包内公开源码白名单；`shared_source_files` 声明必须同时公开的 games 包根目录共享实现。`build_match_plan` 承载 duplicate 多场独立计分计划；`time_controls` 是版本化稳定 ID 白名单，`default_time_control_id` 是历史缺字段和无模板覆盖入口的游戏默认；赛事模板可在已注册白名单内声明自己的创建默认，编排层必须经 `resolve_time_control` 冻结并复核；`time_budget_per_side` 只保留为读取默认累计钟的 legacy property，不得作为新编排真相源。游戏规则全部使用每游戏代码常量：Holdem 每个计分场固定 70 手/20000 筹码/50-100 盲注，复式由多场计划逐场独立判胜计分；现行 `holdem_hu_nlhe_allin_v2` 的 all-in 水位必须包含本街此前盲注/下注，精确耗尽筹码的 call 进入 all-in 状态，只剩 0/1 名可行动玩家时直接 runout，覆盖筹码方正常 call 后无需也全压；Gomoku 固定 15×15 + 26 种指定开局 + 三手交换 + **五手二打（开局 v2 wire 继续发送 `n_range=[2,2]`，响应 `n` 与黑 5 候选数均固定为 2）** + 黑方禁手，Pencil 固定 N=6；对局时限由注册表与持久 `time_control_id` 冻结，Gomoku 支持每方累计 900/300 秒，Pencil 支持每方累计 900 秒或每次决策 1 秒；这些固定裁判规则不能被 admin、match_config 或直接 `run_session` 覆盖；时限只能通过已注册 `time_control_id` 选择，session_factory 对非内部 `rng`/`deal_sequence` 参数明确报错。规则变化但 wire 协议不变时也必须启用新的 `ruleset_id` 与 `rating_pool_id`，经停服 `game-rule-cutover` 归档旧评分池并保留历史 Bot 版本和回放，禁止把不同规则静默混入同一评分池。赛事阶段按 type 严格校验 allowed keys，未知/错拼/其他阶段字段一律拒绝。Bot 非法 JSON/信封/response 与超时首次发生即技术判负（`protocol_error`/`timeout`），平台故障仍 aborted 且不评分；human WebSocket 输入不得包装为 Bot 协议故障。持久化实体缺失或包含未知 `game_id` 时必须 fail closed，禁止猜成 Holdem；只有产品创建入口可明确提供默认游戏。通用层经 `registry.get(game_id)` 取 spec，**禁止 `if game_id==` 分支**；架构守护测试覆盖该约束。
- **未开赛赛事规则迁移**：`game-rule-cutover` 默认继续拒绝全部未终结赛事。只有调用方逐个传入 `--migrate-unstarted-contest-id`，且授权集合与该游戏全部非 showcase live 赛事完全相等时，才允许把状态严格为 `open`、零开始/结束时间、零派发、零 pairing/job/Match/阶段或正式结果的赛事纳入审核计划；完整赛事与有序名册快照摘要必须进入 `plan_digest`。apply 在评分池 CAS 的同一 `BEGIN IMMEDIATE` 中仅更新这些赛事的 ruleset/protocol/rating-pool 三元组，状态、阶段、名册与实名快照原样保留；任何 dry-run 后漂移或 CAS 行数不符都整事务回滚。finished/cancelled 历史赛事永不改写，链尾 postcondition 要求所有 live 赛事使用 target contract。
- **公开数值排名**：每个 `(owner_id,game_id)` 至多一个 `bots.is_ranked=1` 排位代表，由 partial unique index 强制；首个通过预检并激活的 Bot 在空席时自动派遣，owner 可原子切换或退出，停用/版本更新不隐式改席位，历史 Rating/RD/history 不复制、不重置。`RANKING_MIN_RATED_MATCHES` 是当前代表公开排名资格唯一阈值，与 `AUTO_MATCH_BOOTSTRAP_TARGET_MATCHES` 的队列冷启动目标独立。排行榜与 Bot profile 只输出 Rating、RD、95% 置信区间、1-based 名次/百分位、计分场次、不同对手数、资格进度、变化量与胜负平等客观数据；未派遣或不足阈值的样本 `rank=null`，不参与公开名次。

**新增一款游戏的成本**（赛制/编排主流程不加游戏名分支）——checklist：
1. 建 `games/<game>/` 子包：`<game>_judge.py`(纯裁判=游戏规则，0 平台依赖) + `engine.py`(适配层：裁判↔平台协议桥接) + `protocol.py`(仅导出本游戏行协议 API) + `result.py`(独立结果，满足鸭子契约) + `templates.py`(赛事模板) + `spec.py`(装配 GameSpec，明确版本化 `time_controls` 与 `default_time_control_id`；需要兼容旧调用时由只读 `time_budget_per_side` property 从默认控制派生)。若提供稳定的单场公开记录，在游戏包内实现只消费公开投影的 exporter 并赋给 `record_exporter`；否则保持 `None`，不得由通用层猜格式。若复用 `games/_board_protocol.py`，须在 `shared_source_files` 声明以随公开裁判源码提供，且不得导出其他游戏的 builder。
2. `schema.py` 的 `REGISTERED_ENGINES`/`VALID_GAME_IDS` frozenset 各加该项；`Store._migrate()` 会按注册 ID 用同构模板自动建立 `matches_<game>` 表与索引，无需复制静态 DDL。
3. `games/__init__.py`：`registry.register(SPEC)` 一行（启动断言 schema 与注册表一致）。
4. 前端 `src/games/<game>/`：`index.ts` 装配 GameViewSpec（Board/kind/reduce + `winner`/`describeEvent` + `humanPlay` 动作控件与序列化；协议特殊回合经 `canPickBoard(request)`/`turnLabelForRequest(request)` 声明，禁止通用页判断游戏名 + `replay` HUD/摘要/进度/分段导航）+ `canvas.ts`（CanvasRenderer；需要键盘等价操作时以 `keyboardPicks(scene)` 暴露与 pointer pick 同源的合法动作，供键盘/读屏操作）+ `reducer.ts`（事件归约，自包含对标后端 engine.py；启用棋钟时消费 `time_used/time_out`）+ 所需的游戏专属 UI 文件，再在 `src/games/index.ts` 注册一行。规则参数已固定，`configFields` 已删除。`RawEvent`/人类动作/HUD 公共类型在 `src/games/base.ts`；`normalizeGameId` 只规整字符串，`findGame` 对未知 id 返回空并由页面显示 unsupported，禁止回退 Holdem。
5. **不得**反向：`games/<game>/` 不得 import `bzplat.backend.engine`/`_compat`（循环依赖，`test_import_cycles.py` 守护）；通用层（matches/contests/store/api_routes）不得 import 具体游戏模块（经注册表）。
6. 跑测试：`pytest`（含 `test_result_contract`/`test_import_cycles`/`test_game_registry`，时限行为加 runner 回归）+ `npm run build` + `npm run test:e2e`；`screenshot_verify.py` 仅作补充。

**引擎路由入口**：`games.registry.get(game_id)` 取 `GameSpec` → `spec.run_session(decide, **params)` 构造并运行该游戏 Session；`spec.protocol.dumps_request/loads_response/validate_response_payload/fail_response` 处理行协议。`matches/runner.py` 经 games 注册表路由，不再有 if-chain。

**人类 vs Bot**（`match_type=human`）：引擎 `decide(player_idx, request)` 每回合阻塞；`run_bot_vs_human` 把 bot 侧接 BinaryRunner、人类侧接一个等待 `asyncio.Future` 的协程。orchestrator 的 `_human_turns` 注册 pending 回合并广播 `your_turn`，WebSocket `/play` 收到游戏动作即 `resolve_human_turn`。人类对局与 manual/contest/auto 共用全局执行队列和 match slots，claim 后固定占 `1 match slot + 1 sandbox unit`；`human_action_timeout` 默认 120s 逐回合防挂机，**不计 Glicko**，人工/人机请求 per-user 同时活跃 ≤ 1。人机局的冻结时限只约束 Bot，公开 `applies_to=bot_only`；真人继续只受 `human_action_timeout` 默认 120s 的逐回合防挂机限制，页面必须明确这是非对称练习模式。runner 仅为 Bot 侧发出对应 `time_used/time_out`，不得给真人套用 Bot 的累计或单步棋钟。Gomoku 的人类固定座位 1，但棋色由其在三手交换阶段的选择决定。

**挑战对战**（统一入口）：挑战页一个入口，两个物理座位；Bot-vs-Bot 通过 `my_seat=0/1` 选择“我的 Bot 位置”，普通用户/组织者的语义 `my_bot_id` 始终只能选自己的，admin 可选全站 active+runnable Bot。后端依据 `my_seat` 将 my/opponent 的 Bot、version、environment 与 local-agent 四元组整体映射到 A/B，并继续严格校验版本归属、完整性与游戏一致性。座位 1 在 Gomoku 中是开局提案方，座位 2 是交换决策方，棋色待交换决定，不能通称黑白。座位 2 还可改为 **「我亲自上场」**（人类，`human_seat=1` 固定；人类模式不使用 `my_seat`）。两个座位都选 Bot → `POST /api/matches/challenge`（`my_bot_id`/`opponent_bot_id`、`my_seat` + 可选版本，**自博弈允许**——同 bot 同/不同版本均可）；座位 2 = 人类 → `POST /api/matches/human`（`bot_id`=座位1 bot）。挑战选择器不隐藏练习 Bot；只有不同 owner 且双方都是各自 owner/game 当前 `is_ranked` 代表时计入平台排行，任一未派遣 Bot 固定 `rating_reason=ranked_bot_not_selected`。两个 POST 都返回 **HTTP 202 的持久 execution request**（`public_id`、排队位置/双容量/动态 ETA），不是立即返回 Match；前端持久化 `public_id` 并轮询 `GET /api/execution-requests/{public_id}`，只有 claim 后出现 `match_id` 才跳转对局。本人可 `DELETE` 取消 manual/human；可重试的 `interrupted` 通过 `POST .../retry`（202）把同一 job 重新排队，下次 claim 才创建新的不可复活 attempt，旧 Match 保留为不可变审计。显式版本或当前激活版本在 job 创建时冻结，claim 时复制到 `match_config._bot_a/b_version_id`；赛事版本来自 pairing 快照。排队期间上传/回滚不改变 runner 路径；排位代表切换会原子取消旧代表尚未 claim 的计分请求，claim 还会复核冻结双方仍是代表。无 `bot_versions` 行的 legacy Bot 才回退 `bots.binary_path`。`GET /api/bots/{id}/versions` 对非 owner 返回脱敏版本列表。

**座位编号约定**：**展示层从 1 开始**（座位 1/2），**内部 0-indexed**（后端 `winner`/`human_seat` 为 0/1，DB CHECK `winner IN (0,1)`）。前端显示 `+1`（Challenge/HumanPlay/MatchViewer/match-seats/canvas 共 7 处）。

**赛制阶段状态机**：`draft→open→published→running→(rest)→finished`。`ContestManager.maybe_finish` 是对局完成回调入口，负责瑞士补轮 / 淘汰晋级 / 休息期换 Bot / 进入下一阶段。`published` 是「排期已发布、等待开赛」中间态（报名截止→出排期→到点开打的两阶段）；`starts_at=NULL` 明确表示等待组织者手动开始，任何 scheduler/reconcile 路径都不得偷换为立即开赛。`ContestScheduler`（`contests/scheduler.py`，挂 main.py lifespan）后台周期扫描赛事 `*_at` 字段，到点自动推进阶段（开放报名/截止报名出排期/到点 enqueue pairing/rest 恢复）；组织者手动按钮始终可提前触发。逐场排期：运行态 `contest_pairings.scheduled_at=NULL` 才表示立即可排队，published 还必须先通过赛事级 `starts_at` 闸门。赛事只把 pairing 作为 `source=contest` job 写入全局执行队列；Match 及 replay/policy 只在 claim 时创建并原子绑定 pairing，其余 pairing 保持 `pending + match_id=NULL`。单场完成立即回写 pairing 并补下一条可排 job。新阶段首批 pairing（版本快照/bye/排期）与 `current_stage_idx/status` 必须经 Store 单事务批量提交；正式榜清旧/全量写入/`official_results_ready=1` 也必须同事务，启动对账负责补算 `finished+ready=0`。

**组织者实名 + 导出**：`require_real_name` 赛事只允许参赛者本人经 `/register` 报名，并在报名时由 entry 冻结 `real_name/phone/school/student_id`、采集时间与来源；普通 organizer 不得代报名，admin 显式 override 必须写无 PII 审计。管理员代报名以精确的“活跃用户 → 该用户当前可运行、同游戏 Bot”映射为主路径，可暂存多项后一次提交；`assign_all` 只保留为需再次确认的次要快捷操作。候选筛选不替代后端门禁：Manager 与 Store 写事务仍复核用户 active、Bot owner/游戏/当前版本/协议/二进制可运行性及实名完整性，部分跳过必须逐项返回原因。legacy entry 不伪造快照，只在授权私有读取中标为 `current_profile_legacy` 并回退当前资料。`contest_entries_named` 与私有导出在同一 SQL 行内 JOIN 赛事门禁：非实名赛即使组织者/admin 请求也返回零 PII；详情继续返回顶层 `is_organizer`，`my_entry` 使用正向白名单。公开 `/official-results` JSON/CSV 永不含 PII。私有 `GET /api/contests/{id}/export?format=csv` 由组织者/admin gated；无 `schema` 的 16 列 CSV v1 保持兼容，`schema=2` 提供 29 列双语表头、稳定 entry/user/Bot ID、显示名、身份来源和阶段/成绩状态（UTF-8 BOM，文本与公式注入安全）。前端赛程：BracketTree（SVG 连接线，`bracket_slot//2` 拓扑）+ ScheduleTable（一览表）+ 阶段 Tab 显示中文标签 + 进度。

## 按任务必读文档

不要求每个任务机械通读全部文档；按影响面读取对应权威来源，涉及多个领域就合并阅读：

- **开发环境、worktree、构建、部署、脚本**：`doc/DEVELOPMENT.md`。
- **模块、数据、API、前端与安全架构**：`doc/DESIGN.md`；安全/隐私任务同时读 `doc/SECURITY.md`。
- **测试策略、浏览器角色/视口、发布门槛**：`doc/TESTING.md` + `doc/BROWSER_ACCEPTANCE.md`。
- **执行队列、Docker、maintenance、迁移、cutover、评分重建与回滚**：`doc/RUNTIME.md`。
- **唯一现行 Bot 通信协议**：`wiki/PROTOCOL.md` + `contracts/`；Bot 开发/上传读 `wiki/BOT_DEV.md`，游戏规则读对应 `wiki/TEXAS.md` / `wiki/GOMOKU.md` / `wiki/PENCIL.md`。
- **新增游戏或修改样例**：本文件“新增一款游戏的成本” + `doc/DESIGN.md` §2.3 + `samples/`。
- **文档入口与职责**：`doc/INDEX.md`、`wiki/INDEX.md`、README。
