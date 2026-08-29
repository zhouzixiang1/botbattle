# 开发文档

> 本文档说明如何搭建开发环境、构建运行、遵守编码规范、扩展模块与部署运维。

## 1. 环境搭建

### 1.1 前置依赖
| 依赖 | 版本 | 用途 |
|------|------|------|
| Python | ≥ 3.12 | 后端 |
| Node.js | ≥ 22 | 前端构建 |
| Docker | 最新 | Bot 沙箱（必需；`BZ_BOT_LOCAL=1` 可退回本机仅测试用） |

### 1.2 后端安装（仅本任务 worktree）

现有生产主目录的共享 `.venv` 不得由开发任务修改。依赖不变时，可从 worktree CWD 使用
`/home/zzx/project/botbattle/.venv/bin/python` 作为只读工具链；依赖新增/升级时在 worktree 建私有环境：

```bash
cd /home/zzx/project/botbattle/.worktrees/<任务名>
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'          # 装 bzplat 包 + pytest/httpx
```

当前 `pyproject.toml` 只有最低版本约束，不是生产依赖锁；systemd 又固定执行主目录 `.venv/bin/python`。
因此 Python 依赖变更不能原地发布：PR 必须同时提供精确生产 lock/constraints、并行新 venv 的构建/验收、
受控启动切换和旧 release + 旧 lock + 旧 venv 的回滚方案。缺少该闭环即为部署 No-Go。

### 1.3 前端安装（仅本任务 worktree）
```bash
cd /home/zzx/project/botbattle/.worktrees/<任务名>/bzplat/frontend
npm install
```

### 1.4 配置文件
关键配置在 `.env`（**勿提交到版本库，含敏感凭据**）：

| 变量 | 说明 | 默认 |
|------|------|------|
| `BZ_HOST` / `BZ_PORT` | 绑定地址/端口 | 127.0.0.1 / 50380 |
| `BZ_ALLOW_LAN_BIND` | 只有设为 `1` 才允许 `BZ_HOST=0.0.0.0`；此前必须把主机防火墙限制到受信 LAN | 0 |
| `BZ_PUBLIC_ORIGIN` | 浏览器实际访问的唯一 HTTP(S) origin；人机 WS 严格校验，生产必配 | 未设（WS fail closed） |
| `BZ_DB_PATH` | SQLite 路径 | botzone.db |
| `BZ_INSTANCE_KEY` | Docker 清理 namespace；输入会归一化为小写，结果须为 1–48 位字母、数字、`.`、`_`、`-`，生产/每个 worktree 必须稳定且唯一 | 未设时由绝对 DB 路径 SHA-256 派生 |
| `BZ_DOCKER_HOST` | 生产 Docker 控制面显式覆写；只接受 canonical 本机 socket | `unix:///var/run/docker.sock` |
| `BZ_BOT_LOCAL` | 强制本机跑 ELF（测试） | 未设 |
| `BZ_QA_INSTANCE` | 标记隔离 QA 实例；启用时启动前拒绝主 checkout/50380 写目标 | 未设 |
| `BZ_API_TARGET` | Vite REST/SSE/WS 代理目标；50380 被硬拒绝 | 127.0.0.1:50381 |
| `BZ_AVATAR_DIR` | 头像目录 | avatars |
| `BZ_RATE_LIMIT` | 启用限流 | 1 |
| `BZ_TRUST_PROXY` | 允许受信 socket peer 提供代理身份头（反向代理部署时开启） | 未设 |
| `BZ_TRUSTED_PROXY_CIDRS` | 可提供 `X-Real-IP/XFF` 的 ASGI socket peer CIDR；生产显式设精确 loopback，不能填客户端 LAN | `127.0.0.1/32,::1/128` |
| `BZ_TRUSTED_PROXY_HOPS` | XFF 中由受信 HTTP 代理写入的层数；frp TCP 透传不计一层 | 1 |
| `BZ_LOG_LEVEL` / `BZ_LOG_DIR` | 日志级别 / 目录 | INFO / logs |
| `SMTP_HOST/PORT/USER/PASSWORD/FROM` | SMTP（邮箱验证/重置/通知） | 未配则注册/重置返回 503 |
| `SMTP_FROM_NAME` | 邮件显示的发件人名称 | Botbattle |
| `EMAIL_CODE_TTL_MINUTES` | 验证码 TTL | 30 |

> ⚠️ **敏感信息警示**：`.env` 含 SMTP 明文密码，**绝不提交**。`.gitignore` 应排除 `.env`。文档中不回写真实凭据。

