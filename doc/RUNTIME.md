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

- `GameSpec.time_budget_per_side=None` 的游戏（当前仅 holdem）使用代码常量 **60 秒 / 决策**；Gomoku 与 Pencil 为每座位 **900 秒累计棋钟**。管理端、数据库和环境变量均不能覆盖。
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

Traditional 实际同时存活的容器可能更少，但不会因此抬高硬上限；本地 Bot 虽不占 Docker，每场仍占 1 个裁判对局槽，不会绕过排队。

```text
max_match_slots  = 2
max_sandbox_units = 4
host_cpu_budget  = min(进程 affinity、进程可见逻辑 CPU、各级 cgroup CPU quota)
host_memory_budget = min(物理内存、各级 cgroup memory limit)
effective_budget = min(上述探测值、显式的仅收紧启动注入)
```

- `max_match_slots=2`，`max_sandbox_units=4`。这是按 8 vCPU / 16 GiB 主机设定的代码硬顶：最重赛事一场为
  2 个赛事 Bot，共需 4 CPU / 4 GiB；两场合计 8 CPU / 8 GiB，并为系统、应用与 SQLite 保留约 8 GiB 内存。
  显式启动参数和管理员设置不能把硬顶放大到 2 以上；affinity、逻辑 CPU、cgroup 配额、物理内存和 cgroup
  内存上限仍可进一步压低实际并发。每次 claim 在一个
  `BEGIN IMMEDIATE` 中同时要求：活跃 job 的 slot 未满、实际全局 `running` match 数小于
  `max_match_slots`、sandbox units 可容纳当前 job，并且冻结的 CPU/内存需求不超过当前主机预算。赛事主机不足
  4 CPU 或 4 GiB 时该任务保持排队并给出资源不足原因，绝不静默降为节能档。任何来源都不能绕过其中任一维度。
- 上传预检不属于 execution job，仍由独立单槽 admission 控制；它可在双赛事运行时短暂再占 1 CPU / 512 MiB。
  因此 8 vCPU 规划下双槽表示饱和吞吐上限，容器 CPU quota 短时合计可达 9 vCPU，并不承诺零超售或低延迟；
  需要严格 CPU 预留的部署必须再让预检参与统一资源门，不能仅靠调高/调低管理员设置（该设置本来也不存在）。
- `starting/running/settling` 都占容量；match/replay/rating policy 只在 claim 时同事务创建和绑定，
  单纯排队不产生“pending 对局”。未纳入新队列的历史 running match 也按 1 slot，并从追加式资源档位
  registry 推导双 Bot 最大向量计入；当前即保守计作 2 units / 4000 毫核 / 4096 MiB，不能低估后再放入第二场。
- job 入队时冻结两个座位的环境、`profile_version`、sandbox/CPU/内存向量；claim 重新用不可变历史 registry
  校验并复制版本到 Match，runner 的 Traditional 每回合、LongRunning、复式与人机 Bot 侧都只解析该冻结版本。
  未知版本、环境不兼容或快照漂移均在启动进程前 fail closed，不能回退到部署时的当前档位。
- 人工/人机按用户未终态请求（queued/starting/running/settling）合计最多 4 条，其中同时活跃最多 1 条；
  因而没有活跃局时最多 4 条排队，有 1 条活跃时最多另排 3 条，人机来源另限同一时刻仅 1 条未终态请求。
  四类来源合计共享 2 个全局 slot。
  `contest_share_slots=1` 只在存在可运行的 manual/human 前台请求时限制赛事优先占用；auto 不属于该份额，
  不会让第二场赛事给自动排位让槽。若 manual/human 因资源或业务门禁暂不可 claim，dispatcher 可放宽该份额
  以免物理容量空转。manual/human 与 contest 在前台类内保持基础优先级并每 60 秒增加一次无上限 aging；
  auto 是严格后台类，不参与跨来源 aging，也不会出现在前台请求的 `ahead_jobs`、`ahead_sandbox_units` 或 ETA 中。
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

### 五子棋规则代际冷切运行手册

#### 历史首切：自由棋 → CCGC 2013 v1（协议变更）

五子棋从 `gomoku_freestyle_v1 / gomoku_xy_v1 / gomoku_freestyle_rating_v1`
升级到 `gomoku_ccgc_2013_v1 / gomoku_action_v2 / gomoku_ccgc_2013_rating_v1`
是一次**停服 hard cutover**，不提供旧协议兼容或在线迁移。正式入口为
`python -m bzplat.backend.cli game-contract-cutover`，且只能按以下顺序执行：

