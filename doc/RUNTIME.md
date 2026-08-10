# 运行时与资源限制

本文说明本平台 Bot 沙箱的 Docker 资源、决策超时、严格通信模式与并发半负载公式。

## Docker 硬限制

> **原则：Docker 是基础，平台不在 Docker 里做定制。** 平台只使用公共镜像，
> 不构建任何自定义 Dockerfile；所有沙箱策略都通过 `docker run` 参数施加，
> 因此零镜像维护成本、可随时替换基础镜像。

- 平台只接受 Linux x86_64 ELF Bot，公共镜像为 `debian:bookworm-slim`（可用
  `BZ_LINUX_BOT_IMAGE` 覆盖）；PE、Mach-O、ARM64 ELF 与脚本在上传校验阶段拒绝。
- 首次启动 Bot 前，平台在独立的平台准备阶段检查镜像；缺失时只拉取
  `linux/amd64`，并在完成后再次核对 OS/架构。镜像检查/拉取不计入上传的 8 秒
  首回合健康检查，也不计入 Pencil 的 900 秒累计棋钟。registry、daemon、拉取超时
  或架构不符统一归为平台故障，不判 Bot 超时或技术负。
- 测试无 Docker 时：仅兼容的 Linux x86_64 主机可用 `BZ_BOT_LOCAL=1` 直接本机跑
  ELF（降级，不施加容器限制；不得用于生产）。

LongRunning 对局会同时保留双方各一个容器；Traditional 则在每个决策点启动当前行动方的容器并在响应后销毁。两种路径都使用以下 `docker run` 加固参数（Linux 路径）：

| 参数 | 作用 |
|------|------|
| `--cpus=1` | 单核上限（硬编码，admin 不可抬高） |
| `--memory=512m` | 内存上限（硬编码） |
| `--network=none` | 完全断网 |
| `--read-only` | 根文件系统只读 |
| `--tmpfs /tmp:rw,exec,nosuid,size=64m` | /tmp 可写且可执行（PyInstaller 自解压 ELF / ld.so 延迟绑定需 /tmp 可执行映射；根 fs 仍只读） |
| `--cap-drop=ALL` | 丢弃全部 Linux capabilities |
| `--security-opt no-new-privileges` | 禁止提权 |
| `--user 65534:65534` | 以 nobody 身份运行 |
| `--pull=never` | Bot 计时窗口内禁止 `docker run` 隐式拉取镜像 |
| `--entrypoint /app/bot` | 忽略基础镜像自带 Entrypoint/CMD，直接执行已校验 ELF |
| `--rm` | 退出即销毁容器 |

所有参数均为**只读硬限制**，admin 面板不可抬高 CPU/内存。

## 决策超时

