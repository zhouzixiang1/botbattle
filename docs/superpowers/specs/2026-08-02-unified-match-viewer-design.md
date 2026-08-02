# 统一实时/回放对局页 设计文档

> 日期：2026-08-02 · 分支：`feat/unified-match-viewer`
> 目标：把当前分裂的两个页面（`/match/:id` 回放 + `/watch/:id` 直播）合并为**单一页面** `/match/:id`，统一播放模型；座位显示双方 BOT 名 + 用户名。

## 1. 背景与问题

当前有**两个独立、逻辑重复**的页面：

| 页面 | 路由 | 数据源 | 播放逻辑 | 缺陷 |
|---|---|---|---|---|
| `MatchDetail.tsx` | `/match/:id` | REST `GET /api/matches/:id` → `replay.events_json` | **手写** stepIdx/playing/SPEEDS/定时器（不用 `usePlayback`） | 打开停在**终局**；running 对局只显示静态部分回放；无直播路径 |
| `ArenaWatch.tsx` | `/watch/:id` | SSE `/api/matches/:id/events` | 用 `usePlayback` + `PlaybackControls` | 直播默认 `stepIdx=-1` → **瞬间跳最新**，非"人反应节奏"；座位无名称 |

核心重复：两套播放游标/速度/定时器逻辑（`SPEEDS` 常量各定义一份）。核心缺失：座位身份（`get_match` 不 JOIN bot/owner 名，两页都只显示"座位 0/1"）。

## 2. 需求（已与用户确认）

| # | 决策 | 用户选择 |
|---|---|---|
| 直播节奏 | 新动作**按回放速度**（1x≈0.7s/手）逐手显示；Bot 瞬间连走则游标落后、显示"落后 N 手"、可"跳到最新"；Bot 慢时光标停最新等真实动作，**不人为加延迟**。用户也可点"下一步"或看右边日志看最新。 |
| 回放初始 | 直播**结束的**对局：若停在中间，继续放完；**已结束**对局打开：自动从头播放。 |
| 路由 | `/match/:id` 为唯一页（`/watch/:id` 重定向到它）；`/arena` 仍作直播列表入口保留。 |
| 座位显示 | 两行：粗体 **BOT 名** + 灰色 @用户名；人类对局真人座位显示 `@用户名 (你)`。 |
| 直播切入 | 中途进入定位到**最新**，然后按回放速度推进（DVR 模型）。 |

## 3. 关键现状（影响设计的事实）

- **事件无时间戳。** 节奏纯靠"每步固定 ms"（`SPEEDS`: 0.5x/1x/2x/4x）。**无需引入时间戳**——用户要的就是"回放速度"，当前 step-paced 模型刚好够用。
- **`usePlayback` 的 `stepIdx=-1` 语义是"贴尾"**（append 后 cur 自动=末尾，瞬间跳最新）。这与直播需求"按节奏推进"**冲突**——需要新的播放语义。
- **`reduceEvents` 是纯函数**，对任意事件前缀重新归约即得棋盘状态 → 拖动/seek 天然支持（拖到哪步，棋盘重算到那步）。
- **`get_match()` 不 JOIN** bot/owner 名（`db.py:1180` `SELECT *`）。`list_matches()` 有 JOIN 模式可参照。
- **人类对局** `match_type=="human"`：两 seat 复用同一 `bot_id`，真人靠 `human_user_id`/`human_seat` 区分。

## 4. 设计

### 4.1 播放模型重设计（核心）

引入**两种播放模式**，由页面根据对局状态自动选择：

**模式 A — 回放模式（已结束 `completed`/`aborted`）**
- 一次性 `setAll(events_json)` 填满 buffer。
- 进入即**从头自动播放**（`stepIdx=0, playing=true`），按 SPEEDS 步进到末尾自动停（`playing=false, stepIdx=total-1`）。
- 用户可暂停/拖动/步进/调速。