1. 按上一节请求 maintenance，轮询到 `maintenance.ready=true`；确认活动 job/Match、上传、Local AI
   lease 与 Docker launch journal 全部静默。此时 drain 已 requested、`accepting=0`、`auto_enabled=0`。
2. 停止 API、dispatcher、scheduler 与上传 worker，核对原 PID、50380 监听和本 instance 容器均已消失。
   保留数据库邻接的 `.execution-dispatcher.lock` inode，不能删除或替换它。
3. 停服后制作数据库逐字节冷备，并用 `cmp`、`PRAGMA integrity_check`、
   `PRAGMA foreign_key_check` 验证；备份必须是不同文件/不同 inode。记录当前代码 release 与冷备路径，
   二者构成唯一允许的回滚对。
4. 对最终标准 ELF 执行 `file`、`sha256sum`、`stat -c %s`，将审核后的绝对路径、SHA-256 和 size
   原样传给 dry-run。dry-run/apply 都会先从原始绝对 DB 路径取得 dispatcher flock，并在构造任何
   `Store` 前强制验证目标与冷备不同 inode、逐字节 SHA-256 相同，且双方
   `integrity_check=ok / foreign_key_check=0`；锁被占用、确认缺失或冷备陈旧时 Store 不会打开，目标
   schema/bytes/mtime 不变。dry-run 随后只迁移同目录的临时 DB copy 并在 finally 删除，不迁移目标、
   不创建 canonical `bot_uploads`；即便如此仍必须停服且先有可恢复冷备，apply 会重新验证同一 preimage。
5. 审核 dry-run 的 `bot_count`、`from_contract/to_contract`、每 Bot 新 `vN`、canonical 路径、source
   checksum/size、`existing_runtime_modes`、`replacement_runtime_modes`、
   `runtime_mode_change_count` 与 `manifest_digest`。每个 Bot 的 runtime mode 必须原样继承，
   `runtime_mode_change_count` 必须为 0；标准 ELF 会对 manifest 中每种模式分别预检。
   任何 Bot 缺失、路径落入仓库 `samples/`、两个 Bot 共用目标路径，均为 No-Go。
6. 回填同一 `manifest_digest` 与 `target_preimage_sha256` 执行 apply；后者在任何 Store
   打开/迁移前绑定已审核的停服前镜像。首次 apply 只接受
   `target == cold backup == target_preimage_sha256`。若事务已经提交但终端输出丢失，完全相同的命令
   可以带原冷备重试：此时只允许 `cold backup == target_preimage_sha256` 且原始目标库已有同
   `cutover_id + manifest_digest` marker，再由 Store 完整复核 marker、代际链、Bot/version 资产、评分
   归档和活动任务后返回 `already_applied=true`；除此之外 DB 发生任何变化都必须重做 dry-run。CLI 在同一个预先取得的
   dispatcher flock 内完成现行
   `GameSpec` preflight、逐 Bot 私有 ELF staging 与单事务元数据切换；preflight 失败时不创建版本文件、
   不写 cutover marker，也不改变数据库。生产不得设置 `BZ_BOT_LOCAL=1`。

示例（绝对路径、摘要、size 与 cutover id 必须替换为本次已审核值）：

```bash
# dry-run：停服、冷备完成后执行；保存完整 JSON 并审核 manifest_digest/runtime-mode 报告
python -m bzplat.backend.cli game-contract-cutover \
  --db /absolute/path/botzone.db \
  --cutover-id gomoku-ccgc-2013-v1-<deployment-id> \
  --game-id gomoku \
  --from-ruleset gomoku_freestyle_v1 \
  --from-protocol gomoku_xy_v1 \
  --from-rating-pool gomoku_freestyle_rating_v1 \
  --source-binary /absolute/path/gomokubot_linux_amd64 \
  --source-sha256 <reviewed-sha256> \
  --source-size-bytes <reviewed-size> \
  --upload-note 'platform standard Gomoku CCGC 2013 v2 hard cutover' \
  --backup /absolute/path/botzone.pre-gomoku-cutover.db \
  --confirm-service-stopped --confirm-cold-backup

# apply：回填同一次 dry-run 的 manifest_digest；DB 与冷备路径均须再次逐字确认
python -m bzplat.backend.cli game-contract-cutover \
  --db /absolute/path/botzone.db \
  --cutover-id gomoku-ccgc-2013-v1-<deployment-id> \
  --game-id gomoku \
  --from-ruleset gomoku_freestyle_v1 \
  --from-protocol gomoku_xy_v1 \
  --from-rating-pool gomoku_freestyle_rating_v1 \
  --source-binary /absolute/path/gomokubot_linux_amd64 \
  --source-sha256 <reviewed-sha256> \
  --source-size-bytes <reviewed-size> \
  --upload-note 'platform standard Gomoku CCGC 2013 v2 hard cutover' \
  --apply --expect-manifest-digest <reviewed-manifest-digest> \
  --expect-target-preimage-sha256 <reviewed-target-preimage-sha256> \
  --confirm-db /absolute/path/botzone.db \
  --backup /absolute/path/botzone.pre-gomoku-cutover.db \
  --confirm-service-stopped --confirm-cold-backup
```