生产 Docker 命令始终显式带 `--host unix:///var/run/docker.sock`。父进程中的 `DOCKER_HOST`、
`DOCKER_CONTEXT` 与 TLS 变量会从子命令环境中清除，不参与 daemon 选择；若显式设置
`BZ_DOCKER_HOST`，任何非 canonical 值都会在 Store 迁移前 fail closed。平台不支持远端 Docker。

这里的 `BZ_BOT_LOCAL=1` 是**平台开发测试回退**：它让服务器进程直接启动 ELF，生产不得开启。玩家使用的“本地 Bot”是另一项产品能力：用户端通过 WSS 主动连接，服务器仍只负责裁判；两者不要混用。

邮件模块只提供一套 Botbattle 多游戏平台默认文案：邮箱验证、密码重置和验证完成欢迎信。
新库通过 `INSERT OR IGNORE` 播种这三条模板，因此管理后台已经保存的自定义模板不会在重启时
被覆盖；历史库如需恢复官方文案，必须先备份，再对这三个精确 key 做受控数据更新。

## 2. 构建与运行

### 2.1 安全启停

生产 `.env` 应为该实例固定一个不会与 QA/worktree 复用的 namespace：

```bash
# .env 示例；instance key 上线后保持稳定
BZ_INSTANCE_KEY=prod-main
BZ_DOCKER_HOST=unix:///var/run/docker.sock
BZ_PUBLIC_ORIGIN=https://bot.example.com

scripts/platform-ctl.sh start     # 生产唯一控制入口；不要绕过为 raw botzone serve
# 默认 127.0.0.1:50380
# 仅首次安装且用户对精确主库写入明确授权时：
botzone create-admin <user> <email> '<pass>'   # 建管理员（跳过邮箱验证）
```

`platform-ctl.sh` 只有一个控制入口，但支持两种互斥的托管方式：若 user systemd 已加载
`botzone-platform.service`，且 unit 的 `WorkingDirectory` 解析后恰好等于当前 checkout，脚本把
`start/stop/restart/status/logs` 全部委托给该 unit；否则才使用 `platform-ctl/web.pid`。不得同时手工
启用两种方式。PID fallback 在启动前会确认目标端口无监听；没有自己的活动 PID 却发现监听、无法
查询端口或停止后 PID/端口未释放时均 fail closed，不会再创建进程。两种方式的 start/restart 都在
返回成功前等待 `/api/health`，默认就绪上限 60 秒；`0.0.0.0` 绑定仍从 `127.0.0.1` 探活。
stop 最多等待 90 秒完成 lifespan 并释放端口。
PID fallback 的 `web.pid` 不是裸 PID，而是 PID、每次启动随机 nonce 与 Linux 进程 starttime 的
私有身份记录；发信号前还会核对进程 CWD 与只存在于该进程环境的 nonce。旧裸 PID、PID 复用、身份
字段缺失或不匹配都保留现场并拒绝发信号。user-systemd 探测也使用三态：只有明确 `not-found` 或
已确认 unit 属于另一 checkout 才允许 fallback；DBus/systemctl/属性读取错误直接退出，不能静默降级。
因此 `scripts/rebuild.sh` 可以复用同一入口，不会绕过 systemd 另起 `nohup` 进程。

启动会先取得数据库邻接 dispatcher flock，并在共享 `<db>.docker-launch.lock` 内对**本 instance
label namespace** 清理、连续确认容器/name/token 为零，同时闭合 create journal；只有随后完成 attempt
补偿才进入 `running/accepting`。同一 host boot 的未确认 create 属于人工边界，即使瞬时双零也保持
`manual:` paused；不要直接改数据库状态，须在管理端按恢复流程重新做精确清场。其他 Docker 控制结果
不确定时会保持 `paused` 并有界退避，不得以进程/端口已出现代替管理端队列状态与日志检查。
本地 Bot 的旧在线态与租约也只允许取得 dispatcher flock 的实例重置；停服时先关闭连接并重置租约，
最后释放 flock。第二实例若因 flock 已占用而启动失败，不得触碰这些共享状态。赛事任务 claim 还会用
affinity/cgroup/物理资源共同计算的有效主机预算检查冻结的 4 核/4 GiB 请求；不足时保留排队并明确提示，
不会自动换成节能沙箱。

维护前先记录实际 PID，再通过统一脚本停止并核对进程和端口。systemd 模式从 unit 读取 MainPID；
只有 PID fallback 才读取 `platform-ctl/web.pid`。脚本打印 `stopped` 时已确认 lifespan 进程退出且端口
释放，但数据库/Docker 的业务状态仍须按下面清单复核：

