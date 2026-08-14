# 运行时与资源限制

本文说明全来源执行队列、本地 Docker supervisor、Bot 沙箱资源、决策超时与恢复边界。

## Docker 硬限制

> **原则：一个平台进程、一个本地 socket、一个精确 label namespace。** 生产命令一律显式使用
> `docker --host unix:///var/run/docker.sock`，不读取或切换用户的 current context，不支持远端 daemon。

- `BZ_DOCKER_HOST` 只能是 `unix:///var/run/docker.sock`；显式覆写为其他值会在创建/迁移数据库前
  fail closed。父进程继承的 `DOCKER_HOST`、`DOCKER_CONTEXT` 与 TLS 环境会从 Docker 子命令环境
  清除/忽略，命令本身固定带 `--host`，因此用户 context 不能改变目标 daemon。
- 平台只接受 Linux x86_64 ELF Bot，基础镜像固定为 `debian:bookworm-slim`；不存在生产镜像覆盖。
  PE、Mach-O、ARM64 ELF 与脚本在上传校验阶段拒绝。
- 首次启动 Bot 前在平台准备阶段检查/按 `linux/amd64` 拉取镜像并复核架构；该阶段不计入 Bot
  决策时限或 Pencil 累计棋钟。镜像与 Docker 控制面故障属于平台恢复事件，不判 Bot 技术负。
- `BZ_BOT_LOCAL=1` 只供测试：直接运行兼容的本机 ELF，不施加 Docker 隔离，禁止生产启用。

LongRunning 对局会同时保留双方各一个容器；Traditional 在决策点创建当前行动方容器并在响应后清理。
两条路径复用同一个命令构造器，先 `create`，再 `start -a -i`，最后按精确 label 删除并确认清零：

| 参数 | 作用 |
|------|------|
| `--cpus=<档位>` | 节能沙箱为 1，赛事沙箱为 2；只能从版本化白名单解析，admin 不可任意组合或抬高 |
| `--memory=<档位>`、`--memory-swap=<档位>` | 节能沙箱均为 512m，赛事沙箱均为 2048m；内存与 swap 合计不超过所选档位 |
| `--network=none` | 完全断网 |
| `--read-only` | 根文件系统只读 |
| `--tmpfs /tmp:rw,exec,nosuid,nodev,size=64m` | /tmp 有界可写且可执行；禁 suid 与设备节点，根 fs 仍只读 |
| `--cap-drop=ALL` | 丢弃全部 Linux capabilities |
| `--security-opt no-new-privileges` | 禁止提权 |
| `--user 65534:65534` | 以 nobody 身份运行 |
| `--pids-limit=64` | 限制进程数量 |
| `--ulimit nofile=64:64`、`nproc=64:64` | 限制文件描述符和进程资源 |
| `--log-driver=none` | 禁止 daemon 持久收集 Bot 容器日志 |
| `--pull=never` | Bot 计时窗口内禁止 `docker run` 隐式拉取镜像 |
| `--entrypoint /app/bot` | 忽略基础镜像自带 Entrypoint/CMD，直接执行已校验 ELF |
| `--platform linux/amd64` | 运行目标固定为 Linux amd64 |

平台管理两种 Docker 档位：`platform_low` 每 Bot 1 CPU / 512 MiB，用于日常节能挑战、自动排位、人机 Bot 侧与上传预检；`platform_high` 每 Bot 2 CPU / 2 GiB，仅由锦标赛使用。用户端 `remote_local` 和真人座位不创建平台容器。所有参数均为**只读硬限制**，admin 面板不可抬高。容器名由
`instance namespace + request public_id hash + attempt + slot` 确定；容器同时带
`io.botbattle.instance/job/attempt/slot` 四个执行 label；容器创建阶段另带唯一
`io.botbattle.launch` token。namespace 优先取显式
`BZ_INSTANCE_KEY`（输入先归一化为小写，结果须为 1–48 位字母、数字、`.`、`_`、`-`），否则取绝对数据库路径的 SHA-256
摘要，因此 worktree/QA/生产互不误删。生产和每个并行 worktree 应显式使用稳定且互不相同的 key。

