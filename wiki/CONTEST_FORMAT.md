# 赛制模板

组织者发布比赛时选择一个**赛制模板**（template），由若干**阶段**（stage）顺序组成。
管理员可在管理端「赛制模板」页用**图形化分阶段表单**增删改查模板（内置模板可改不可删）。

> 组织者创建比赛时所选模板的阶段与对局参数会**冻结快照**进该比赛，之后改模板不影响已创建的比赛。

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
| pencil  | `{"n_dots": 6}` | 点阵边长（默认 6；可调 3–15） |

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

`draft → open → published → running ⇄ rest → finished`

- **draft**：创建后未开放报名；
- **open**：开放报名（选手派遣 Bot）；`registration_opens_at` 到点自动开放（或组织者手动）；
- **published**：报名截止、排期已发布、等待开赛。`registration_closes_at` 到点自动出排期（或组织者手动 `/publish`）；选手可看到完整对阵表 + 每场计划开赛时间（`scheduled_at`），但比赛未开始；
- **running**：比赛中。`starts_at`（或逐场 `scheduled_at`）到点自动开打（dispatch pairing）；每阶段对阵生成后派遣（`match_type=contest`）；
- **rest**：阶段间休息（可换 Bot）。`rest_ends_at` 到点自动恢复；
- **finished**：末阶段完成。

**时间调度器**：后台周期扫描赛事的 `registration_opens_at` / `registration_closes_at` / `starts_at` / `rest_ends_at` / `scheduled_at` 字段，到点自动推进阶段。组织者手动按钮（`/open` `/publish` `/start` `/resume` `/advance`）始终可用——到点自动 + 手动可提前。

**逐场排期**：每场对阵（pairing）有独立的 `scheduled_at`（计划开赛时间）。阶段可配 `round_stagger_minutes`（轮次间错峰分钟数）；`scheduled_at` 到点才 dispatch。

详见 [对局](#/wiki?slug=match)。

## 管理员配置赛制

1. 进入管理端「赛制模板」tab；
2. 新建模板或编辑现有模板：填 id/名称/游戏 → 配 match_config → 用分阶段表单增删阶段、
   随 type 切换字段 → 「预估场数」输入人数实时预览各阶段/总场数；
3. 保存即生效（**组织者创建比赛时立即可选**）。

### 模板查询

组织者创建比赛时可查看可选模板：

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/contests/templates` | 列出模板（可 `?game=` 过滤） |

## 默认内置模板（9 个）

平台内置 9 个模板（内置可改不可删）。部分模板用于特定的赛事阶段（预赛 / 决赛）：

| id | 游戏 | 赛制 | 备注 |
|----|------|------|----------------|
| `holdem_swiss_ko` | 德州 | 瑞士 → 单败 | 默认 |
| `holdem_rr` | 德州 | 单循环（小规模） | 默认 |
| `holdem_prelim_swiss` | 德州 | 单阶段瑞士（全员排名） | **预赛**：全员唯一正式名次 |
| `holdem_final_ranked` | 德州 | 全员单循环 → Top8 双循环 | **决赛**：Top8 循环结果嵌回总榜合成正式榜 |
| `gomoku_group_drr_ko` | 五子棋 | 分组双循环 → 单败 | 默认 |
| `gomoku_swiss_ko` | 五子棋 | 瑞士 → 单败 | 默认 |
| `board_rr` | 棋类 | 双循环（课堂演示） | 默认 |
| `pencil_group_drr_ko` | 点格棋 | 分组双循环 → 单败 | 默认 |
| `pencil_swiss_ko` | 点格棋 | 瑞士 → 单败 | 默认 |

### 预赛 / 决赛与正式名次（简要）

- 创建比赛时模板可声明赛事阶段（预赛 `preliminary` / 决赛 `final` / 普通 `standalone`），赛事继承该阶段。
- 阶段结束时计算**全员正式名次**（破同分规则：积分 → Buchholz Cut1 → Sonneborn-Berger → 直接交手 → 净码等）。
- 决赛模板 `holdem_final_ranked`：Stage1 全员单循环；Stage2 把 Top8 循环结果嵌回总榜。

## 赛制合理性指南（人数 → 场次 → 时长 → 推荐）

不同人数下各赛制的**对局场数**差异巨大，直接影响赛事时长与可行性。下表按规模给出推荐。

### 场次公式

| 赛制 | 场次公式 | 备注 |
|------|---------|------|
| 单循环 `round_robin` | `n*(n-1)/2` | 全员互打，最公平；人数过多时不现实 |
| 双循环 `double_round_robin` | `n*(n-1)` | 先后手对调 |
| 分组循环 `group_round_robin` | `Σ 每组 n_i*(n_i-1)/2` | 每组人数不宜过多 |
| 分组双循环 `group_double_round_robin` | `Σ 每组 n_i*(n_i-1)` | 同上 |
| 瑞士 `swiss` | `rounds * (n//2)`，`rounds=ceil(log2(n))` | 大规模首选 |
| 单败淘汰 `single_elimination` | `n-1` | 最快但运气成分大 |

### 决策表（人数 → 推荐赛制）

| 人数 | 推荐赛制 | 场次 | 说明 |
|------|---------|------|------|
| **≤12** | 单/双循环 | n*(n-1)/2（≤66） | 全员互打最公平 |
| **13~32** | 瑞士→淘汰 或 分组循环→淘汰 | swiss: ceil(log2(n))*(n//2)（如 32人=5轮80场） | 瑞士 log2 轮即定排名，再单败决冠军 |
| **33~128** | 瑞士→淘汰（advance_count 控晋级） | 如 64人 swiss 6轮192场 + 淘汰 | `advance_count=8/16` 控淘汰规模 |
| **129~512** | 瑞士→淘汰 | 如 500人 swiss 9轮2250场 + 淘汰 | swiss 唯一可行循环式；单败 499场最快 |
| **全员单循环 @500** | ❌ 不可行 | 124750 场 | 不现实；改用 swiss |

### 关键约束

- **循环人数上限**：全员单/双循环 + 分组循环的每组人数有上限（默认 12，admin 可调）。超限会提示改用 Swiss / 分组模板——500 人全员循环 = 124750 场不现实。
- **瑞士轮数** = `ceil(log2(n))`（500人=9轮），每轮 n//2 场，是大规模唯一可行的"循环式"赛制。
- **单败淘汰** = n-1 场（500人=499场），最快但单场定胜负、运气成分大，适合决冠军而非排全员名次。

### 推荐组合

- **小规模决公平排名**（≤12）：单/双循环（全员互打）。
- **中规模决冠军**（13~128）：瑞士（定前 N）→ 单败淘汰（决冠军）。`holdem_swiss_ko` / `gomoku_swiss_ko`。
- **大规模决冠军**（129~512）：瑞士（advance 8/16）→ 单败。`swiss_ko` 模板。
- **分组+淘汰**（任何规模，每组≤12）：`group_*_ko` 模板（蛇形种子分组循环→淘汰），兼顾公平与效率。

### 预估场数

admin 端「赛制模板」页的「预估场数」可实时预览各阶段场数。