```bash
scripts/platform-ctl.sh status
service_pid="$(systemctl --user show botzone-platform.service \
  --property=MainPID --value 2>/dev/null || true)"
[[ "$service_pid" =~ ^[1-9][0-9]*$ ]] || service_pid="$(cut -d' ' -f1 platform-ctl/web.pid)"
scripts/platform-ctl.sh stop
ps -p "$service_pid" -o pid=,stat=,cmd=       # 应无输出
ss -tlnp | grep ':50380'                      # 应无输出
docker --host unix:///var/run/docker.sock ps -a \
  --filter label=io.botbattle.instance=prod-main
```

正常 lifespan 会先置 `accepting=0`、停止 dispatcher、取消并等待本进程 attempt，再尽力清理精确
label。若进程仍在、端口仍监听或本 namespace 容器仍存在，不得开始 DB 维护，也不得启动第二个
进程；先查 `logs/web.log`/`logs/app.log`。强制崩溃后的残留由下一次启动精确清场与补偿，不能手工
跨 namespace 批量删除。评分重建还有更严格的 DB No-Go，见 6.5。

默认主库旁的 `botzone.db.execution-dispatcher.lock` 与 `botzone.db.docker-launch.lock` 是 flock
协调 inode，不是可清理缓存：前者可能被运行中 dispatcher 长期持有，后者在 Docker create/cleanup
窗口持有。运行中删除会让旧进程继续锁住已 unlink 的 inode，而新进程锁住同名新 inode，从而破坏
互斥。仓库只精确忽略这两个默认文件名，不使用 `*.lock`；停服后它们也应保留，不能纳入缓存清理。

#### 计划部署：先排空，再停服

线上更新不得直接以 `restart` 抢断当前局。按以下顺序操作；三个 maintenance 端点均只允许超级管理员：

1. 先确认执行服务为 `running`。若管理端显示运行环境故障 `paused`，先调用
   `POST /api/admin/execution-queue/resume` 完成精确清场与业务对账；未恢复时开始排空会返回
   `409 maintenance_state_conflict`。
2. 在管理端点击“准备维护”，等价于一次
   `POST /api/admin/execution-queue/maintenance`。成功响应必须同时满足
   `maintenance.requested=true`、`dispatcher.accepting=false`、`dispatcher.auto_enabled=false`。
   这一步一次提交即可；不要循环 POST。
3. 轮询 `GET /api/admin/execution-queue/maintenance`，直到 `maintenance.ready=true`。期间当前局自然
   收尾，既有 queued 任务和赛事配对保持原样；不得手工取消队列、改主库状态或提前停服。若
   `active_count`、`uploads_in_flight`、`active_local_ai_leases`、`owned_execution_tasks`、
   `untracked_running_matches` 非零，或 `docker_launch_state` 非 `idle`，继续等待。
   `readiness_unavailable` 非空表示上传/任务探针不可用，或评分与赛事恢复仍在执行，不能视为就绪。
4. ready 后继续严格执行 `AGENTS.md` §1.8：停服并核对 PID/端口/instance 容器和 SQLite sidecar，
   封存不同 inode 的冷备，在第三份临时 DB 预演迁移，随后才精确推进已审 target SHA、按 manifest/lock 更新依赖并
   `bash scripts/rebuild.sh`。不要从 ready 直接 restart，不要打开唯一冷备，不要删除 DB 邻接 flock，
   也不要跨 namespace 手工清容器。
5. 重启健康检查通过后再次 GET。预期为 `dispatcher.state=running`、`accepting=false`、
   `maintenance.requested=true`；启动清理、attempt 恢复和赛事/评分对账不会解除部署门。
6. 验证新版本后调用一次 `DELETE /api/admin/execution-queue/maintenance`。只有 ready 仍成立时才返回
   200，并恢复 `accepting=true`；`auto_enabled` 保持 false。确需自动排位时，再单独调用
   `PUT /api/admin/auto-match` 并提交 `{"enabled": true}`。

部署取消也使用第 6 步显式结束排空，不能靠重启、运行环境恢复或直接改数据库解除。若排空过程中又因
Docker 不确定进入 `paused`，管理端执行“清场并恢复”后继续轮询；恢复只让 dispatcher 回到
`running + accepting=false`，不会丢 queued job，也不会清除 drain。

### 2.2 构建前端
```bash
cd /home/zzx/project/botbattle/.worktrees/<任务名>/bzplat/frontend
npm run build   # 只写本任务 worktree 的 dist
```
> **关键前端依赖**：react 19 / vite 8 / tailwindcss v4 / shadcn(new-york) / recharts。
> 视觉层另用 `gsap ^3.x`（npm 安装，2025-04 起 100% 免费商用，驱动 canvas 牌桌动画）+
> Poker.JS（vendor 副本，来源 Tairraos/Poker.JS，用于 canvas 矢量扑克牌绘制）。