apply 的一个事务会把每个既有五子棋 Bot 的旧 version 标为 retired，新建使用标准 ELF、
`gomoku_action_v2` 的固定 `vN` 并设为 current；不会原地修改旧 version 的路径、checksum、size 或版本号。
新 `vN` 继承每个 Bot 原有的 `traditional`/`longrunning` 模式，不做全局改写。执行 apply 前必须
确认环境未设置 `BZ_BOT_LOCAL`；正式 CLI 会拒绝本机 subprocess 预检，只允许生产 Docker runtime。
旧 Gomoku Match/赛事仍冻结为 legacy 三元组，旧 queued/interrupted job 不可继续或重试。旧评分投影按
legacy pool 归档，新 pool 从每 Bot 1500/350/0.06、0 场开始；离线 rating rebuild 只重放各游戏 active
pool，不把 legacy Gomoku settlement 带回新榜。每个新 version 都必须落在数据库同目录的
`bot_uploads/<bot_id>/vN/bot.bin`；内容可以相同，但路径与 inode 必须逐 Bot 唯一，旧文件不改。

apply 后保持停服，至少完成以下验收后才启动：数据库 `integrity_check=ok`、FK 零违规；
`rating_pool_state` 三元组与代码 GameSpec 完全一致；cutover marker 代际边形成唯一、无断链/分叉的
from→to 三元组链，链尾等于 active contract；所有 Gomoku current version 均等于 dry-run manifest 为各
Bot 固定的 vN（原 `MAX(version)+1`）、未 retired、SHA/size 与标准 ELF 相同；所有旧 version 均
retired；新文件路径/inode 数均等于 Gomoku Bot 数；active legacy job
为 0；首次恢复服务前新 Gomoku ratings 的 `matches_played` 总和为 0。用完全相同参数和原冷备再次 apply
必须返回 `already_applied=true`、同一 manifest digest 且不新增 marker/version。后续若再次更换 wire 协议，
仍须使用从未在该游戏 cutover 链出现过的新 protocol ID；协议不变而只换 ruleset/评分池时必须改用下节
`game-rule-cutover`。ruleset、protocol 与 rating pool 均禁止 A→B→A 式回用；历史 marker 与其审计
vN/归档永久保留，只有链尾代际保持 active。新评分池产生合法对局后，幂等复核允许评分继续演进，
不要求回到零。

新代码启动会在任何 runtime 启动前核对所有 active contract；legacy DB 或三元组漂移会明确拒绝启动。
runner 也会在启动 Bot 前核对 Match 冻结三元组。启动健康后 maintenance 仍应保持 requested、
`accepting=0`、`auto_enabled=0`；完成真实对局/回放/排名验收后，先
`DELETE /api/admin/execution-queue/maintenance` 恢复接单，再由管理员显式
`PUT /api/admin/auto-match` 提交 `{"enabled": true}` 恢复自动排位。

若 apply 前失败，修正原因后沿用同一冷备和已审核计划重试。若 apply 后必须回滚，按以下 fail-closed
步骤执行，数据库与代码 release 必须作为一对恢复：

1. 继续保持 maintenance、停服和 dispatcher flock，保存一份 post-cutover 故障库及其 SHA-256，供审计，
   不得在故障库上直接“改回”字段。
2. 确认目标库没有 `-wal`、`-shm`、`-journal` 或 hot journal；若存在，先停止并查明仍在访问数据库的
   进程，禁止直接删除旁路文件。
3. 在目标库同目录创建冷备临时副本，核对其 SHA-256 等于已审核 `target_preimage_sha256`，执行
   `integrity_check=ok` 与 `foreign_key_check=0`，fsync 临时文件；再用同文件系统原子 rename 替换目标库，
   最后 fsync 目标父目录。保留原冷备不动。
