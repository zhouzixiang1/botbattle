# 对局

对齐 [Botzone · 对局](https://wiki.botzone.org.cn/index.php?title=%E5%AF%B9%E5%B1%80) 概念，并说明本站流程。

## Botzone 概念摘要

- 对局：Bot / 人类进行的一局游戏；可建桌、占位、开打、观战、回放。
- 常见错误：TLE（超时）、MLE（内存）、OLE（输出过长）、NJ（非法）、RE（运行时错误）。
- 游戏桌：开打前占位与聊天；天梯按 Elo 匹配。

## 本平台流程

1. **上传 Bot**（选择 `game_id`：holdem / gomoku / pencil）。
2. **挑战**：选你的 Bot + 对手方式（见下）→ 创建 `match` → 引擎跑完。
   - **搜索用户**：`/api/users?q=` 搜用户 → 选其该游戏 public Bot。
   - **自博弈**：选你自己的另一只同游戏 Bot（同一 owner 两不同 Bot 对战）。
   - **人类亲自上场**：你作为人类玩家对战 Bot（见「人类对战」）。
3. **赛事**：按[赛制模板](#/wiki?slug=contest-format)生成 pairing，同样走 challenge 通道，带 `contest_id`；每场对局参数由模板的 `match_config`（holdem→hands、pencil→n_dots）决定。
4. **闲时自动对局**：系统空闲时自动安排 bot 对战维护天梯（`match_type=ladder`，见下）。
5. **观赛**：SSE `/api/matches/:id/events`；回放读 `match_replays.events_json`。
6. **评分**：Glicko-2；排行榜可按游戏过滤。

## 对局类型（match_type）

| 类型 | 来源 | 是否更新全局 Glicko-2 |
|------|------|----------------------|
| `challenge` | 用户主动挑战（含自博弈） | ✅ 是 |
| `table` | 游戏桌 | ✅ 是 |
| `ladder` | 系统闲时自动对局 | ✅ 是 |
| `contest` | 赛事内对局 | ❌ 否（仅计入赛事内 stage 积分） |
| `human` | 人类 vs Bot | ❌ 否（人类无评分） |

> **评分隔离**：`contest` 只计入赛事内积分榜；`human` 不计 Glicko（人类无 rating 行）；
> `challenge`/`table`/`ladder` 更新全局评分。

## 人类对战（match_type=human）

登录用户可作为**人类玩家**亲自上手对战 Bot（挑战页选「人类亲自上场」）：

- 建局：`POST /api/matches/human`（`bot_id` + `human_seat` 0/1）→ 跳 `/play/:id`。
- 双向通道：**WebSocket** `/api/matches/:id/play`（鉴权：query `token` 或 cookie）。
  - 推送 `snapshot`（历史）+ 事件流 + `your_turn`（轮到人类，含 request）+ `match_end`。
  - 人类回传落子：棋类 `{x,y}`、扑克 `{a:"f|c|k|r|all", x?:raise-to}`。
- **资源**：走独立并发信号量（默认 `human_max_concurrent=4`，不占 Bot 半负载槽）；
  人类决策超时 `human_action_timeout`（默认 120s，超时回安全默认：扑克 fold / 棋类判负）；
  每用户同时进行的人类局 ≤ 1。
- 不计 Glicko-2 天梯。

> 棋类棋盘可点击落子；扑克提供 Fold/Check/Call/Raise/Allin 按钮栏。

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