### 2.3 构建三游戏样例 Bot

仓库样例只面向平台开发、回归与发布验收。统一脚本构建 Holdem、Gomoku、Pencil 的
Linux x86_64 ELF，并检查产物类型：

```bash
cd /home/zzx/project/botbattle/.worktrees/<任务名>
bash samples/build_sample.sh
file samples/{callbot,gomokubot,pencilbot}_linux_amd64
sha256sum samples/gomoku_showcase/*_linux_amd64
```

脚本还会从 `samples/gomoku_showcase/gomoku_showcase_bot.c` 构建赛事演示专用的
`tactical/steady/foundation` 三档 LongRunning ELF。三档不读时钟、不用随机数，checksum
由演示 seed manifest 锁定；它们是合成的强/中/弱矩阵，不是自然形成的 12 种独立棋力。
当前 Gomoku 样例和三档演示 ELF 均为 `gomoku_action_v2`，支持指定开局、交换、固定五手二打和 PASS；
改源码后必须通过 Traditional/LongRunning 真实自对局、禁手零触发与 checked-in ELF 可重建一致性后才能更新 manifest。
同一 wire 下仍会按现行规则拒绝 `n=3/4` 或非两个黑 5 候选；旧构建必须更新策略并重新编译，不能只因
仍使用 `gomoku_action_v2` 就视为兼容现行 `gomoku_ccgc_2013_five_move_two_v2`。

玩家侧跨系统构建说明不依赖仓库脚本，见 `wiki/BOT_DEV.md`。

### 2.4 运行时代码发布需要 rebuild（生产先排空）

前端产物（`dist`）由后端 StaticFiles 托管、后端代码由运行进程加载，因此运行时代码不 rebuild 不会生效；
但 `scripts/rebuild.sh` 本身不申请 drain、停服冷备、迁移预演、冻结 target SHA 的精确推进或依赖安装。生产必须完整执行
§2.1 与 `AGENTS.md` §1.8，直到停服/冷备/预演/精确推进已审 target SHA/依赖步骤完成后才运行脚本。只有先 fetch、
冻结并审阅完整 fast-forward 区间、确认其中全是纯文档/规则后，才无需 rebuild 或 restart；推进必须离线钉死该 SHA，
不能在审阅后再用普通 `git pull` 获取远端新提交。若区间夹带运行时变更，必须在推进工作树前转完整发布流程。前端依赖变化按 `package-lock.json` 执行 `npm ci`；Python 依赖变化还必须先满足
§1.2 的 lock + 并行 venv 发布门，禁止原地修改生产 `.venv`。

### 2.5 worktree 隔离开发（勿碰线上 50380）

主目录 `main` 只跑线上服务（默认 `:50380` + 主库）。特性开发在 **git worktree** 中跑**独立**栈，避免污染线上 db/源码：

```bash
# 1) 主库只读复制到 worktree（必须 cp，不得软链接）
cp /home/zzx/project/botbattle/botzone.db .worktrees/<分支名>/botzone.db

# 2) 终端 A：后端（CWD=worktree，显式锁定副本并声明 QA）
cd .worktrees/<分支名>
BZ_DB_PATH="$PWD/botzone.db" BZ_INSTANCE_KEY=qa-refactor-global-queue \
  BZ_DOCKER_HOST=unix:///var/run/docker.sock BZ_QA_INSTANCE=1 BZ_BOT_LOCAL=1 BZ_SKIP_CAPTCHA=1 \
  BZ_PUBLIC_ORIGIN=http://127.0.0.1:5173 \
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
- 每个并行 worktree 要把示例 `BZ_INSTANCE_KEY` 换成自己的稳定唯一值；不要与生产或其他 worktree 共用。即使当前使用 `BZ_BOT_LOCAL=1`，也保留该约束以防切回 Docker 后误清理。
- `BZ_QA_INSTANCE=1` 通过独立代码能力门强制禁用自动排位；复制库中的 `execution_control.auto_enabled` 即使为真也无效，API 尝试开启返回 409。生产同样只以该字段作为自动 producer 的唯一开关，不存在 QA/生产两套参数 profile。
- 合并走 GitHub PR；详见根目录 [`AGENTS.md`](../AGENTS.md) §1.3“建立独立 worktree 与分支”与 §1.4“数据库、端口与运行时隔离”。

### 2.6 本地 Bot 客户端

公开客户端为 `scripts/local_ai_client.py`。它只实现 Traditional 进程生命周期：收到一条 `turn` 后启动一次用户命令，向 stdin 写一行，读取 stdout 首行并结束进程。游戏信封和动作校验仍由 GameSpec/裁判负责，客户端不能添加游戏名分支。

```bash
read -rsp "接入令牌: " BZ_LOCAL_AI_TOKEN && echo
export BZ_LOCAL_AI_TOKEN
python scripts/local_ai_client.py \
  --url wss://bot.example.com/api/local-ai/connect \
  --command ./my_bot