4. 恢复与该冷备成对记录的旧代码 release；在仍停服状态下核对旧 active contract、所有 Bot current/
   version protocol、legacy rating pool、队列与赛事冻结三元组均与 preimage 一致，并再次执行完整性/FK
   检查。任何一项不一致都不得启动。
5. 只使用已保存且已复核 digest 的 cutover manifest，逐个确认恢复后的 DB 已不再引用本次新增 vN，
   再对 manifest 精确列出的 `bot_uploads/<bot_id>/vN/bot.bin` 和目录解除只读权限、unlink/rmdir，并 fsync
   各父目录；禁止通配符、递归 `rm`、删除整个 `bot_uploads`，也不得触碰旧 version 字节或其他代际资产。
6. 先以 maintenance 状态启动旧 release，验证 runtime contract、队列、评分与一场受控对局；验收完成后
   才恢复接单，自动排位仍需另行显式开启。保存故障库、冷备、manifest、命令输出和验收日志。

禁止只回滚代码、只恢复 DB、把 legacy version 原地重新激活，或执行反向在线 cutover；这些组合都会让
runtime 规则、冻结契约、Bot 协议和评分代际失配。

#### 现行切换：CCGC 2013 v1 → 固定五手二打（同协议）

现行目标契约为
`gomoku_ccgc_2013_five_move_two_v2 / gomoku_action_v2 / gomoku_ccgc_2013_five_move_two_rating_v2`；
来源是上一竞赛代
`gomoku_ccgc_2013_v1 / gomoku_action_v2 / gomoku_ccgc_2013_rating_v1`。两代 wire 都是
`gomoku_action_v2`，但新局的开局请求固定发送 `n_range=[2,2]`，响应 `n` 与黑 5 候选数只能为 2。
因此这不是协议二进制替换，必须使用专用离线入口
`python -m bzplat.backend.cli game-rule-cutover`，不得把同协议代际塞进上节 `game-contract-cutover`。

只能按以下顺序执行：

1. 请求 maintenance 并等待 `maintenance.ready=true`，确认 `accepting=0`、`auto_enabled=0`；随后停止
   API、dispatcher、scheduler 与上传 worker。数据库中的 dispatcher 必须为 `stopped`，全站
   starting/running/settling job 与 attempt、pending/running Match、Local AI lease 均为 0，Docker launch
   journal 为 `idle`。目标游戏还必须没有未结算旧规则 Match 或未结束的非 showcase 赛事。
2. 停服后制作不同 inode 的逐字节冷备，核对 `cmp`、SHA-256、`PRAGMA integrity_check=ok` 与
   `PRAGMA foreign_key_check` 零行；保留数据库邻接 `.execution-dispatcher.lock`。dry-run/apply 都要求
   `--db`、`--backup` 为绝对路径并先取得该 flock，确认缺失时不会打开 Store。
3. 执行 dry-run。CLI 只在目标库同目录创建临时 copy 并在结束时删除，目标 DB 零写；目标契约直接取
   当前代码 GameSpec，调用方只声明来源三元组。保存完整 JSON，审核 `from_contract/to_contract`、
   `bot_count/current_version_count/bot_snapshot_digest`、三项评分投影 digest、
   `queued_job_ids/retryable_interrupted_job_ids/cancelled_job_count`、`plan_digest`、
   `target_preimage_sha256`。rule-only 计划的 `version_manifest` 必须为空，`manifest_digest` 必须是空数组
   `[]` 的 canonical SHA-256；任何 current Bot/version 协议、镜像字段、canonical 路径、SHA/size、权限、
   inode 漂移均为 No-Go。
4. 本命令不会重新预检或替换 Bot。切换前须通知并复核仍返回 `n=3/4` 的旧构建：它们的版本可保留，
   但任何新局都会被现行裁判判为非法，不存在兼容回落。此次生产快照中曾固定三打/四打的排位 Bot
   `#1103`、`#1112` 必须单独确认是否已上传 fixed2 版本；仓库 C/Python 与 showcase 样例只返回
   `n=2` 和两个候选。执行日仍须从只读生产快照刷新该清单，不能只依赖这里的历史 ID。
5. apply 必须回填同一次 dry-run 的 `plan_digest`、空 `manifest_digest` 与
   `target_preimage_sha256`，并再次逐字确认 DB/冷备路径。首次 apply 只接受目标库仍等于审核 preimage；
   事务提交后输出丢失时，完全相同的 cutover id、来源三元组、digest 与原冷备可以重试，CLI 只在完整
   marker 边和资产/评分后置条件吻合时返回 `already_applied=true`。

