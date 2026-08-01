# Bot 详情页

每个 Bot 都有独立的详情页 `/bot/:id`，展示其档案、对局历史、对手战绩与评分变化曲线。从排行榜、首页最新对局、对局历史等任何出现 Bot 名的地方点击即可进入。

## 页面内容

**顶部信息卡**：
- Bot 显示名 / 用户名（@name）
- 游戏标签（德州扑克 / 五子棋 / 点格棋）
- 简介（description）
- 所有者（链接到用户主页 `/user/:name`）
- 版本号、平台（format/os-arch）、创建时间、停用状态

**核心指标**（4 个卡片）：
- **Rating**：Glicko-2 评分（含 rd 不确定度）
- **胜率**：`(胜 + 平×0.5) / 总场`，标注总场数
- **胜**：累计胜场
- **负/平**：累计负场与平局

**三个 Tab**：
1. **对局历史**：该 Bot 最近 30 场对局（时间、对手名→链接到对手 Bot 详情、胜负结果彩色标记、对局类型、回放链接）。
2. **对手战绩**：对该 Bot 各对手的胜负表（按交手次数倒序，含胜率）。
3. **评分曲线**：Glicko-2 评分随时间变化的 SVG 折线图（每次评分更新落一个数据点）。

## 后端端点（均公开，无需登录）

| 端点 | 说明 |
|------|------|
| `GET /api/bots/{id}/profile` | Bot 档案聚合：bot 信息 + owner + rating + 胜率字段 |
| `GET /api/bots/{id}/matches?limit=&offset=` | 该 Bot 的对局历史（复用 list_matches，含双方 bot 名） |
| `GET /api/bots/{id}/opponents?limit=` | 该 Bot 对各对手的战绩（从 pair_stats 读，视角还原） |
| `GET /api/bots/{id}/rating-history?limit=` | 评分变化时序（rating_history 表，画曲线用） |

## 数据来源

- **档案/胜率**：`ratings` 表（rating/rd/vol/wins/losses/draws/net_chips/matches_played）+ `bots` 表 + `users` 表 JOIN。
- **对局历史**：`matches` 表（按 `bot_a_id=? OR bot_b_id=?` 查）。
- **对手战绩**：`pair_stats` 表（`a_wins/a_losses/draws` 列，按 `(min_id, max_id)` 规范化存储，读取时按查询方向还原视角）。
- **评分曲线**：`rating_history` 表（每次 `_apply_ratings` 落一条快照，每 bot 截断保留最近 200 条）。

`pair_stats` 的胜负计数在对局完成评分更新时（`orchestrator._apply_ratings`）顺带累积——challenge/table/ladder 类型对局都会记录（contest 与 human 类型不更新评分，故不计入）。