- `GameSpec.time_budget_per_side=None` 的游戏（当前 holdem / gomoku）使用代码常量 **60 秒 / 决策**；管理端、数据库和环境变量均不能覆盖。
- Pencil 的 `GameSpec.time_budget_per_side=900`：双方各有一只独立、固定 **900 秒（15 分钟）累计棋钟**，Bot-vs-Bot 与人类对战走同一契约；每次等待只使用该座位的剩余时间，不能靠多回合重置。该固定规则不读取 `action_timeout_sec`，admin 不可改。
- Bot 单步超时或 Pencil 累计棋钟耗尽在第一次发生时即终止对局，持久化为 `completed + reason=timeout + technical_loss=1`；不会生成代替动作继续对局。Bot-vs-Bot 技术结果进入评分/赛事积分，人机局由人类获胜但不计 Glicko。人类侧逐回合/累计超时仍走人类 inactivity 与游戏裁判逻辑。
- 人类对战的 `human_action_timeout` 默认仍为 **120 秒 / 回合**，用于等待 WebSocket 落子的内层保护；Pencil 同时受外层 900 秒累计棋钟约束，以先到的限制为准。
- 棋钟成功决策写入 `time_used {seat,used,remaining,budget}`，耗尽写入 `time_out {seat,used,budget}`；事件进入回放/SSE，点格棋对局页据此展示双方剩余时间和「超时」标记。
- **故障语义**（详见 [对局](#/wiki?slug=guide)）：Bot 信封/response 格式错误 → `completed + reason=protocol_error + technical_loss=1`；Bot 决策超时 → `completed + reason=timeout + technical_loss=1`。两者在首个故障终止，回放写 `technical_incident`，结果只公开 `technical_incident_count`、`technical_incidents_by_seat` 与最多 3 条 `technical_incident_samples`；结构化日志带 `match_id/bot_id/version_id/runtime/seat/turn` 且不记录原始 stdout/私有路径。历史回放中的旧错误事件只在服务端读取时归一化，不作为新写入或对外字段。Bot-vs-Bot 评分，人机局不评分；格式正确但游戏内非法动作仍归裁判。中途崩溃由引擎计分判负；Bot-vs-Bot 启动失败结算为 `completed + technical_loss`，human 启动失败为 `aborted + bot_crashed`。Docker 125 等平台沙箱故障为 `aborted + platform_error`、不评分；上传在 worker 中按所选 runtime_mode 使用正式首回合同一信封与握手预检，平台故障返回 503，不改变原激活版本，也不阻塞主事件循环。
- **中止公开边界**：中止对局的 replay/SSE/WS 终局只发送 `{"type":"error","reason":"稳定原因码"}`；不发送 `message`、异常文本或路径。未知/历史自由文本统一投影为 `platform_error`，管理员中止固定为 `admin_aborted`，详细诊断只写结构化日志。pending/running 的 `reason` 为空，页面不会在对局仍运行时提前显示“正常结束”。
- **完成公开边界**：完成对局的 replay/SSE/WS 终局只发送 `match_end {winner,reason,deltas}`；`reason` 只能取 `schema.PUBLIC_MATCH_COMPLETED_REASONS`，未知英文/中文自由文本统一为 `completed`。公开详情中的 `result` 只保留进度、净结果、复式 leg 与脱敏技术故障摘要，执行用 `match_config` 和其他诊断字段不对外返回。
- **事件公开边界**：非终态 replay/live 也只允许逐事件声明的字段；未知事件类型整条丢弃，已知事件的额外诊断字段丢弃。活跃真人德扑的公开观赛隐藏双方底牌与 `your_turn.request`，本人鉴权 WebSocket 只获得自己座位的底牌和请求；结束后才提供完整回放。SSE/WS 快照与可见性元数据全部构造成功后才注册队列；故障不留孤儿订阅，元数据缺失时默认按最严格可见性投影。
- 本平台默认 Traditional（每个决策点重启进程）；显式选择 LongRunning 并完成精确握手后才整场长驻。两种模式使用相同 stdin/stdout 单行 JSON 信封；缺失/错误握手立即协议判负，不回退。

平台不按编程语言调整时限。无累计棋钟的游戏统一使用
`runtime/config.py::ACTION_TIMEOUT_SEC`；Pencil 使用 GameSpec 固定的每方 900 秒累计预算。

## 并发半负载

容量按最保守的 LongRunning 情况估算：每场对局最多同时保留 **2 个 Bot 容器 × 1 核**。Traditional 实际并发容器数通常更低，但不据此抬高平台硬上限。

```
cpu_count = os.cpu_count()          # 真实核数，禁止伪造
full      = max(1, cpu_count // 2)  # 满载对局数
ceiling   = max(1, full // 2)       # = max(1, cpu_count // 4)
configured = 2                       # runtime/config.py 代码常量
effective  = min(configured, ceiling)
```

- 生效并发固定取代码值 2 与机器 ceiling 的较小值，不读取旧
  `platform_settings.max_concurrent_matches`，也不存在管理写接口或环境变量覆盖。
- 为何一场占两核：双方各一容器且 `--cpus=1`。
- 全局 admission 会把已经接纳但尚未真正运行的赛事/挑战也计入占位；auto-match 只能使用
  `available_bot_slots()` 的剩余量再扣用户预留槽，不能只看当前容器数继续堆积任务。

## 运行模式边界

| 模式 | 进程 | 请求 |
|------|------|------|
| Traditional | 每个决策点启动并停止 | 每次完整 `requests[]/responses[]` |
| LongRunning | 整场一个进程 | 首回合完整历史；精确握手后为单 `request` |

上传预检与正式首回合使用同一模式、同一信封、同一 response 校验。LongRunning 未在
握手时间窗内输出固定字符串即技术负，runner 不切换模式。详见[协议规范](#/wiki?slug=protocol)。

## 德州牌型参考

![德州扑克牌型](/wiki-assets/TexasHoldemHandType.jpg)

## 持续自动排位（维护天梯榜）

`AutoMatchScheduler` 随服务启动，持续维护固定 6 场的持久预告队列；不再有每日场次上限、
空闲等待、Bot 冷却、陈旧阈值或“每轮最多几场”。自动排位本身**全局串行**，任何时刻最多
一场 `dispatched`；挑战和赛事仍共用原有全局 admission，自动排位原子占位时必须再留下
1 个前台槽。前台容量不足时队列原位等待，不丢配对，也不创建未启动的垃圾对局。

唯一可变运行项是管理员总开关：独立单例表 `auto_match_control` 首次升级默认开启，
`PUT /api/admin/auto-match` 只接受严格 boolean 并写审计日志。关闭后不再补队或派发，当前局
自然完成、预告队列保留；再次开启立即续跑。`BZ_QA_INSTANCE=1` 另有不可绕过的代码能力门，
即使复制的生产库开关为开也不能调度，管理员尝试开启返回 409。旧 `platform_settings` 的
auto-match 键及 `auto_match_daily_claims` 在迁移中幂等删除，不存在第二套开关或额度真值。

公平选择由 SQLite 持久状态推进，不依赖可被前台挑战影响的 `ratings.last_played_at` 或
`pair_stats`：

1. 游戏按固定游标轮转，只跳过当前没有合法配对的游戏；不同游戏不比较原始场数。
2. 定级/正式通道持久交替；只有一个定级所有者时才允许匹配正式 Bot，并记录 fallback。
3. 先按每游戏 auto 专属的所有者服务次数/最近服务轮次排序；同一所有者在全局活跃队列最多
   占一席，拥有多个 Bot 不会获得多倍份额；所有者内部再轮转服务最少的 Bot。
4. 对手依次按 Bot 对次数、所有者对次数、Rating 距离、服务债务和稳定 ID 决定；座位使用
   auto 专属先后手计数最小化双方债务。双方必须同游戏、不同 Bot、不同所有者。
5. 队列冻结双方当前版本；派发事务再次验证用户/Bot 活跃、Linux x86_64 ELF、版本归属与
   “没有另一场计分生命周期”。版本在 queued/dispatched 期间受外键 RESTRICT 保护。

队列的 `queued → dispatched → completed+settled/aborted` 全生命周期由 `BEGIN IMMEDIATE`、
CAS 和部分唯一索引守护；claim 与 match/index/replay/评分资格同事务。启动前失败会精确删除
未启动对象并恢复原队列位置；平台故障不评分、不计服务，并进入持久指数退避，Bot 协议错误、
超时或崩溃仍是合法技术负并计分。dispatcher 使用持久 lease，活跃进程不会被另一实例误恢复。
每次选择的策略版本、游标、通道、服务计数、配对次数、Rating 差、座位债务、冻结版本和终态
永久写入 `auto_match_decisions`，队列终态删除也不丢公平证据。

定级阈值只有代码常量 `AUTO_MATCH_PLACEMENT_REQUIRED=10`，同时驱动 `/api/tiers`、排行榜、
Bot profile 和队列通道；它不是管理员参数。公开 `GET /api/auto-match/queue?game_id=` 返回脱敏的
正在进行/即将进行、全局位置和暂停原因；排行榜按当前游戏展示，管理首页展示全局队列与唯一开关。

### 排行榜重建与上线 No-Go

挑战创建事务会冻结评分资格：不同所有者 Bot 挑战/ladder 计分；同 Bot、自有不同 Bot、人机与
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
全榜 diff 复用线上榜的
active Linux/amd64 ELF eligibility、10 场正式/定级分段、`rating → matches_played → bot_id` 排序和
per-game tier 曲线，未定级 Bot 的正式 rank 始终为空。apply 除绝对路径二次确认和停服声明外，要求冷备
与目标双方 `integrity_check=ok`、`foreign_key_check=0`，并在首个 DML 前同时核对三项已审核 digest、
完整业务 digest 和数据库文件 digest；旧业务备份即使评分源相同也不能通过。上述检查在
`BEGIN EXCLUSIVE` 内对目标再次执行，并复核无 running match、无活跃 dispatcher lease，且
`auto_match_queue` 必须为 0 行；queued/dispatched 任一旧评分代际条目尚在都是发布 No-Go。故障整事务
回滚。语义投影与验证水位都已一致时，二次 apply 直接 rollback，保持数据库字节、mtime 与 rebuilt_at
不变，是真正 zero-write no-op。它只重建 `ratings`、每 Bot 最近 200 条 `rating_history`、`pair_stats`
和 projection state，不删除、不重排 `match_rating_policies` 或 settlements；已删除 Bot 仍在内存中参与
Glicko 传播，但不会写回带 FK 的投影表。直接命中的污染 Bot 不是完整影响范围，是否可上线必须以
全榜重建 hash、Rating 与名次 diff 为准。

> **可见性**：自动 ladder 对局会出现在首页最新对局和排行榜队列，可直接进入观赛。

## 代码配置与只读诊断

`bzplat/backend/runtime/config.py` 是运行参数的唯一真相源，集中声明决策超时、默认并发、
自动排位定级阈值与赛事 scheduler 参数。阶段休息时间直接属于各代码模板。修改须走代码评审、测试、
部署；旧 `platform_settings` 同名记录只作为历史数据保留，启动不 seed、不读取、不回写。

`GET /api/admin/settings/runtime` 仅供诊断，响应明确包含 `source="code"`、
`mutable=false`、当前机器 ceiling、实际生效并发、队列计数和冻结配置。不存在
`PATCH /api/admin/settings/runtime`，管理端也不展示“运行时”Tab。

赛制模板同样由 `games/<game>/templates.py` 通过游戏注册表聚合。公开
`GET /api/contests/templates` 返回 `source="code"`、`mutable=false`；历史
`contest_templates` 表不再 seed/对账或参与解析，不存在 `/api/admin/templates*`。
