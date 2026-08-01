# 赛事对阵图 + 显示 Bot 名

赛事详情页（`/contests/:id`）的报名列表、积分榜、对阵均显示 Bot 名/用户名（替换原来的裸 `#ID`），并新增对阵图数据端点。

## 改进点

- **报名列表**：Bot 名（链接到 Bot 详情）+ @用户名（链接到用户主页）+ 种子/分组/淘汰标记。
- **积分榜**：排名 # + Bot 名（链接）+ 积分/W-D-L/净筹码。
- **对阵列表**：轮次/分组标签 + 双方 Bot 名（链接，胜者绿色加粗、负者灰色）+ 状态 + 观战链接。
- **对阵图数据端点** `GET /api/contests/{id}/bracket`：返回带 bot 名/owner 名/对局 winner 的对阵，便于前端画 bracket 树（数据含 `stage_idx/round_num/group_id/bracket_slot/match_winner`）。

## 后端

- `store.contest_bracket(contest_id)`：JOIN bots + users + matches，返回带名 + winner 的对阵。
- `store.contest_entries_named(contest_id)`：JOIN bots + users，返回带名的报名。
- `GET /api/contests/{id}`：entries 与 pairings 改用 named 版本；standings 补 `bot_name`。
- `GET /api/contests/{id}/bracket`（公开）：对阵图聚合数据。

## 数据基础

`contest_pairings` 表已有 `stage_idx/round_num/group_id/bracket_slot/match_id`（JOIN matches 取 winner）—— 足以支撑前端画单败淘汰 bracket 树与瑞士/循环轮次分组表。`contest_entries` 有 `seed/group_id/eliminated`。