```

安全边界：

- 客户端只接受 `wss://` 且拒绝 URL userinfo、query、fragment 和握手 30x 重定向；令牌只读 `BZ_LOCAL_AI_TOKEN`，只进入首个目标的 `Authorization: Bearer`，日志不输出连接异常正文，启动 Bot 子进程前还会从继承环境中移除该变量。重定向拒绝同时兼容 websockets 10.4--13 的 `handle_redirect` 与 14+ 的 `process_redirect`，未知连接实现 fail closed。
- 单次服务端输入最大 1 MiB，Bot stdout 首行最大 64 KiB；`timeout_ms` 包括本机进程启动时间，超时后终止整个进程组。
- 同一连接串行执行决策；断线按 1/2/4/8/16/30 秒退避，重连不改变服务端持有的绝对回合截止时间。
- 服务端在数据库认证前按可信代理边界解析 peer IP，限制握手频率和同时认证数；未认证或被限流的连接在 `accept` 前发送标准 ASGI WebSocket policy close，由 Uvicorn 返回可解析的 HTTP 403，不走 SansIO 会产生重复实体头和未完成状态的 denial-response 扩展。Uvicorn 在解码前把单消息硬顶钉在 256 KiB（为 64 KiB Bot 输出经 JSON 转义预留空间），SansIO transport 每收到一条完整消息即暂停继续读取，应用兜底遇到超限消息立即以 1009 断开。已连接 socket 使用入站突发桶，SQLite 存活写合并到 15 秒一次。每用户最多 8 个 active identity、4 个在线连接，全站最多 64 个在线连接；令牌轮换同时受规范化 HTTP 路径桶和稳定 owner+agent 桶约束，并在同一 SQLite 事务拒绝仍持 active lease 的 identity，避免轮换中断正在执行的对局。
- 撤销后的同名 identity 行可原子复用并换发全新 public id/token；账号或 Bot 停用会在同一 Store 事务撤销关联 identity、释放租约，transport 再由 hub 通知或周期复核关闭。
- 用户端无需监听端口。生产反向代理必须允许 `/api/local-ai/connect` 的 WebSocket Upgrade 和 `Authorization` 请求头，并使用有效 TLS 证书。

玩家操作与消息格式见 `wiki/LOCAL_AI.md`。客户端令牌不得写入 `.env`、systemd unit、命令参数、URL 或测试 fixture；自动托管时应使用操作系统凭据存储，在启动时注入环境变量。

## 3. 编码规范

