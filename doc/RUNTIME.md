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

- `GameSpec.time_budget_per_side=None` 的游戏（当前 holdem / gomoku）沿用单次决策超时：默认 **60 秒 / 决策**，管理员可在「运行时」面板改为 1–300 秒。
- Pencil 的 `GameSpec.time_budget_per_side=900`：双方各有一只独立、固定 **900 秒（15 分钟）累计棋钟**，Bot-vs-Bot 与人类对战走同一契约；每次等待只使用该座位的剩余时间，不能靠多回合重置。该固定规则不读取 `action_timeout_sec`，admin 不可改。
- Bot 单步超时或 Pencil 累计棋钟耗尽在第一次发生时即终止对局，持久化为 `completed + reason=timeout + technical_loss=1`；不会生成代替动作继续对局。Bot-vs-Bot 技术结果进入评分/赛事积分，人机局由人类获胜但不计 Glicko。人类侧逐回合/累计超时仍走人类 inactivity 与游戏裁判逻辑。
- 人类对战的 `human_action_timeout` 默认仍为 **120 秒 / 回合**，用于等待 WebSocket 落子的内层保护；Pencil 同时受外层 900 秒累计棋钟约束，以先到的限制为准。
- 棋钟成功决策写入 `time_used {seat,used,remaining,budget}`，耗尽写入 `time_out {seat,used,budget}`；事件进入回放/SSE，点格棋对局页据此展示双方剩余时间和「超时」标记。
- **故障语义**（详见 [对局](#/wiki?slug=guide)）：Bot 信封/response 格式错误 → `completed + reason=protocol_error + technical_loss=1`；Bot 决策超时 → `completed + reason=timeout + technical_loss=1`。两者在首个故障终止，回放写 `technical_incident`，结果只公开 `technical_incident_count`、`technical_incidents_by_seat` 与最多 3 条 `technical_incident_samples`；结构化日志带 `match_id/bot_id/version_id/runtime/seat/turn` 且不记录原始 stdout/私有路径。历史回放中的旧错误事件只在服务端读取时归一化，不作为新写入或对外字段。Bot-vs-Bot 评分，人机局不评分；格式正确但游戏内非法动作仍归裁判。中途崩溃由引擎计分判负；Bot-vs-Bot 启动失败结算为 `completed + technical_loss`，human 启动失败为 `aborted + bot_crashed`。Docker 125 等平台沙箱故障为 `aborted + platform_error`、不评分；上传在 worker 中按所选 runtime_mode 使用正式首回合同一信封与握手预检，平台故障返回 503，不改变原激活版本，也不阻塞主事件循环。
- 本平台默认 Traditional（每个决策点重启进程）；显式选择 LongRunning 并完成精确握手后才整场长驻。两种模式使用相同 stdin/stdout 单行 JSON 信封；缺失/错误握手立即协议判负，不回退。

平台不按编程语言调整时限。无累计棋钟的游戏统一使用管理员配置的
`action_timeout_sec`；Pencil 使用 GameSpec 固定的每方 900 秒累计预算。

## 并发半负载

容量按最保守的 LongRunning 情况估算：每场对局最多同时保留 **2 个 Bot 容器 × 1 核**。Traditional 实际并发容器数通常更低，但不据此抬高平台硬上限。

```
cpu_count = os.cpu_count()          # 真实核数，禁止伪造
full      = max(1, cpu_count // 2)  # 满载对局数
ceiling   = max(1, full // 2)       # = max(1, cpu_count // 4)
effective = min(admin_requested, ceiling)
```

- Admin 设置的 `max_concurrent_matches` **不得超过 ceiling**；超过则 API 返回 **400**。
- 为何一场占两核：双方各一容器且 `--cpus=1`。

## 运行模式边界

| 模式 | 进程 | 请求 |
|------|------|------|
| Traditional | 每个决策点启动并停止 | 每次完整 `requests[]/responses[]` |
| LongRunning | 整场一个进程 | 首回合完整历史；精确握手后为单 `request` |

上传预检与正式首回合使用同一模式、同一信封、同一 response 校验。LongRunning 未在
握手时间窗内输出固定字符串即技术负，runner 不切换模式。详见[协议规范](#/wiki?slug=protocol)。

## 德州牌型参考

![德州扑克牌型](/wiki-assets/TexasHoldemHandType.jpg)

## 闲时自动对局（维护天梯榜）

平台在**系统空闲**时自动安排 bot 对战，使 Glicko-2 排行榜保持新鲜。
单进程单事件循环后台任务（`bzplat/backend/matches/auto_matcher.py`），随服务启动即挂载。

**触发条件**（全部满足才安排）：

1. `auto_match_enabled = 1`（默认开，admin 可关）；
2. 有空闲并发槽：`max_concurrent - reserve_slots - 当前运行数 > 0`；
   `reserve_slots`（默认 1）为用户主动挑战**预留**的槽位，避免抢占；
3. 连续空闲达 `auto_match_min_idle_sec`（默认 5 秒），即真正闲时。

**配对策略**：陈旧度优先（`last_played_at` 最旧 / 从未赛）+ rating 就近（Swiss 式）。
**新 bot 定级优先**：`matches_played < auto_match_placement_games`（默认 10）的「定级期」bot 排最前，
且用更短 cooldown（cooldown÷10，最少 30s）加快定级；打满后回归陈旧度调度。
**节流**：同一 bot 两场间隔不低于 `auto_match_bot_cooldown`（默认 600 秒）；
近期已配对组合短期不再重复。**每轮**最多补 `auto_match_max_per_round`（默认 2）场；
**每日**总量上限 `auto_match_daily_cap`（默认 200，0=不限，达上限当日停）。
`match_type=ladder`，`owner` 为空（系统发起），**计入全局 Glicko-2 评分**
（比赛 contest 对局不计全局，见 [对局](#/wiki?slug=guide)）。

| 配置项 | 默认 | 含义 |
|--------|------|------|
| `auto_match_enabled` | 1 | 启用 |
| `auto_match_interval_sec` | 30 | 轮询间隔 |
| `auto_match_min_idle_sec` | 5 | 连续空闲触发秒数 |
| `auto_match_bot_cooldown` | 600 | 同 bot 两场间隔下限（秒） |
| `auto_match_stale_sec` | 3600 | 仅调度陈旧超此阈值（秒）的 bot；0=不限 |
| `auto_match_reserve_slots` | 1 | 为用户挑战预留的并发槽 |
| `auto_match_placement_games` | 10 | 新 bot 定级赛场次（前 N 场优先，0=禁用） |
| `auto_match_max_per_round` | 2 | 每轮最多补几场 |
| `auto_match_daily_cap` | 200 | 每日后台对局总量上限（0=不限） |

配置写入即**热更新**（调度器每轮重读 settings），无需重启。admin「运行时」Tab 可见
「今日后台对局 N/上限」实时计数。

> **可见性**：后台 ladder 对局会出现在首页「最新对局」（带「后台」徽章），便于观察天梯维护。

## 管理员配置

`GET/PATCH /api/admin/settings/runtime`：

- 可改：`action_timeout_sec`、`max_concurrent_matches`（≤ ceiling）、`contest_default_rest_minutes`、
  上述全部 `auto_match_*`
- 只读：`bot_cpus=1`、`bot_memory_mb=512`
- 原子更新：多字段 PATCH 先校验整包，随后在一个 SQLite 事务内写入；任一字段非法或写入失败都不保留部分新值
- 热更新：数据库事务提交后才重建 Semaphore / 单步决策超时；自动对局参数由调度器每轮读取。Pencil 900 秒棋钟是固定游戏规则，不受该设置影响