### 本地 supervisor 与恢复

- dispatcher 以数据库邻接的 OS 文件锁保证同一数据库只有一个平台进程负责派发；`execution_control`
  只保存 `stopped/starting/running/paused/stopping` 与暂停诊断，不保存 PID、lease 或 daemon incarnation。
  独立单例 `docker_launch_journal` 只记录尚未闭合的 create intent：token、确定性名称、owner、attempt/slot
  与 Linux host boot id；它不是运行中容器清单，也不提供热接管。
- execution 与上传预检共用同一 supervisor 和数据库邻接 `<db>.docker-launch.lock`。跨线程/进程 flock
  串行覆盖 create + start 与 job/instance cleanup；create 请求发给 daemon 前必须先把 journal 从 `idle`
  写为 `creating`，精确 name/token/label inspect 后才可写 `created`，StartedAt 或精确清场后才回 `idle`。
- 上传预检不伪装成对局 job，但所有 worker 共用一个进程级单槽 admission；因此任意时刻最多额外运行
  1 个 512 MiB/1 CPU 的预检容器，平台物理上界为执行队列 `max_sandbox_units + 1`。取消、失败或返回都会
  释放该槽；若 Docker 结果不确定，journal/paused 门禁仍会阻止释放后出现新的 create。
- 每次服务启动先停止并删除**本 instance namespace** 的所有容器，并连续两次查询 label/name/token 为 0；
  只有 journal 也闭合后才恢复持久请求。不会跨 namespace 清理，也没有多 leader 热接管或远端 Docker 证明。
  同一 host boot 上未获 ACK 的 `creating` 即使瞬时双零也不能排除 daemon 迟到创建，必须保持 `manual:`
  无限暂停；只有观察到精确 token 容器并删除，或确认 host boot 已改变且 namespace 精确双零，才能闭合。
- `create/inspect/start/rm` 的返回若不确定，dispatcher 持久进入 `paused`，保留当前容量，不生成
  `platform_error` 垃圾局。可自动证明安全的普通控制故障按 1–60 秒有界退避；上述同 boot create 歧义
  不自动重试，管理员恢复也必须重新执行精确清场，不能只把数据库标志改为 running。
- 正常 attempt 只有在 match 已写终态、且该 job/attempt 的 label 连续两次为 0 后，才从 `settling`
  进入终态并释放容量。应用停止先拒绝新 job，再取消/等待本进程任务并尽力清理；进程崩溃不做跨进程
  接管，由下一进程统一清场与补偿。

## 决策超时