| 规范 | 要求 |
|------|------|
| **Python 包名** | 必须是 `bzplat`，**绝不能叫 `platform`**（会遮蔽标准库 `platform`）。所有 import 用绝对路径 `from bzplat.backend...` |
| **常量集中** | 所有状态码/对局类型/`REGISTERED_ENGINES`/`VALID_GAME_IDS`/平台 settings 键名集中在 `store/schema.py`，别散落 |
| **日志** | 后端生产代码禁止 `print()`，统一 `logging.getLogger(__name__)`；测试夹具/子进程样例及明确面向 stdout 的 CLI 输出除外 |
| **游戏解耦** | 通用层（matches/contests/store/api_routes）**禁止 `if game_id == ...` 分支**；经 `games.registry.get(game_id)` 取 `GameSpec`；持久化实体缺失/未知 game_id 必须失败，不能猜默认游戏 |
| **资源硬顶** | `runtime/config.py` 固定 6 match slots / 12 sandbox units；`runtime/limits.py` 只允许节能档（每 Bot 1 核/512 MiB）与赛事档（每 Bot 2 核/2 GiB），后者仅 contest 来源可取，本地 Bot/真人不启动 Docker。job 入队冻结 sandbox/CPU/内存向量，claim 再按 affinity/cgroup/物理预算逐维准入，实际并发依组合为 1–6；显式启动值只能收紧，admin 不能抬高。每个 job 占 1 slot，赛事份额 1 不是额外槽；同一非 human Bot 全局至多一个 active job。全员及分组单/双循环均无人数硬上限，完整 O(n²) 排期只增加持久 job；历史 `allow_large_round_robin` 仅作 no-op 兼容。Bot 文件上限 100 MiB |
| **赛事演进边界** | 模板人数范围、用途与时长只是非阻断推荐；新增/修改阶段结构须同步模板元数据、estimate、前端风险提示和测试。新 Holdem/Gomoku KO 只有冻结 `paired_swap_until_decided` 才可无限追加两场换座决胜组；历史无 marker 保持平局阻断。不得原地改写 running/finished 快照；仅 draft/open 且零 pairing/job/Match/正式结果的赛事可走现有 CAS 更新。Gomoku `swiss_round_bands` 在 publish 冻结 `effective_rounds`，不得在通用层加游戏名分支 |
| **运行参数** | `runtime/config.py` 是 action timeout、全局双资源容量/aging/用户上限、自动排位 bootstrap 目标、公开排名资格、赛事 scheduler 等参数的代码唯一来源；修改后须评审、测试并重新发布。自动排位只是 producer，唯一可变项为 `execution_control.auto_enabled`；`BZ_MAX_CONCURRENT_MATCHES` 与 admin runtime PATCH 均不支持 |
| **前端图标** | 统一 lucide-react（**无 emoji**），按需导入 |
| **前端颜色** | 用语义 token（`bg-background`/`text-primary`），不裸 hex、不硬编码 slate/brand 颜色 |
| **前端组件** | 用 `@/components/ui/*` 共享原语，禁内联重复样式 |
| **路径别名** | 前端跨目录/跨层 import 用 `@/` → `src/`；同目录内部允许相对路径 |

## 4. Git 工作流

遵循 [`AGENTS.md`](../AGENTS.md)（权威）：
1. 任何仓库修改先在独立 worktree 的 `feat/`、`fix/`、`refactor/`、`docs/`、`test/` 或 `chore/` 分支完成；主目录只维护 `main` 和生产服务。
2. 合并必须走 GitHub Pull Request；禁止直接在 `main` 提交、push，或把本地 feature 分支 merge 到 `main`。PR 合并后只能按 `AGENTS.md` §1.8 将 main fast-forward 到已 fetch、已审阅的精确 target SHA；随后清理本任务 worktree 与本地/远端分支。
3. 验证按 `AGENTS.md`“变更对应的最低验证矩阵”执行：后端最终候选跑完整 `pytest`；前端按影响跑 unit/build/Playwright；API、DB、运行时和发布候选另有隔离 smoke/迁移/浏览器门禁。
4. 行为变更同步边界回归与下方文档影响矩阵。会话记忆仅在用户明确要求且环境允许时更新，不能替代测试和仓库文档。
5. 多 agent 的不同任务使用独立 worktree；同一任务并行时预先划分互不重叠的文件所有权，公共文件由主负责人统一集成。

## 5. 模块扩展指南

### 5.1 新增一款游戏（赛制/编排主流程不加游戏名分支）

通用层**不得**再加 `if game_id == ...` 分支。权威 checklist 与 [`AGENTS.md`](../AGENTS.md) / [`DESIGN.md`](./DESIGN.md) §2.3 一致：

1. 建 `games/<game>/` 子包：
   - `<game>_judge.py`（纯游戏规则，零平台依赖）
   - `engine.py`（裁判↔平台协议适配，提供 Session 并驱动纯裁判，`run_async(decide) → MatchResult`）
   - `protocol.py`（`dumps_request` / `loads_response` / `validate_response_payload` / `fail_response`；只导出本游戏 API；复用 `games/_board_protocol.py` 时在 spec 的 `shared_source_files` 声明公开源码）
   - `result.py`（**独立**定义，满足鸭子契约：`winners` + `deltas`，**不**共享基类）
   - `templates.py`（本游戏内置赛事模板）
   - `spec.py`（装配 `GameSpec`，声明 `normalize_delta` 与 `progress_from_events`）
2. `store/schema.py` 的 `REGISTERED_ENGINES` / `VALID_GAME_IDS` frozenset 各加一项；`Store._migrate()` 根据注册 ID 用同构模板创建 `matches_<game>` 表及索引，不复制静态 DDL。
3. `games/__init__.py`：`registry.register(SPEC)` 一行（启动时断言 schema 与注册表 ID 集合一致）。
4. 前端：`src/games/<game>/index.ts`（`GameViewSpec`：Board/kind/reduce/`CanvasRenderer`）+ 在 `src/games/index.ts` 注册；规则参数固定后已无 `configFields`。
5. **禁止反向依赖**：`games/<game>/` 不得反向 import 通用层；通用层不得 import 具体游戏模块（`test_import_cycles.py` 源码扫描守护）。
6. 跑测试：`pytest`（含 `test_result_contract` / `test_import_cycles` / `test_game_registry` / `test_tongyong_layer_no_game_branches`）+ `npm run build`。