示例（路径、部署 ID 与三个 digest 必须替换为本次 dry-run 的审核值）：

```bash
# dry-run：保存 JSON，审核 plan_digest/manifest_digest/target_preimage_sha256
python -m bzplat.backend.cli game-rule-cutover \
  --db /absolute/path/botzone.db \
  --cutover-id gomoku-five-move-two-v2-<deployment-id> \
  --game-id gomoku \
  --from-ruleset gomoku_ccgc_2013_v1 \
  --from-protocol gomoku_action_v2 \
  --from-rating-pool gomoku_ccgc_2013_rating_v1 \
  --backup /absolute/path/botzone.pre-five-move-two.db \
  --confirm-service-stopped --confirm-cold-backup

# apply：只接受上述同一审核计划与冷备 preimage
python -m bzplat.backend.cli game-rule-cutover \
  --db /absolute/path/botzone.db \
  --cutover-id gomoku-five-move-two-v2-<deployment-id> \
  --game-id gomoku \
  --from-ruleset gomoku_ccgc_2013_v1 \
  --from-protocol gomoku_action_v2 \
  --from-rating-pool gomoku_ccgc_2013_rating_v1 \
  --apply \
  --expect-plan-digest <reviewed-plan-digest> \
  --expect-manifest-digest <reviewed-empty-manifest-digest> \
  --expect-target-preimage-sha256 <reviewed-target-preimage-sha256> \
  --confirm-db /absolute/path/botzone.db \
  --backup /absolute/path/botzone.pre-five-move-two.db \
  --confirm-service-stopped --confirm-cold-backup
```

apply 在一个事务中按上一 pool 归档 `ratings/rating_history/pair_stats`，把现行评分重置为
1500/350/0.06、0 胜负平、0 场，清空该游戏自动排位服务历史，取消来源契约的 queued job 并关闭
interrupted job 的通用重试，再以 compare-and-swap 推进 active ruleset/pool 和写入空 manifest marker。
它不会新建、退役、改写或 pin 任何 Bot/version，也不会创建/删除 `bot_uploads` 文件；历史 Match、回放、
赛事与上一评分池归档保持原契约。

apply 后继续停服，至少复核：数据库完整性/FK；marker 链唯一且链尾等于现行三元组；空 manifest、
`bot_count=retired_count=0`；所有 current Bot/version ID、路径、checksum、size、runtime mode 与 preimage
一致；旧 pool 归档 digest/行数一致；新 pool 所有 Bot 为默认评分且 0 场；旧 queued/retryable job 已按
`ruleset_retired` 收敛；历史 `gomoku_ccgc_2013_v1` 三打/四打回放仍显示真实候选数。以 maintenance 状态
启动新代码后，使用 fixed2 样例各跑 Traditional/LongRunning 受控对局，核对开局 wire
`n_range=[2,2]`、响应 `n=2`、回放/HUD 和新评分池；再恢复接单，自动排位最后单独开启。

若 apply 前失败，修正后必须重跑 dry-run；若 apply 后回滚，保持停服和 flock，保存 post-cutover 故障库，
按上节同样的无 WAL/SHM/journal、完整性/FK、同文件系统原子替换与父目录 fsync 规则恢复原冷备及配对的
旧代码 release。rule-only 切换没有新增 vN 或资产目录，回滚时**不得删除或改写任何 Bot 文件**。只回滚
代码、只改 `rating_pool_state`、反向运行 cutover 或把归档评分手工灌回当前表均禁止。

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

### 闲时公平生产者

自动排位只是全局队列的一个 `source=auto` 后台生产者，不再拥有独立 dispatcher 或物理容量池。
唯一可变项 `execution_control.auto_enabled` 由 `PUT /api/admin/auto-match` 严格 boolean 修改，但开启只表示
**允许在闲时运行**，不表示立即生成或 claim。管理员单纯关闭开关只停止新的 auto 生成/claim，
已有自动局自然结束；manual/human/contest 完全不受开关影响。`BZ_QA_INSTANCE=1`
的代码能力门仍禁止开启自动生产。

闲时门禁与公平选择是两层不同职责。dispatcher 只有同时满足以下条件时才允许生成或 claim auto：

1. manual/human/contest 没有 queued/starting/running/settling 请求；非 showcase 真实赛事不处于 `running/rest`，
   且不处于已有待开 pairing、`starts_at` 进入未来 5 分钟保护窗的 `published`；远期或
   `starts_at=NULL` 的 published 不会无期阻塞 auto；
