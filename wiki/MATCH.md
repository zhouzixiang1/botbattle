# 对局

对齐 [Botzone · 对局](https://wiki.botzone.org.cn/index.php?title=%E5%AF%B9%E5%B1%80) 概念，并说明本站流程。

## Botzone 概念摘要

- 对局：Bot / 人类进行的一局游戏；可建桌、占位、开打、观战、回放。
- 常见错误：TLE（超时）、MLE（内存）、OLE（输出过长）、NJ（非法）、RE（运行时错误）。
- 游戏桌：开打前占位与聊天；天梯按 Elo 匹配。

## 本平台流程

1. **上传 Bot**（选择 `game_id`：holdem / gomoku / pencil）。
2. **挑战**：双方同游戏 Bot → 创建 `match` → MatchRunner 启两进程 → 引擎跑完。
3. **赛事**：按[赛制模板](#/wiki?slug=contest-format)生成 pairing，同样走 challenge 通道，带 `contest_id`；每场对局参数由模板的 `match_config`（holdem→hands、pencil→n_dots）决定。
4. **闲时自动对局**：系统空闲时自动安排 bot 对战维护天梯（`match_type=ladder`，见下）。
5. **观赛**：SSE `/api/matches/:id/events`；回放读 `match_replays.events_json`。
6. **评分**：Glicko-2；排行榜可按游戏过滤。

## 对局类型（match_type）

| 类型 | 来源 | 是否更新全局 Glicko-2 |
|------|------|----------------------|
| `challenge` | 用户主动挑战 | ✅ 是 |
| `table` | 游戏桌 | ✅ 是 |
| `ladder` | 系统闲时自动对局 | ✅ 是 |
| `contest` | 赛事内对局 | ❌ 否（仅计入赛事内 stage 积分） |

> **评分隔离**：比赛（contest）对局只计入赛事内 `contest_stage_results` 积分榜，
> **不**更新全局 Glicko-2 排行榜；其余三类对局均更新全局评分。

## 状态

`pending` → `running` → `completed` | `aborted`

- `pending`：已创建 match 行，尚未获取并发信号量。
- `running`：已获信号量、引擎正在跑（DB `status='running'`，对应 admin 面板「进行中」计数）。
- `completed`：引擎跑完，已写 `winner / earnings / replay`。
- `aborted`：异常（容器 OOM、双方崩溃等）。

SSE 观赛：先推送 `snapshot`（含当前事件历史，迟到者可补看），之后逐事件广播；
空闲时 25 秒发一次 `ping` 保活；`match_end` / `error` 后流结束。回放事件流增量落盘
（`settle` / `hand_start` / `match_end` / `move` / `match_start` 或每 5 个事件）。

## 错误与后果

| 情况 | holdem | gomoku / pencil |
|------|--------|-----------------|
| 决策超时 / 崩溃 | 视为 fold | 判负 |
| 非法动作 | fold | 判负 |
| 容器 OOM 等 | 对局 abort 或该方失败 | 同左 |

资源限制详见 [运行时](#/wiki?slug=runtime)。