> 新代码直接面向 `games` 注册表，不要在 `matches/runner.py` 加游戏分支。`run_session` 的 kwargs 仅供内部复现控制，新增或拼错规则键必须显式失败。

公开排名资格只修改 `runtime/config.py` 的 `RANKING_MIN_RATED_MATCHES`；auto-match 内部冷启动目标是独立的 `AutoMatchConfig.bootstrap_target_matches`。两者目前默认都是 10，但消费边界不同，不得用调度配置驱动公开 API。

### 5.2 新增 API 端点
- 在 `api_routes.py`（或 `auth/routes.py`）加路由，按需用 `require_user`/`require_admin`/`require_organizer` 依赖。
- 常量（新状态码/类型）加到 `schema.py`。
- **路由顺序注意**：字面量路由（如 `/api/matches/liked-top`）必须在参数路由（`/api/matches/{match_id}`）之前注册。

## 6. 部署与运维

### 6.1 systemd 部署
`deploy/botzone-platform.service` 提供 systemd unit 模板。
建议生产安装为当前服务用户的 user unit，并保持 linger，使登出后仍由 systemd 托管：

```bash
install -d -m 700 "$HOME/.config/systemd/user"
install -m 600 deploy/botzone-platform.service \
  "$HOME/.config/systemd/user/botzone-platform.service"
systemctl --user daemon-reload
systemctl --user enable --now botzone-platform.service
sudo loginctl enable-linger "$USER"              # 管理员首次安装时执行一次
loginctl show-user "$USER" -p Linger          # 应为 Linger=yes
scripts/platform-ctl.sh status                # 应显示 running (user systemd)
```