2. 全站两个 match slot 均为空，且上述前台空闲已经连续保持 **300 秒**；`next_eligible_at`
   持久到数据库。进入真实前台/赛事 guard 时仅把 `gate_reason=busy` 写入一次，持续繁忙不产生每秒写放大；
   guard 首次解除才开始完整 300 秒空闲窗，该边界跨 Store reopen/进程重启保持。有 auto 的恢复则推进 cooldown，
   不把每次重启一概当作重置；
3. 没有 active auto，距上一场 auto 收口也已经冷却 **300 秒**；
4. 评分投影、Docker launch journal、冻结版本及其他既有安全门禁全部正常；auto claim 后仍能按追加式
   资源 registry 的逐维最高值预留一整场前台资源。当前为 1 match slot + 2 sandbox units + 4000 毫核
   + 4096 MiB；未来追加更高档位会自动提高预留。主机 ceiling 收紧后无法同时容纳 auto 与该预留时，
   auto 保持等待。

满足门禁后，producer 至多保留 **1 条** auto 候选，dispatcher 至多运行 **1 场** auto，始终给随后到达的
前台请求保留至少 1 个 match slot。任一 manual/human/contest 前台请求成功入队/重试时（尤其人类对局入队），在同一 `BEGIN IMMEDIATE` 中取消 queued auto；
真实赛事 guard 出现后由 dispatcher 下一次 reconcile 事务取消/yield，而 auto claim 自身在事务内重查 guard，因此不会穿透开局。在途 auto 通过专用非评分终态
`auto_yield_foreground` 安全让路；只有对应 sandbox 完成精确清理后容量才释放，页面统一显示
“自动排位为前台任务让路”。auto 收口后重新进入 300 秒冷却，不能在持续空闲时无间隔连跑。

yield 与物理启动以 Docker create intent 事务确定唯一顺序。execution 所有者把 launch journal 从 `idle`
写为 `creating` 时，必须在同一个 `BEGIN IMMEDIATE` 内先证明 host-wide journal 已收敛，再复核 job 仍处于当前
`starting/running` attempt、attempt 序号一致且 `cancel_requested=0`。`settling` 仍占收尾容量，但不得再创建物理容器：

1. 前台入队/yield 先提交时，create intent 看到取消标记并以 `execution_attempt_not_current` 拒绝；事务回滚后
   journal 仍为 `idle`，物理 `docker create` 不得发生。Docker supervisor 必须保留这个确定性错误，BinaryRunner
   将其转换为 `asyncio.CancelledError` 语义的 benign task cancellation，随后按普通 attempt 取消路径清理；不得误报
   Docker 控制不确定，也不得把 dispatcher/队列置为 paused。
2. launch intent 先提交时，token、确定性名称、instance/job/attempt/slot 与 host boot 已先持久化；后到的 yield
   继续设置取消标记，但不能撤销或跳过 launch journal。runner 必须沿既有 token/name/label/journal exact cleanup
   收敛，确认物理容器与 intent 均清零后才释放资源并让前台 claim。

producer 与 claim 都必须在各自 `BEGIN IMMEDIATE` 内重新检查持久前台真相；外层 dispatcher 的空闲快照
只用于节流和展示，不能成为并发 enqueue 穿透门禁。`auto_match_fair_state` 追加可幂等迁移列
`dispatch_policy_version` / `next_eligible_at` / `gate_reason`，以持久化策略代际、最早可运行时间和门禁原因。首次
`idle-only-v1` 对账会取消遗留 queued auto、让在途 auto 以专用 `auto_idle_policy_cutover`
（“自动排位策略升级后收口”）收口，并从当时起重新计 300 秒空闲窗；
审计 decision/job 行保留终态真值，不会无边界消化旧预告。

公平选择由 SQLite 持久状态推进，不借用可被人工挑战影响的 `ratings.last_played_at` 或 `pair_stats`：

1. 游戏按固定游标轮转；`bootstrap/established` 两条内部服务 lane 持久交替。bootstrap 目标场数只帮助
   新 Bot 获得自动服务，与公开排名资格常量彼此独立。
2. 先按每游戏 auto 专属 owner 服务次数/最近轮次排序；候选只包含每个 owner/game 当前唯一
   `is_ranked=true` 排位代表，同一 owner 在自动活跃请求中最多占一席。其他 active Bot 仍可练习或参赛，
   但不会被 auto producer 选中。
3. 对手依次按 Bot pair、owner pair、Rating 距离、服务债务和稳定 ID；座位用 auto 专属计数平衡。
   双方必须同游戏、不同 Bot、不同 owner，same-owner 评分仍保持中性。