- `GameSpec.time_budget_per_side=None` 的游戏（当前 holdem / gomoku）使用代码常量 **60 秒 / 决策**；管理端、数据库和环境变量均不能覆盖。
- Pencil 的 `GameSpec.time_budget_per_side=900`：双方各有一只独立、固定 **900 秒（15 分钟）累计棋钟**，Bot-vs-Bot 与人类对战走同一契约；每次等待只使用该座位的剩余时间，不能靠多回合重置。该固定规则不读取 `action_timeout_sec`，admin 不可改。
- Bot 单步超时或 Pencil 累计棋钟耗尽在第一次发生时即终止对局，持久化为 `completed + reason=timeout + technical_loss=1`；不会生成代替动作继续对局。Bot-vs-Bot 技术结果进入评分/赛事积分，人机局由人类获胜但不计 Glicko。人类侧逐回合/累计超时仍走人类 inactivity 与游戏裁判逻辑。
- 人类对战的 `human_action_timeout` 默认仍为 **120 秒 / 回合**，用于等待 WebSocket 落子的内层保护；Pencil 同时受外层 900 秒累计棋钟约束，以先到的限制为准。
- 棋钟成功决策写入 `time_used {seat,used,remaining,budget}`，耗尽写入 `time_out {seat,used,budget}`；事件进入回放/SSE，点格棋对局页据此展示双方剩余时间和「超时」标记。
- **故障语义**（详见 [对局](#/wiki?slug=guide)）：Bot 信封/response 格式错误 → `completed + reason=protocol_error + technical_loss=1`；Bot 决策超时 → `completed + reason=timeout + technical_loss=1`。两者在首个故障终止，回放写 `technical_incident`，结果只公开 `technical_incident_count`、`technical_incidents_by_seat` 与最多 3 条 `technical_incident_samples`；结构化日志带 `match_id/bot_id/version_id/runtime/seat/turn` 且不记录原始 stdout/私有路径。历史回放中的旧错误事件只在服务端读取时归一化，不作为新写入或对外字段。Bot-vs-Bot 评分，人机局不评分；格式正确但游戏内非法动作仍归裁判。中途崩溃由引擎计分判负；Bot-vs-Bot 启动失败结算为 `completed + technical_loss`，human 启动失败为 `aborted + bot_crashed`。执行队列中的 Docker 控制故障不伪造 `platform_error` 对局：dispatcher 暂停并在 label 清零后按 attempt 是否已有公开事件精确补偿；上传预检的平台故障返回 503，不改变原激活版本，也不阻塞主事件循环。
- **中止公开边界**：中止对局的 replay/SSE/WS 终局只发送 `{"type":"error","reason":"稳定原因码"}`；不发送 `message`、异常文本或路径。未知/历史自由文本统一投影为 `platform_error`，管理员中止固定为 `admin_aborted`，详细诊断只写结构化日志。pending/running 的 `reason` 为空，页面不会在对局仍运行时提前显示“正常结束”。
- **完成公开边界**：完成对局的 replay/SSE/WS 终局只发送 `match_end {winner,reason,deltas}`；`reason` 只能取 `schema.PUBLIC_MATCH_COMPLETED_REASONS`，未知英文/中文自由文本统一为 `completed`。公开详情中的 `result` 只保留进度、净结果、复式 leg 与脱敏技术故障摘要，执行用 `match_config` 和其他诊断字段不对外返回。
- **事件公开边界**：非终态 replay/live 也只允许逐事件声明的字段；未知事件类型整条丢弃，已知事件的额外诊断字段丢弃。活跃真人德扑的公开观赛隐藏双方底牌与 `your_turn.request`，本人鉴权 WebSocket 只获得自己座位的底牌和请求；结束后才提供完整回放。SSE/WS 快照与可见性元数据全部构造成功后才注册队列；故障不留孤儿订阅，元数据缺失时默认按最严格可见性投影。
- 本平台默认 Traditional（每个决策点重启进程）；显式选择 LongRunning 并完成精确握手后才整场长驻。两种模式使用相同 stdin/stdout 单行 JSON 信封；缺失/错误握手立即协议判负，不回退。

平台不按编程语言调整时限。无累计棋钟的游戏统一使用
`runtime/config.py::ACTION_TIMEOUT_SEC`；Pencil 使用 GameSpec 固定的每方 900 秒累计预算。

## 全局执行容量

所有会实际运行 Bot 的来源——人工挑战、人机、赛事、自动排位——先进入同一持久队列。每场都占
**1 match slot**；`sandbox units` 只计算平台实际管理的 Docker 座位，本地 Bot 和真人座位均为 0：

| 对局来源 / 座位环境 | sandbox units | 冻结的主机资源需求 |
|----------------------|---------------|----------------------|
| 日常挑战或自动排位：节能 + 节能 | 2 | 2000 毫核 / 1024 MiB |
| 日常挑战：节能 + 本地 Bot | 1 | 1000 毫核 / 512 MiB |
| 日常挑战：本地 Bot + 本地 Bot | 0 | 0 / 0；平台只运行裁判 |
| 人机：节能 + 真人 | 1 | 1000 毫核 / 512 MiB |
| 锦标赛：赛事 + 赛事 | 2 | 4000 毫核 / 4096 MiB |

Traditional 实际同时存活的容器可能更少，但不会因此抬高硬上限；本地 Bot 虽不占 Docker，仍占唯一裁判对局槽，不会绕过排队。

```text
max_match_slots  = 1
host_cpu_budget  = min(进程 affinity、进程可见逻辑 CPU、各级 cgroup CPU quota)
host_memory_budget = min(物理内存、各级 cgroup memory limit)
effective_budget = min(上述探测值、显式的仅收紧启动注入)
```

- `max_match_slots=1`，`max_sandbox_units=2`。显式启动参数、CPU 数量和管理员设置都不能放大；每次 claim 在一个
  `BEGIN IMMEDIATE` 中同时要求：活跃 job 的 slot 未满、实际全局 `running` match 数小于
  `max_match_slots`、sandbox units 可容纳当前 job，并且冻结的 CPU/内存需求不超过当前主机预算。赛事主机不足
  4 CPU 或 4 GiB 时该任务保持排队并给出资源不足原因，绝不静默降为节能档。任何来源都不能绕过其中任一维度。
- `starting/running/settling` 都占容量；match/replay/rating policy 只在 claim 时同事务创建和绑定，
  单纯排队不产生“pending 对局”。未纳入新队列的历史 running match 也按 1 slot + 保守 2 units 计入。
- job 入队时冻结两个座位的环境、`profile_version`、sandbox/CPU/内存向量；claim 重新用不可变历史 registry
  校验并复制版本到 Match，runner 的 Traditional 每回合、LongRunning、复式与人机 Bot 侧都只解析该冻结版本。
  未知版本、环境不兼容或快照漂移均在启动进程前 fail closed，不能回退到部署时的当前档位。
- 人工/人机按用户同时活跃最多 1 条、排队最多 4 条；四类来源合计只占用唯一的全局 slot。
  基础优先级为人工/人机 > 赛事 > 自动，但每 60 秒增加一次无上限 aging，自动请求最终一定能越过
  后续到达的高优先级请求，不会永久饥饿。
- rated job claim 前还须通过评分投影 readiness 与双方 Bot 的 rated-overlap 门禁。真正新建且没有任何旧业务表的库
  会在初始化事务内认证其规范空投影；任何已存在的 schema 仍必须走离线 rebuild，即使当时没有对局也不会被启动自动信任。
  容量可在容器清零后释放，
  但同一 Bot 的 completed 未结算局仍阻止下一条 rated job，避免 Glicko 顺序重叠。

## 部署排空状态机

计划部署使用独立、持久的 `deployment_drain_requested` 控制位，不能用 dispatcher 的
`paused/stopped` 或临时 `pause_reason` 代替。管理员开始排空时，Store 在一个
`BEGIN IMMEDIATE` 事务内同时写入 `deployment_drain_requested=1`、`accepting=0` 和
`auto_enabled=0`。因此事务边界之前已经入队或 claim 的工作保持原生命周期；边界之后的新挑战、
人机、重试、赛事派发和 Bot 新建/版本上传被明确拒绝。已有 queued job、冻结版本、赛事 pairing 和
自动排位 decision 均原样保留，不取消、不改序，也不生成技术赛果。维护期间 queued job 的公开读模型
返回 `blocked_code=deployment_maintenance` 和“部署维护中，恢复调度后继续排队”；恢复后自动回到真实
容量阻塞或动态 ETA，持久 job 本身不写入临时维护原因。

排空不会停止 dispatcher loop：当前 `starting/running/settling` 继续完成、取消、评分结算与精确
Docker 清理，但不再 claim 或补充自动任务。赛事 scheduler/reconcile 在检查 Bot 可用性、绑定 Match、
写赛事运行态或技术结果之前检查同一 admission gate；维护期间只保留 pending pairing，恢复后再派发。
Bot 上传与开始排空共用进程内 admission mutex；边界前已进入的 multipart/预检计入活动上传，边界后在
读取请求体、写暂存文件或启动预检容器前拒绝。

`maintenance.ready` 永远由当前事实派生，不持久化绿色状态；它同时要求：

- drain 已请求，dispatcher 为 `running` 且 `accepting=0`；
- 没有 `starting/running/settling` job，也没有缺少 execution job 所有者的 legacy/异常 `running` Match；
- Docker launch journal 为 `idle`，没有 active 本地 Bot lease；
- 没有本进程仍在执行终局/赛事回调的 execution task、评分/赛事应用恢复，也没有活动上传；
- 上述任一探针缺失或异常时 fail closed，并在管理员投影的 `readiness_unavailable` 给出诊断项。

部署排空与运行故障暂停是正交状态。排空期间若 Docker 控制不确定而进入 `paused`，后台有界重试不会
自行跨过管理员正在准备的部署边界；管理员须使用“清场并恢复”执行精确 namespace cleanup、attempt
补偿和赛事/评分对账。恢复成功后状态为 `running + drain=1 + accepting=0`，不会接新任务。正常重启也会
先执行相同清理/恢复链，再回到这一状态；`start/recover/resume/close` 均不得清除 drain。
管理员恢复请求若在已切换为 `running` 后被客户端取消，dispatcher 必须原子退回
`paused + accepting=0`，不能把未完成的评分/赛事对账暴露为健康状态。管理员中止对局从取消 runner、
写终局 replay 到赛事回调返回的整个协程都计入 owned task；namespace 清场须等待该 handoff 完成。
上传请求取消时，活动计数递减与全局上传 permit 释放位于不可取消的短 cleanup 区，避免部署永远卡在
“仍有上传”或后续上传永久拿不到槽位。

只有管理员显式结束排空且上述 blockers 全部为零时，单个事务才清除 drain 并把 `accepting` 置 1。
自动排位仍保持关闭，必须另行通过自动排位开关启用。这保证进程重启、运行环境恢复或重复请求都不会
意外把平台重新开放。

## 运行模式边界

| 模式 | 进程 | 请求 |
|------|------|------|
| Traditional | 每个决策点启动并停止 | 每次完整 `requests[]/responses[]` |
| LongRunning | 整场一个进程 | 首回合完整历史；精确握手后为单 `request` |

上传预检与正式首回合使用同一模式、同一信封、同一 response 校验；预检始终使用节能档
`platform_low`（1 CPU / 512 MiB），不会占用赛事档。LongRunning 未在
握手时间窗内输出固定字符串即技术负，runner 不切换模式。详见[协议规范](#/wiki?slug=protocol)。

## 德州牌型参考

![德州扑克牌型](/wiki-assets/TexasHoldemHandType.jpg)

## 持久执行请求与自动排位

### 状态机与补偿

`execution_jobs` 保存面向用户的一条持久请求；`execution_job_attempts` 保存每次不可复活的 Match 尝试：

```text
queued --原子 claim/建 Match--> starting --> running --> settling --label=0--> completed
   |                              |           |             |          cancelled
   +--排队取消--> cancelled       +-----------+-------------+--------> interrupted
```

- `queued` 只有冻结的 Bot/version 与编排快照，没有 match/index/replay/policy。claim 同一事务创建四者、
  绑定赛事 pairing（如有）、写 attempt 并进入 `starting`；runner 真正写 Match `running` 后才进入
  `running`。Match 终态后 job 先进入 `settling`，精确清理确认前仍占容量。
- `starting` 启动任务失败且 replay 无事件时，精确删除本 attempt 的 match/index/replay/policy 并原位
  requeue。重启恢复同样只在整个 instance namespace 已清零后执行该补偿。
- claim 建 Match 前会重新校验冻结版本；失效时 manual/human 变为 `interrupted + retryable`，
  auto/contest 变为 `cancelled + non-retryable`，auto decision 同步取消，四类均不创建 Match。该分流避免
  对无人可修复的后台请求无限重试，同时让用户发起的请求保留明确恢复入口；contest pairing 会复位为
  `pending + match_id=NULL` 并把 `scheduled_at` 至少后移 30 秒，避免 scheduler 对同一坏版本热循环。
- crash 后若 replay 已有公开事件，旧 Match 保留为 `aborted + orphan_after_restart` 审计，旧 attempt
  标为 `interrupted`，绝不复活同一 Match。只有 manual/human 对用户显示可重试；auto/contest 的旧 job
  固定 `retryable=0`，分别由自动 producer 与赛事 pairing 状态机决定是否生成/排入后续工作，不能经
  通用 `/retry` 复活。运行期基础设施失败时，manual/human 进入可重试 `interrupted`；auto/contest 的
  同一持久 job 以 `failure_count/next_attempt_at` 执行 1、2、4…最多 60 秒退避，赛事 pairing 的
  `scheduled_at` 同步后移，避免恢复成功后每秒创建 attempt/Match 的热循环。普通无错误重启仍即时恢复。
  不会为基础设施故障伪造 `platform_error` 局或重复 active job。
- 已写 Match 终态但进程尚未来得及确认清理的 job 恢复为 `settling`；清理与后续评分 settlement 均幂等。
  用户可取消本人 queued manual/human，管理员可按权限取消更广范围；取消 active 请求只置标记并由
  dispatcher 经 orchestrator 安全收敛，精确 label 清零前不释放容量。管理员取消 queued contest 时 pairing
  保持 `pending + match_id=NULL`，并将 `scheduled_at` 至少后移 30 秒；这与 claim 前版本失效使用同一最小
  退避边界，防止 scheduler 立刻重建刚取消或仍不可运行的请求。
- `finalize_ready` 即使看到 `cleanup_state=confirmed`，也必须复核对应 Match 已是 completed/aborted；
  若仍为 pending/running 则抛出 invariant error、保留 `settling` 与容量，绝不把非终态 Match 当作已收尾。
  对 runtime interrupted 的 retryable 也按 source 决定：manual/human 为 1，auto/contest 为 0。

### 自动公平生产者

自动排位只是全局队列的一个 `source=auto` 生产者，不再拥有独立 admission、dispatcher 或物理 fence。
它持续补足最多 6 条预告请求；没有每日上限、空闲等待、Bot 冷却、陈旧阈值或“每轮最多几场”。
唯一可变项 `execution_control.auto_enabled` 由 `PUT /api/admin/auto-match` 严格 boolean 修改：关闭只停止
自动生成和自动 claim，已有自动局自然结束；人工、人机、赛事完全不受影响，已排自动请求留在队列可见。
再次开启立即续跑。`BZ_QA_INSTANCE=1` 的代码能力门仍禁止开启自动生产。

公平选择由 SQLite 持久状态推进，不借用可被人工挑战影响的 `ratings.last_played_at` 或 `pair_stats`：

1. 游戏按固定游标轮转；`bootstrap/established` 两条内部服务 lane 持久交替。bootstrap 目标场数只帮助
   新 Bot 获得自动服务，与公开排名资格常量彼此独立。
2. 先按每游戏 auto 专属 owner 服务次数/最近轮次排序；同一 owner 在自动活跃请求中最多占一席，
   owner 内再轮转服务最少的 Bot。
3. 对手依次按 Bot pair、owner pair、Rating 距离、服务债务和稳定 ID；座位用 auto 专属计数平衡。
   双方必须同游戏、不同 Bot、不同 owner，same-owner 评分仍保持中性。
4. 决策永久记录策略版本、游标/lane、服务计数、pair 次数、Rating 差、座位债务、冻结版本、
   `job_public_id` 和每次 attempt 终态；通用 job 是生命周期真值，`auto_match_decisions` 是选择审计。

公开 `GET /api/execution-queue` 返回 dispatcher、双容量向量和脱敏 active/queued；POST 挑战/人机返回
HTTP 202 与 request public_id、真实 `ahead_jobs`、`ahead_sandbox_units`、容量及动态 ETA 区间。
`GET/DELETE /api/execution-requests/{public_id}` 用于查询/取消，interrupted 的人工/人机可 POST `retry`。
所有投影都由白名单构造，不返回内部 DB id、version id、二进制路径、checksum、token、match_config 或
Docker 诊断。ETA 明确是随对局时长、优先级和资源变化的区间，而非承诺时间。

### 旧库迁移与 schema 幂等边界

- Store 迁移新增 `execution_jobs`、`execution_job_attempts`、`execution_control`、`local_ai_agents` 与
  `local_ai_leases`。队列表重建时为每个 job 增加双方环境、本地 agent 引用、sandbox/CPU/内存与
  `profile_version` 冻结快照；保留全部 job/attempt ID 和生命周期。既有任务按历史 v0 迁为节能沙箱，
  人机的真人位置保持 0 sandbox unit；不回填 token 或把历史任务推断为用户端本地 Bot。旧自动队列的 queued
  记录转换为 `source=auto,status=queued`；旧 active/dispatched 若无法证明旧 worker 与容器已停止，
  会转为仍占容量的 `settling` attempt，并把 control 置为 `paused`、写入 `manual:` 原因。普通重启不得
  自动跨越该人工确认边界：运维必须先证明旧平台/容器已停，再从管理端恢复以执行当前 instance 的
  精确 label 清场。
- `local_ai_agents` 只保存连接 token 的 SHA-256 与短提示；新 token 只在创建/轮换响应中出现一次。
  `local_ai_leases` 记录 agent 与 job/attempt/位置的占用和释放。服务启动时只有取得 dispatcher flock 的
  唯一 owner 才能清除上个进程遗留的在线态并释放活跃租约，未取得 flock 的实例不得写这些临时状态。
- 迁移先校验旧 row/decision/状态映射的一致性，任何歧义 fail closed；成功映射后才删除已退役的
  `auto_match_queue`、`auto_match_control`、`auto_match_dispatcher`、daily claims 和旧 runtime/auto KV。
  这些名称只描述迁移来源，不是现行队列或开关；现行唯一自动开关为
  `execution_control.auto_enabled`。
- 存量评分投影只标为 `legacy-unverified`，摘要留空；启动迁移不擅自重放历史。完成下节的离线
  `rating-rebuild` 前，projection readiness 会阻止 rated/auto claim，因此这是升级上线的明确 No-Go，
  不是可忽略告警。
- 当前 39 个现行 trigger 由 `_ensure_trigger` 规范化比较。定义相同的二次打开不执行 trigger
  `DROP/CREATE`，`schema_version` 不因这些 trigger 改变；缺失/过期定义修复一次，对象类型冲突、非法
  identifier 或创建后定义不符会抛错并由 Store 事务回滚。Store 的其他迁移仍可能执行 DML，所以整体
  只保证逻辑幂等，不保证数据库文件字节、SHA-256 或 mtime 不变。

### 排行榜重建与上线 No-Go

execution job 创建事务会冻结评分资格：不同所有者 Bot 挑战/ladder 计分；同 Bot、自有不同 Bot、人机与
赛事均为中性局。中性局完成后仍写 exactly-once settlement marker，但不改 ratings、历史、胜负或
pair_stats；对局详情同时返回创建时资格 `rated/rating_reason` 与唯一公开的 marker 布尔真值
`rating_settled`（内部 order/status 不出公共契约），两者
不可互相代替。符合资格的在途、完成未落 marker、完成已落 marker、中止对局分别显示“预计计分”、
“待结算”、“已计分”、“已中止未计分”。历史结算首次迁移按
`(COALESCE(ended_at, settled_at), match_id)` 固化为连续 `settled_order=1..N`，以后完成事务先冻结
单调序号，恢复和离线重放只认该序号，绝不能再按 `created_at` 猜顺序。

旧库不会在启动迁移时冒险自动重放。`rating_projection_state` 未经当前策略验证或落后于 settlement
水位时，自动排位一律暂停。生产升级前必须在停服维护窗完成以下流程，否则是发布 **No-Go**：

在线事务还要求 `mutation_revision == trusted_mutation_revision`。评分/Bot universe/source 输入的每次
DML 都由数据库递增前者；只有写前完整可信的显式 mutation guard 才能在同一事务同步后者和全部
摘要。completed 后合法、连续的未结算尾部可跨重启继续，但任何 stale 状态都不能被后续
ensure/评分/中性 marker/可见性写“洗白”；通用硬删、换 `game_id` 与无 marker 的低层评分写必须走
下述离线 rebuild 才能恢复自动排位。该认证从 `owner-neutral-v3` 起生效；升级前遗留的 v2 标记
没有可信 mutation lineage，即使摘要吻合也必须先离线重建。

```bash
# 1. 默认只读 dry-run；保存同一只读快照的三项摘要与全榜 diff
python -m bzplat.backend.cli rating-rebuild --db /absolute/path/botzone.db

# 2. 停止 API/worker/scheduler，逐字节 cp 冷备后回填三项摘要
python -m bzplat.backend.cli rating-rebuild \
  --db /absolute/path/botzone.db --apply \
  --expect-source-digest <reviewed-source-digest> \
  --expect-plan-digest <reviewed-plan-digest> \
  --expect-rebuilt-projection-digest <reviewed-rebuilt-projection-digest> \
  --confirm-db /absolute/path/botzone.db \
  --backup /absolute/path/botzone.cold-backup.db \
  --confirm-service-stopped --confirm-cold-backup

# 3. 仍在停服窗口验证；退出码必须为 0
python -m bzplat.backend.cli rating-rebuild --db /absolute/path/botzone.db --verify
```

dry-run/verify 用 SQLite 只读 URI，并显式 `BEGIN` 固定单一读快照，不改变文件字节或 mtime；该快照同时
产生 immutable source、Bot universe plan 与 rebuilt projection 三项 digest；plan 的 Bot universe 精确包含
线上榜可见性消费的 `id/game_id/is_active/format/os/arch`，任何 active 或二进制 metadata 漂移都使已审核
plan 失效。rated source 还要求 `rated ⇔ rating_reason=eligible`，`deltas` 必须恰为两个非 bool 整数且零和。
全榜 diff 复用线上榜的 active Linux/amd64 ELF eligibility、独立公开排名资格阈值与
`rating → matches_played → bot_id` 排序。apply 除绝对路径二次确认和停服声明外，要求冷备
与目标双方 `integrity_check=ok`、`foreign_key_check=0`，并在首个 DML 前同时核对三项已审核 digest、
完整业务 digest 和数据库文件 digest；旧业务备份即使评分源相同也不能通过。上述检查在
`BEGIN EXCLUSIVE` 内对目标再次执行，并复核无 running match、`execution_control` 必须是
`stopped + accepting=0`，且 `execution_jobs` 不得有 `starting/running/settling` attempt。故障整事务
回滚。语义投影与验证水位都已一致时，二次 apply 直接 rollback，保持数据库字节、mtime 与 rebuilt_at
不变，是真正 zero-write no-op。它只重建 `ratings`、每 Bot 最近 200 条 `rating_history`、`pair_stats`
和 projection state，不删除、不重排 `match_rating_policies` 或 settlements；已删除 Bot 仍在内存中参与
Glicko 传播，但不会写回带 FK 的投影表。直接命中的污染 Bot 不是完整影响范围，是否可上线必须以
全榜重建 hash、Rating 与名次 diff 为准。

> **可见性**：自动 ladder 对局会出现在首页最新对局和排行榜队列，可直接进入观赛。

## 代码配置与只读诊断

`bzplat/backend/runtime/config.py` 是运行参数的唯一真相源，集中声明决策超时、默认并发、
全局执行容量/aging/用户上限、自动 bootstrap 目标、独立公开排名资格与赛事 scheduler 参数。
阶段休息时间直接属于各代码模板。修改须走代码评审、测试、
部署；旧 `platform_settings` 同名记录只作为历史数据保留，启动不 seed、不读取、不回写。

`GET /api/admin/settings/runtime` 仅供诊断，响应明确包含 `source="code"`、
`mutable=false`、当前机器 ceiling、实际生效并发、队列计数和冻结配置。不存在
`PATCH /api/admin/settings/runtime`，管理端也不展示“运行时”Tab。

赛制模板同样由 `games/<game>/templates.py` 通过游戏注册表聚合。公开
`GET /api/contests/templates` 返回 `source="code"`、`mutable=false`；历史
`contest_templates` 表不再 seed/对账或参与解析，不存在 `/api/admin/templates*`。