首次切换前必须先用旧控制方式完整停服并确认 50380 已释放；不能让 systemd 与 PID fallback 同时启动。
更新 unit 后先 `systemd-analyze --user verify`，再 `daemon-reload`。脚本只接管 `WorkingDirectory` 与
当前 checkout 完全一致的 user unit，避免从 linked worktree 误重启 main；其他 unit 即使同名也不会
被操作，其监听端口仍会触发 fallback 的 fail-closed 检查。
systemd 模板使用 `UMask=0077`，`scripts/platform-ctl.sh` 也在创建 PID、日志、数据库关联
产物前固定 `umask 077`；生产 `.env`、数据库与日志应为 `0600`，私有运行目录为 `0700`。
头像是公开静态内容，权限可按静态服务器的只读需求单独配置，不能因此放宽其他运行目录。
服务默认只绑定 `127.0.0.1`，本机 frp/nginx 继续连接回环端口。确需让
`192.168.1.0/24` 直连时，必须先按 [SECURITY.md](./SECURITY.md#受控-lan-直连)
把主机防火墙的 50380 入站限制到该网段，再同时设置
`BZ_HOST=0.0.0.0` 与 `BZ_ALLOW_LAN_BIND=1`。缺 gate、其他非回环地址或无效端口都会在创建
PID/日志/数据库前拒绝；CLI 同样执行该门，不能通过直接 systemctl 绕过。systemd 模板不再硬编码
host/port，而由 CLI 从 `EnvironmentFile` 读取并安全默认到 `127.0.0.1:50380`。既有生产实例更新模板前
必须先完成 §2.1 / `AGENTS.md` §1.8 的 maintenance 排空并达到 ready，再在停服发布窗内重新 `install`、
`daemon-reload`，最后由 `scripts/platform-ctl.sh restart` 做有界健康验证；首次安装按本节上方的停服
切换流程执行，不得与 PID fallback 并行。

### 6.2 日志（三文件 + 启动日志）
- `logs/app.log`：业务/系统日志（`logging_config.setup_logging`，格式 `时间 级别 [模块] 消息`）。排查执行队列/自动 producer、Docker cleanup/恢复、对局/Bot 崩溃和 WS 在此；Bot EOF 附 stderr 末尾。Uvicorn HTTP/WS record 在 handler 序列化前只保留 path，不记录 query。
- `logs/access.log`：HTTP 访问日志（真实 IP + 方法 + 路径 + 状态 + 耗时；middleware 使用 `request.url.path`，不含 query）。
- `logs/audit.log`：安全审计（登录/注册/改密/上传/管理操作等）。
- `logs/web.log`：PID fallback 的 uvicorn 启动 stdout；systemd 模式通过 `scripts/platform-ctl.sh logs`
  读取该 unit 的 journal。CLI 禁止 Uvicorn 默认日志配置覆盖平台 handler，因此两者同样不含请求 query。
- **admin「日志」Tab**：`GET /api/admin/logs?file={app|access|audit}`（文件参数白名单）。详见 [SECURITY.md](./SECURITY.md)。

上游 nginx 是独立日志边界：其 `access_log` 必须用 `$request_method`、`$uri`、
`$server_protocol` 组成请求行，禁止记录包含 query 的 `$request`/`$request_uri`。完整示例见
[SECURITY.md](./SECURITY.md)。

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
专用 Bot 最终全部 inactive（历史详情仍可按 ID 查看），不会进入五子棋榜单或自动排位候选。

部署到主库属于显式运维写操作，只能在代码已评审、主库已备份且 50380 已停服后执行；独立 seed
进程不能与线上 dispatcher/orchestrator 叠加并发：

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
| `bzplat/frontend/e2e/*.spec.ts` | 访客/用户/组织者/admin 真浏览器回归；覆盖 Console+Network+SSE+WS、多视口、赛事计分/赛果、Holdem HUD/复式/真人公开信息。用 `playwright test --list` 获取目标 HEAD 的实时数量，最终执行真值见 `TESTING.md`，不在文档硬编码易漂移计数 |

### 6.5 评分投影维护命令

`python -m bzplat.backend.cli rating-rebuild --db /absolute/path.db` 默认只读 dry-run；
`--verify` 只读核对投影 digest 与水位，任何不一致退出 1。`--apply` 是长期维护入口，不是一次性
修复脚本：必须停服、提供逐字节独立冷备、逐字确认绝对 DB，并回填同一 dry-run 的
`source_digest`、`plan_digest`、`rebuilt_projection_digest` 三项摘要。冷备与目标都必须通过完整性、
外键、全业务与文件摘要门禁；实现还会在 `BEGIN EXCLUSIVE` 内复核三摘要、无 running Match、
`execution_control` 为 `stopped + accepting=0`，且 `execution_jobs` 没有
`starting/running/settling`，并在提交前复核重建投影。语义已经一致的再次
apply 为 zero-write no-op。
完整命令和生产 No-Go 清单见 [RUNTIME.md](./RUNTIME.md#排行榜重建与上线-no-go)。禁止按
`created_at` 自制重放脚本，也禁止直接清空 policies/settlements 来“通过”验证。

### 6.6 游戏规则代际冷切命令

`python -m bzplat.backend.cli game-contract-cutover` 是 wire 协议不兼容升级的长期离线入口，会用经审核
的标准 ELF 为每个 Bot 建立新版本；`python -m bzplat.backend.cli game-rule-cutover` 专用于协议 ID 不变、
但 ruleset 与 rating pool 同时换代的离线切换，保留现有 Bot/version，marker manifest 固定为空。
同协议切换默认拒绝未终结赛事；产品方若决定让尚未开赛的 `open` 赛事直接使用新规则，必须逐个重复传入
`--migrate-unstarted-contest-id`。Store 会要求授权集合与全部 live 赛事精确相等，并在同一 cutover 事务中
只更新通过零 pairing/job/Match/result 门禁的赛事三元组，完整赛事/名册摘要绑定 `plan_digest`。
两条命令的 dry-run 与 apply 都要求 API/dispatcher/scheduler/上传预检已经停服，并提供与目标逐字节一致、
不同 inode、完整性与外键均通过的冷备；实现会在构造可迁移 Store 之前先取得目标数据库邻接的 dispatcher
flock 并验证冷备。dry-run 只迁移并规划同目录临时 DB copy，不写目标 DB 或创建 `bot_uploads`。
不兼容协议 apply 须回填标准 ELF 的精确 SHA-256/size、manifest digest 与 preimage；同协议规则 apply
须回填 `plan_digest`、空 manifest digest 与 preimage。两者都要求目标 DB 二次确认，提交后输出丢失的
幂等重试也只能使用原冷备，并在完整 marker/postcondition 复核通过时返回 no-op。不得用临时 SQL 或
一次性脚本替代，也不得把同 wire 规则变化静默混入旧评分池。完整 maintenance、冷备、验收、恢复接单、
恢复自动排位与成对回滚步骤见
[RUNTIME.md](./RUNTIME.md#五子棋规则代际冷切运行手册)。

> 返回 [doc/INDEX.md](./INDEX.md)