4. 决策永久记录策略版本、游标/lane、服务计数、pair 次数、Rating 差、座位债务、冻结版本、
   `job_public_id` 和每次 attempt 终态；通用 job 是生命周期真值，`auto_match_decisions` 是选择审计。

公开 `GET /api/execution-queue` 返回 dispatcher、双容量向量、脱敏 active/queued 与顶层 `auto_scheduler`：
其 `mode=idle_only`，并公开 `state/reason/idle_required_seconds/cooldown_seconds/max_active/queued_target`
及可选 `next_eligible_at`。`state` 为 `disabled/foreground_busy/contest_guard/cooldown/ready/yielding/running`，
`reason` 是稳定机器码，前端必须本地化而不得直出；这使“已启用但等待前台/赛事/冷却”、“正在安全收口”与“正在闲时运行”可以区分。POST 挑战/人机返回
HTTP 202 与 request public_id、真实前台 `ahead_jobs`、`ahead_sandbox_units`、容量及动态 ETA 区间；auto
永不计入前台顺位或 ETA。
`GET/DELETE /api/execution-requests/{public_id}` 用于查询/取消，interrupted 的人工/人机可 POST `retry`。
所有投影都由白名单构造，不返回内部 DB id、version id、二进制路径、checksum、token、match_config 或
Docker 诊断。ETA 明确是随对局时长、优先级和资源变化的区间，而非承诺时间。

### 旧库迁移与 schema 幂等边界

- 首次出现 `bots.is_ranked` 时，迁移只在同一 owner/game 的 active、现行协议且可执行 Bot 中选择一个代表，
  按公开资格、Rating、计分场次和稳定 Bot ID 确定性排序，再建立 `(owner_id,game_id) WHERE is_ranked=1`
  partial unique index。该回填只执行一次；后续 owner 显式退出留下的空席在重启时不得被静默补回。
  Bot universe 因新增 owner/代表维度从 v3 升为 v4，旧生产库迁移后必须停服执行下述 rating rebuild，
  不能由启动迁移伪造可信摘要。
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
- 当前 46 个现行 trigger 由 `_ensure_trigger` 规范化比较。定义相同的二次打开不执行 trigger
  `DROP/CREATE`，`schema_version` 不因这些 trigger 改变；缺失/过期定义修复一次，对象类型冲突、非法
  identifier 或创建后定义不符会抛错并由 Store 事务回滚。Store 的其他迁移仍可能执行 DML，所以整体
  只保证逻辑幂等，不保证数据库文件字节、SHA-256 或 mtime 不变。

### 排行榜重建与上线 No-Go

execution job 创建事务会冻结评分资格：只有不同所有者、且双方都是各自 owner/game 当前唯一排位代表的
Bot 挑战/ladder 计分；同 Bot、自有不同 Bot、任一未派遣 Bot、人机与赛事均为中性局。未派遣原因统一为
`ranked_bot_not_selected`。中性局完成后仍写 exactly-once settlement marker，但不改 ratings、历史、胜负或
pair_stats；对局详情同时返回创建时资格 `rated/rating_reason` 与唯一公开的 marker 布尔真值
`rating_settled`（内部 order/status 不出公共契约），两者
不可互相代替。符合资格的在途、完成未落 marker、完成已落 marker、中止对局分别显示“预计计分”、
“待结算”、“已计分”、“已中止未计分”。历史结算首次迁移按
`(COALESCE(ended_at, settled_at), match_id)` 固化为连续 `settled_order=1..N`，以后完成事务先冻结
单调序号，恢复和离线重放只认该序号，绝不能再按 `created_at` 猜顺序。

旧库不会在启动迁移时冒险自动重放。`rating_projection_state` 未经当前策略验证或落后于 settlement
水位时，自动排位一律暂停。生产升级前必须在停服维护窗完成以下流程，否则是发布 **No-Go**：

1. 在线请求 deployment maintenance，等待 `ready=true`，再停止 API、dispatcher、scheduler 与 worker；确认
   监听端口、生产 namespace 容器、上传、Local AI lease、运行对局和 SQLite `-wal/-shm/-journal` sidecar
   全部为零。
2. 在拉取/运行新代码前，先为旧 schema 主库制作一份逐字节相同的**迁移前冷备**，记录 release SHA、数据库
   SHA-256、inode、`integrity_check=ok` 与空 `foreign_key_check`。旧 release 的回滚只能与这份迁移前冷备
   成对执行，禁止让旧代码写迁移后的数据库。
