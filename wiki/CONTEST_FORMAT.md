# 赛制模板

组织者发布比赛时选择一个**赛制模板**（template），由若干**阶段**（stage）顺序组成。
管理员可在管理端「赛制模板」页用**图形化分阶段表单**增删改查模板（内置模板可改不可删）。

> 模板存于 `contest_templates` 表；组织者创建比赛时所选模板的 stages + 对局参数会**冻结快照**进该比赛，
> 之后改模板不影响已创建的比赛。

## 模板结构

```
id            模板标识（小写字母/数字/_，2–32）
name          显示名
game_id       holdem | gomoku | pencil
match_config  对局参数（每游戏一份，见下）
stages[]      阶段数组（按顺序执行）
is_builtin    内置标记（内置可改不可删）
```

## 对局参数 match_config（通用化，取代德扑专属的 hands_per_match）

每款游戏的每场对局参数不同，由 `match_config` 表达：

| game_id | match_config | 说明 |
|---------|--------------|------|
| holdem  | `{"hands": 70}` | 每场手数（1–500） |
| gomoku  | `{}` | 单局，无可调参数 |
| pencil  | `{"n_dots": 11}` | 点阵边长（3–15） |

创建比赛时按所选游戏显示对应字段；派遣对局时按 game 取参数透传给引擎
（holdem→`hands`，pencil→`n_dots`，gomoku 单局）。旧的 `hands_per_match` 字段保留向后兼容。

## 阶段（stage）配置

每个阶段是 `{key, type, scoring, ...}`，字段随 `type` 动态变化。

### 阶段类型 type（6 种）

| type | 说明 | 专属字段 |
|------|------|----------|
| `round_robin` | 单循环 | — |
| `double_round_robin` | 双循环（先后手对调） | — |
| `group_round_robin` | 蛇形种子分组单循环 | `group_count`、`advance_per_group` |
| `group_double_round_robin` | 分组双循环 | `group_count`、`advance_per_group` |
| `swiss` | 瑞士轮（按积分就近配对，避开重复） | `rounds`、`advance_count` |
| `single_elimination` | 单败淘汰（种子位 + bye） | — |

### 计分 scoring（2 种）

| scoring | 胜/平/负 | 适用 |
|---------|----------|------|
| `poker_3_1_0` | 3 / 1 / 0 | 扑克 |
| `ccgc_2_1_0` | 2 / 1 / 0 | 棋类（CCGC） |

### 阶段通用字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `key` | string | 阶段标识（前端 tab 名） |
| `advance_count` | int | 晋级总人数（top-N） |
| `rest_after_minutes` | int | 本阶段结束后休息分钟数（>0 进休息态，可换 Bot） |
| `allow_bot_swap_in_rest` | bool | 休息期是否允许换 Bot |

> `rounds=0`（swiss）表示按 `ceil(log2(n))` 自动确定轮数。

## 阶段状态机

`draft → open → running ⇄ rest → finished`

- 每阶段对阵生成后派遣（`match_type=contest`）；
- 阶段全部完成 → 快照积分 → 若有下一阶段且配了休息 → 进 `rest`（休息期可换 Bot）→ 恢复后晋级下一阶段；
- 末阶段完成 → `finished`。

详见 [对局](#/wiki?slug=match)。

## 管理员配置赛制

1. 进入管理端「赛制模板」tab；
2. 新建模板或编辑现有模板：填 id/名称/游戏 → 配 match_config → 用分阶段表单增删阶段、
   随 type 切换字段 → 「预估场数」输入人数实时预览各阶段/总场数；
3. 保存即生效（**组织者创建比赛时立即可选**，前后端同源）。

### API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/contests/templates` | 组织者：列出模板（可 `?game=` 过滤） |
| GET | `/api/admin/templates` | admin：列出 |
| POST | `/api/admin/templates` | admin：新建（校验） |
| PUT | `/api/admin/templates/{id}` | admin：更新 |
| DELETE | `/api/admin/templates/{id}` | admin：删除（内置不可删） |
| POST | `/api/admin/templates/preview` | admin：预估场数 `{stages, n}` |

> 公开端与 admin 端**同源读表**，确保组织者看到的模板与 admin 配置一致。

## 默认内置模板

| id | 游戏 | 赛制 |
|----|------|------|
| `holdem_swiss_ko` | 德州 | 瑞士 → 单败 |
| `holdem_rr` | 德州 | 单循环（小规模） |
| `gomoku_group_drr_ko` | 五子棋 | 分组双循环 → 单败 |
| `gomoku_swiss_ko` | 五子棋 | 瑞士 → 单败 |
| `pencil_group_drr_ko` | 点格棋 | 分组双循环 → 单败 |
| `pencil_swiss_ko` | 点格棋 | 瑞士 → 单败 |
| `board_rr` | 棋类 | 双循环（课堂演示） |
