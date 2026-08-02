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
- **`your_turn` 持久化**：`your_turn` 事件**同时进入实时推送与持久化事件流（snapshot 历史）**，
  且发出即落库（不等下一个 checkpoint）。这样前端重连 / React StrictMode 重挂载 / 迟到连接时，
  能从 snapshot 历史正确恢复「轮到我」状态（前端 `myTurn` 由事件流推导，不依赖瞬时消息）。
- **资源**：走独立并发信号量（默认 `human_max_concurrent=4`，不占 Bot 半负载槽）；
  人类决策超时 `human_action_timeout`（默认 120s，超时回安全默认：扑克 fold / 棋类判负）；
  每用户同时进行的人类局 ≤ 1。
- **连续超时自动中止**：人类连续 `human_max_consecutive_timeouts`（默认 5）次不响应即中止对局
  （`aborted`，reason=`human_inactive`，广播 `error`），避免扑克 70 手最长 2.3 小时死磕占用人类槽、
  锁死 per-user 名额。棋类一手非法即结束，不会累积到此阈值。对局异常/中止时**无条件释放** per-user 锁。
- **Bot 崩溃快速中止**：若 Bot 二进制启动即崩（进程退出/EOF，如动态链接库缺失、glibc 不匹配），
  对局立即 `aborted`（reason=`bot_crashed`）并广播 `error`，而非吞成默认动作死磕数小时。
  该行为对三款游戏（holdem/gomoku/pencil）一致——引擎层识别 `BotCrashedError`（不可恢复）与
  普通决策错误（可恢复，判对手赢）两类，前者上抛触发 abort、后者按规则处理。
- 不计 Glicko-2 天梯。

> 棋类棋盘可点击落子；扑克提供 Fold/Check/Call/Raise/Allin 按钮栏。

## 状态

`pending` → `running` → `completed` | `aborted`

- `pending`：已创建 match 行，尚未获取并发信号量。
- `running`：已获信号量、引擎正在跑（DB `status='running'`，对应 admin 面板「进行中」计数）。
- `completed`：引擎跑完，已写 `winner / earnings / replay`。
- `aborted`：异常（容器 OOM、双方崩溃等）。

> **孤儿对局自愈**：服务非正常退出（崩溃/重启）后，DB 里残留的 `running` 记录已无对应内存协程（尤其人类对局的 `_human_turns` Future）。服务启动时 `lifespan` 调 `Store.recover_orphan_matches()`，把所有残留 `running` 统一标为 `aborted`（`reason=orphan_after_restart`），避免永久卡死与并发/活跃用户计数泄漏。

SSE 观赛：先推送 `snapshot`（含当前事件历史，迟到者可补看），之后逐事件广播；
空闲时 25 秒发一次 `ping` 保活；`match_end` / `error` 后流结束。回放事件流增量落盘
（`settle` / `hand_start` / `match_end` / `move` / `match_start` / `your_turn` 或每 5 个事件；
其中 `your_turn` 在人类对局中**发出即落库**，保证前端重连可恢复）。
每个订阅者的事件队列 `maxsize=2000`（Bot 决策极快时减少丢事件）；满时丢最旧事件保最新。

## 错误与后果

| 情况 | holdem | gomoku / pencil |
|------|--------|-----------------|
| 决策超时 / 崩溃 | 视为 fold | 判负 |
| 非法动作 | fold | 判负 |
| 容器 OOM 等 | 对局 abort 或该方失败 | 同左 |

资源限制详见 [运行时](#/wiki?slug=runtime)。