**模式 B — 直播模式（`running`/`pending`）**
- 开 SSE，`snapshot` 先 `setAll(history)` 把游标定位到**末尾**（最新），`playing=true`。
- 之后每个新事件 `append` 进 buffer。**关键**：定时器按 SPEEDS 步进游标，**追到末尾就停住等**（而非转回 `-1` 瞬间跳最新）。
- 游标落后末尾时显示"落后 N 手"+"跳到最新"按钮；点"跳到最新"=`seek(total-1)`。
- 收到 `match_end`/`error`：**不强制跳结局**。若游标在中间（用户暂停过），保持当前位置继续按节奏放完剩余（"还停留在中间的，继续放完"）。SSE 关闭后转为纯回放（buffer 不再增长，游标走到末尾停）。

**实现方式**：扩展 `usePlayback`，新增一个 `liveMode` 布尔参数控制"追到末尾"的行为：
- `liveMode=false`（回放）：追到末尾 → `playing=false`（停）。
- `liveMode=true`（直播）：追到末尾 → `playing` 保持 `true` 但游标不动，等下次 `append` 增长 buffer 后继续步进。
- 新增 `jumpToLive()` = `seek(total-1)`（"跳到最新"）。
- 直播模式下 `atLive` 语义改为"游标在末尾"；`lag` = 末尾 - 游标（落后手数）。
- **废弃 `stepIdx=-1` 贴尾语义**在直播跟随路径上的使用——直播也用显式 stepIdx（= total-1），由定时器驱动增长。`-1` 仅保留为"回放未启动"的初始哨兵值，或彻底用 `0`/`total-1` 显式值。

> 注：`stepIdx=-1` 贴尾逻辑会被替换，但 `togglePlay`/`step`/`seek` 等公开 API 形态保持兼容，避免动 `PlaybackControls`。

### 4.2 页面合并

**单一组件** `MatchViewer`（替换 `MatchDetail` + `ArenaWatch` 的合体），路由 `/match/:id`：

1. **先 REST 探测状态**：`GET /api/matches/:id` → 读 `match.status`。
   - `completed`/`aborted` → 回放模式：`setAll(events)` + `seek(0)` + `playing=true`。
   - `running`/`pending` → 直播模式：`setAll(events)` + `seek(total-1)` + `playing=true` + 开 SSE。
2. **SSE 仅直播模式开**：复用 ArenaWatch 的 EventSource 逻辑（`snapshot`→setAll、增量→append、`match_end`→关流）。
3. **UI 复用**：`MatchBoard` + `PlaybackControls` + 右侧动作日志（合并两版 `eventDesc`，统一一份）。
4. **扑克手导航器**（逐手跳转 + 赢家绿点）从 MatchDetail 保留——只在 `!isBoard` 时显示。
5. **状态徽章**：直播显示"直播中"+脉动点 + 落后手数；回放显示 `completed`/`aborted`。

**路由调整**（`app-shell.tsx`）：
- `/match/:id` → `<MatchViewer />`（唯一对局页）。
- `/watch/:id` → `<Navigate to="/match/:id" replace />`（重定向）。
- `/arena`（无 id）→ 保留为"直播列表入口"（或直接复用 MatchViewer 的空态：列出 running 对局）。**先保留 ArenaWatch 的空态 UI**，仅把带 id 的观赛重定向走。

**删除**：`MatchDetail.tsx`、`ArenaWatch.tsx` 的播放/日志逻辑合进 `MatchViewer.tsx`（新文件，或直接重命名 MatchDetail 为 MatchViewer 并吸收 ArenaWatch 逻辑）。

### 4.3 座位身份显示（前后端协同）

**后端**（`bzplat/backend/store/db.py` + `api_routes.py`）：
- 新增 `get_match_detailed(id)`（或扩展 `get_match`）：JOIN `bots`（取 name/display_name）+ JOIN `users`（取 owner username/display_name），返回 `bot_a`/`bot_b` 各含 `{id, name, display_name, owner_name, owner_display}`。**参照 `list_matches` 的 JOIN**（`db.py:1248`）。
- `match_detail` 路由 + SSE/WS `snapshot.match` 改用此 enriched 数据。
- **人类对局特例**：`human_seat` 那侧的"bot"显示为真人——返回时把 `human_user_id` 对应的用户名塞进对应 seat 的 `owner_name`，并标 `is_human: true`。