3. 拉取并构建目标 release，但仍不启动 50380。`rating-rebuild` 是只读评分命令，**不会执行 Store schema
   migration**；必须先用新代码在停服状态离线打开并立即关闭目标库，使 `is_ranked`、canonical partial unique
   index 和首次确定性派遣在同一 Store migration 事务中落地：

   ```bash
   python - <<'PY'
   from pathlib import Path
   from bzplat.backend.store import Store

   db = Path("/absolute/path/botzone.db").resolve(strict=True)
   Store(str(db)).close()
   PY
   ```

4. 仍保持停服，只读核验 `bots.is_ranked`、`idx_bots_one_ranked_per_owner_game` 的 canonical partial unique
   定义、每个 `(owner_id,game_id)` 最多一行 `is_ranked=1`、首次 backfill 计划以及 integrity/FK。随后从此
   **迁移后、重建前**主库再制作第二份逐字节相同的冷备；下述 `rating-rebuild --apply` 的 `--backup`
   必须指向这第二份冷备，不能误用缺少 `is_ranked` 的迁移前冷备。
5. 仅在上述 schema 与双冷备门禁全部通过后，按下方命令执行 dry-run，审核三项摘要与全榜 diff，再 apply
   和独立 verify。启动服务后仍保持 maintenance，先完成 schema/v4 投影、排行榜、RBAC 与受控代表
   `PUT → DELETE → PUT` 验收；此时 `accepting=0` 且 `auto_enabled=0`，不得声称已验收新挑战或自动候选。
   上述只读/控制面检查通过后才解除 maintenance，并断言 `accepting=1`、`auto_enabled=0`；在自动排位仍
   关闭时依次完成“当前代表对当前代表”的计分 canary 与“非代表对当前代表”的中性练习 canary，核对
   settlement、Rating/RD/history/pair_stats。最后才显式恢复自动排位，并观察一个新 auto job 的双方均为
   当前代表、不同 owner 且冻结为 `rated=1/rating_reason=eligible`。

若离线迁移或评分重建验收失败，保持服务停止：保存故障库后，用旧 release + 迁移前冷备成对回滚；若仅
评分 apply 失败且事务已完整回滚，可继续使用新 release + 已验证的迁移后冷备重新审计。服务一旦在新 schema
上恢复写入，恢复任一冷备都会丢失其后的合法业务写，必须重新进入维护并单独评估，不能自动回退。

在线事务还要求 `mutation_revision == trusted_mutation_revision`。评分/Bot universe/source 输入的每次
DML 都由数据库递增前者；只有写前完整可信的显式 mutation guard 才能在同一事务同步后者和全部
摘要。completed 后合法、连续的未结算尾部可跨重启继续，但任何 stale 状态都不能被后续
ensure/评分/中性 marker/可见性写“洗白”；通用硬删、换 `game_id` 与无 marker 的低层评分写必须走
下述离线 rebuild 才能恢复自动排位。排位代表进入 Bot universe 后，认证从 `owner-ranked-bot-v4` 起生效；
升级前遗留的 `owner-neutral-v3` 及更早标记
没有可信 mutation lineage，即使摘要吻合也必须先离线重建。

```bash
# 前置条件：新代码已离线完成 Store migration，且第二份冷备与当前目标逐字节相同。
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
线上榜可见性消费的 `id/owner_id/game_id/is_active/is_ranked/format/os/arch`，任何 owner、active、排位代表
或二进制 metadata 漂移都使已审核 plan 失效。rated source 还要求 `rated ⇔ rating_reason=eligible`，
`deltas` 必须恰为两个非 bool 整数且零和。
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
全局执行容量/前台 aging/用户上限、自动排位 300 秒空闲与冷却门禁、单场单候选上限、bootstrap 目标、
独立公开排名资格与赛事 scheduler 参数。
阶段休息时间直接属于各代码模板。修改须走代码评审、测试、
部署；旧 `platform_settings` 同名记录只作为历史数据保留，启动不 seed、不读取、不回写。

`GET /api/admin/settings/runtime` 仅供诊断，响应明确包含 `source="code"`、
`mutable=false`、当前机器 ceiling、实际生效并发、队列计数和冻结配置。不存在
`PATCH /api/admin/settings/runtime`，管理端也不展示“运行时”Tab。

赛制模板同样由 `games/<game>/templates.py` 通过游戏注册表聚合。公开
`GET /api/contests/templates` 返回 `source="code"`、`mutable=false`；历史
`contest_templates` 表不再 seed/对账或参与解析，不存在 `/api/admin/templates*`。
