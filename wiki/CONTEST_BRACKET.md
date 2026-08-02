# 赛事对阵图 + 显示 Bot 名

赛事详情页（`/contests/:id`）的报名列表、积分榜、对阵均显示 Bot 名/用户名（替换原来的裸 `#ID`），并新增对阵图数据端点。赛程表（对阵）按赛制类型分两种展示：

- **淘汰赛**（`single_elimination`）：树状对阵图（`BracketTree` 组件）——按 `bracket_slot` 排列，每轮一列，胜者高亮、负者灰色划线，横向滚动 + 轮次折叠/跳转（大规模如 500 人 = 9 轮 512 槽可折叠到关注轮）。
- **瑞士 / 循环 / 分组**：按轮次（或分组）折叠的列表（`PairingFoldedList`）——大规模（>60 场或 >6 组）默认收起，展开看明细，顶部显示每组场次/已完成数。

## 改进点

- **报名列表**：Bot 名（链接到 Bot 详情）+ @用户名（链接到用户主页）+ 种子/分组/淘汰标记。
- **积分榜**：排名 # + Bot 名（链接）+ 积分/W-D-L/净筹码。
- **对阵展示**：按赛制类型分树状图（淘汰）/折叠列表（瑞士·循环·分组）；双方 Bot 名（链接，胜者绿色加粗、负者灰色）+ 状态 + 查看链接。
- **对阵图数据端点** `GET /api/contests/{id}/bracket`：返回带 bot 名/owner 名/对局 winner 的对阵，便于前端画 bracket 树（数据含 `stage_idx/round_num/group_id/bracket_slot/match_winner`）。

## 后端

- `store.contest_bracket(contest_id)`：JOIN bots + users + matches，返回带名 + winner 的对阵。
- `store.contest_entries_named(contest_id)`：JOIN bots + users，返回带名的报名。
- `GET /api/contests/{id}`：entries 与 pairings 改用 named 版本；standings 补 `bot_name`。
- `GET /api/contests/{id}/bracket`（公开）：对阵图聚合数据。

## 数据基础

`contest_pairings` 表已有 `stage_idx/round_num/group_id/bracket_slot/match_id`（JOIN matches 取 winner）—— 足以支撑前端画单败淘汰 bracket 树与瑞士/循环轮次分组表。`contest_entries` 有 `seed/group_id/eliminated`。
