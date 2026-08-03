# 运行时与资源限制

本文说明本平台 Bot 沙箱的 Docker 资源、决策超时、并发半负载公式，以及与 [Botzone](https://wiki.botzone.org.cn/index.php?title=Bot) 的差异。

## Docker 硬限制

> **原则：Docker 是基础，平台不在 Docker 里做定制。** 平台只使用公共镜像，
> 不构建任何自定义 Dockerfile；所有沙箱策略都通过 `docker run` 参数施加，
> 因此零镜像维护成本、可随时替换基础镜像。

- Linux ELF Bot：公共镜像 `debian:bookworm-slim`（可用 `BZ_LINUX_BOT_IMAGE` 覆盖）。
- Windows PE Bot：公共镜像 `scottyhardy/docker-wine:stable`（`BZ_WINE_BOT_IMAGE`）。
- 测试无 Docker 时：`BZ_BOT_LOCAL=1` 直接本机跑 ELF（降级，不施加容器限制）。

每场对局起 2 个容器（双方各一），完整 `docker run` 加固参数（Linux 路径）：

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
| `--rm` | 退出即销毁容器 |

Windows PE 走 Wine 容器时 CPU/内存/断网/cap-drop 对齐，但保留写权限（Wine 需要）。
所有参数均为**只读硬限制**，admin 面板不可抬高 CPU/内存。

## 决策超时

- 默认 **60 秒 / 决策**（管理员可在「运行时」面板改，范围 1–300）。
- 决策超时视为该 Bot **fold / 判负**（扑克弃牌；棋类判负）。进程崩溃 / EOF 则整场对局 **aborted**（`bot_crashed`），见 [对局](#/wiki?slug=match)。
- 本平台采用**整场对局长驻**进程（stdin/stdout 行协议），因此超时默认远高于 Botzone 的 1s/回合。

### Botzone 语言时限倍率（对照）

| 语言 | 倍率 |
|------|------|
| C/C++ | ×1 |
| JavaScript | ×2 |
| Java | ×3 |
| C# / Python | ×6 |
| Pascal | ×1 |

本平台**不按语言倍率**调整，统一使用管理员配置的 `action_timeout_sec`。首回合亦无 ×2。

## 并发半负载

每场对局 = **2 个 bot 容器 × 1 核**。

```
cpu_count = os.cpu_count()          # 真实核数，禁止伪造
full      = max(1, cpu_count // 2)  # 满载对局数
ceiling   = max(1, full // 2)       # = max(1, cpu_count // 4)
effective = min(admin_requested, ceiling)
```

- Admin 设置的 `max_concurrent_matches` **不得超过 ceiling**；超过则 API 返回 **400**。
- 为何一场占两核：双方各一容器且 `--cpus=1`。

## 与 Botzone 差异

| 项 | Botzone | 本平台 |
|----|---------|--------|
| CPU | 评测机单核 | Docker `--cpus=1` |
| 内存 | 默认 256MB | **512MB** |
| 决策时限 | 默认 1s/回合（首回合×2） | 默认 **60s**（可配） |
| 进程模型 | 传统每回合启停，或可选长时运行 | **整场长驻**行协议 |

### 运行模式示意

Botzone「传统」模式（每回合启停）：

![Botzone 传统运行模式](/wiki-assets/BotRunMode_Traditional.png)

Botzone「长时运行」模式：

![Botzone 长时运行模式](/wiki-assets/BotRunMode_LongRunning.png)

本平台对齐长驻进程思路，但协议为紧凑行 JSON，详见[协议规范](#/wiki?slug=protocol)。

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
（比赛 contest 对局不计全局，见 [对局](#/wiki?slug=match)）。

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
- 热更新：并发上限（重建 Semaphore）、决策超时、自动对局参数均为运行时生效
