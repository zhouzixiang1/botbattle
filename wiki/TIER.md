# 段位称号 + 排名变化趋势

排行榜与 Bot 详情页展示 rating 对应的段位徽章 + 升降趋势箭头。

## 段位体系（per-game）

段位曲线**按游戏独立**声明在 `games/<game>/tiers.py`（查表算法共享 `games.base.tier_for_in`）。当前三游戏默认阈值相同（可各自调整）：

| 段位 | key | 最低 rating | 前端徽章色系 |
|------|-----|------------|--------------|
| 大师 | master | 2200 | amber |
| 专家 | expert | 2050 | emerald |
| 高手 | gold | 1900 | primary（品牌 emerald） |
| 熟练 | silver | 1750 | slate |
| 进阶 | bronze | 1600 | teal |
| 新手 | novice | 0 | muted |

> 前端配色统一 emerald / amber / teal / slate（**无紫色**），与设计系统一致。

**数据来源**：

- 后端：`games/<game>/tiers.py` 声明曲线，挂在 **`GameSpec.tiers`**；查表经共享 `base.tier_for_in`，统一入口 **`registry.tier_for(game_id, rating)`**（GameSpec **无** `tier_for` 字段）
- API：`GET /api/tiers?game_id=`（不传则按实现默认；推荐按游戏拉取）
- 前端：`lib/tiers.ts` 的 `fetchTiers` / `useGameTiers(gameId)`（带缓存）；`TIERS` 常量仅作兜底镜像

## 排名变化趋势（rating_delta）

每次对局更新评分时，`_apply_ratings` 落一条 **per-game** `rating_history` 快照（`bot_id` + `game_id`）。排行榜的 `rating_delta` = 当前 rating − 上一条历史评分：

- `+N`（绿色 ▲）：评分上升
- `-N`（红色 ▼）：评分下降
- `null`：仅 1 场或无历史（不显示箭头）

## 端点

- `GET /api/tiers?game_id=`（公开）：该游戏段位定义列表。
- `GET /api/leaderboard`：每行含 `tier_name/tier_level/tier_key` + `rating_delta`（按游戏过滤）。
- `GET /api/bots/{id}/profile`：含 `tier_name/tier_level/tier_key`。

## 前端

- `lib/tiers.ts`：`useGameTiers` + `tierFor(rating, tiers)` + `trendDelta(delta)`。
- 排行榜：段位列（彩色徽章）+ Rating 列旁升降箭头。
- Bot 详情：Rating 卡片显示段位徽章。

> 用户 **XP 等级**（`users.level`）与段位是两套系统；数据集下载等 gating 看等级，见 [经验与等级](#/wiki?slug=xp-level)。
