# 段位称号 + 排名变化趋势

排行榜与 Bot 详情页展示 rating 对应的段位徽章 + 升降趋势箭头。

## 段位体系

按 Glicko-2 rating 分档（边界含最低值）：

| 段位 | key | 最低 rating | 颜色 |
|------|-----|------------|------|
| 大师 | master | 2200 | 紫 |
| 专家 | expert | 2050 | 靛 |
| 高手 | gold | 1900 | 琥珀 |
| 熟练 | silver | 1750 | 石板 |
| 进阶 | bronze | 1600 | 翠 |
| 新手 | novice | 0 | 天蓝 |

**数据来源**：`engine/tiers.py`（`TIERS` 列表，降序匹配；`tier_for(rating)` → `Tier`）。前端 `lib/tiers.ts` 镜像（修改需同步）。

## 排名变化趋势（rating_delta）

每次对局更新评分时，`_apply_ratings` 落一条 `rating_history` 快照（PR-1）。排行榜的 `rating_delta` = 当前 rating − 上一条历史评分：

- `+N`（绿色 ▲）：评分上升
- `-N`（红色 ▼）：评分下降
- `null`：仅 1 场或无历史（不显示箭头）

## 端点

- `GET /api/tiers`（公开）：段位定义列表（前端镜像校验 / 等级 gating 用）。
- `GET /api/leaderboard`：每行含 `tier_name/tier_level/tier_key` + `rating_delta`。
- `GET /api/bots/{id}/profile`：含 `tier_name/tier_level/tier_key`。

## 前端

- `lib/tiers.ts`：`TIERS` 镜像 + `tierFor(rating)` + `trendDelta(delta)`。
- 排行榜：段位列（彩色徽章）+ Rating 列旁升降箭头。
- Bot 详情：Rating 卡片显示段位徽章。

> 后续 PR-9（等级系统）的等级 gating 可基于 `tier.level` 推导（tier.level ≥ 1 即「进阶」以上）。