**前端**：
- `MatchBoard` 增加 `seats?: SeatInfo[]`（`[{botName, ownerName, isHuman}, ...]`）可选 prop，透传给各 `Board`。
- `PokerTable.SeatBox`：`座位 {idx}` 改为两行——粗体 BOT 名 + 灰色 @用户名；人类座 `@用户名 (你)`。
- 棋类（Gomoku/Pencil）：在棋盘旁的图例（"黑(0)/白(1)"）旁补 BOT 名；当前色块着色保留。

### 4.4 边界与一致性行为

- **`over`（对局结束）后**：回放模式播完自动停；直播模式 match_end 到达后游标走完剩余停。控制条始终可用（可拖回去重看）。
- **重连**：直播模式 SSE 断线 → `onerror` 显示"连接异常，重连中…"，可选自动重连（指数退避，最多 3 次）。**V1 先不做自动重连**，显示错误 + 手动刷新按钮（避免过度设计）。
- **空 buffer**：`connecting`/`加载中` 空态。
- **评论**（Comments 组件）：从 MatchDetail 保留到 MatchViewer 底部。

## 5. 改动清单（文件级）

**后端**
- `bzplat/backend/store/db.py`：新增 `get_match_detailed()`（JOIN bots+users），或给 `get_match` 加 `detailed` 形参。
- `bzplat/backend/api_routes.py`：`match_detail` 用 enriched match；SSE `subscribe` 的 snapshot.match 同步。
- 测试：`tests/test_match_detail.py`（或同类）加 seat-name JOIN 断言 + 人类对局 is_human 断言。

**前端**
- `src/components/use-playback.ts`：扩展 `liveMode` + `jumpToLive`，重构追末尾行为（**TDD：先写 hook 测试**）。
- `src/pages/MatchViewer.tsx`（新，或重命名 MatchDetail）：合并两页逻辑，按 status 选模式。
- `src/components/MatchBoard.tsx`：加 `seats` prop 透传。
- `src/games/base.ts`：`BoardProps` 加 `seats?`。
- `src/components/poker/PokerTable.tsx`：SeatBox 显示名称两行。
- `src/components/{gomoku,pencil}/*Board.tsx`：图例补名称。
- `src/components/shell/app-shell.tsx`：路由 `/watch/:id` 重定向、`/match/:id` → MatchViewer。
- 删除 `src/pages/ArenaWatch.tsx`、`src/pages/MatchDetail.tsx`（逻辑迁入 MatchViewer；`/arena` 空态逻辑保留为一小段或并入 MatchViewer）。

**文档**
- `wiki/MATCH.md`：观赛/回放统一页说明 + 座位名称显示。
- `doc/DESIGN.md` §前端：MatchViewer 取代双页。

## 6. 测试策略

- **后端**：`get_match_detailed` 返回 bot/owner 名；人类对局 seat 标 is_human；snapshot.match 含名。
- **前端 hook**（`use-playback.test.ts`，新）：
  - 回放模式：setAll + seek(0) + playing → 步进到末尾停。
  - 直播模式：setAll + append 时游标按节奏追赶、追到末尾等、lag 正确、jumpToLive。
  - 直播 match_end 到达、游标在中间时不跳结局。
- **npm run build** + 截图验证（`scripts/screenshot_verify.py`）零回归（座位名、直播徽章、回放控制）。

## 7. 非目标（V1 不做）

- 事件加时间戳 / 真实墙钟节奏（用户明确要"回放速度"，step-paced 足够）。
- SSE 自动重连（V1 手动刷新）。
- 直播列表页大改（`/arena` 空态先保留）。
- 棋类座位的复杂身份卡（先在图例补名即可）。

## 8. 风险

- **`usePlayback` 重构影响 ArenaWatch→MatchViewer 迁移**：先重构 hook（带测试）再迁页面，分两步提交。
- **`stepIdx=-1` 语义变更**：需确认 `PlaybackControls` 不依赖 `-1`（看代码它只读 `atLive`/`lag`/`cur`，安全）。
- **JOIN 性能**：`get_match` 单行查询加 JOIN 无影响。
