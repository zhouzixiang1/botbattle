# 平台功能指南

本页汇总平台各项功能的用法：对局、裁判、段位与等级、锦标赛、Bot 详情、用户主页、社交、通知、设置。游戏规则见左侧 [德州扑克](#/wiki?slug=texas) / [五子棋](#/wiki?slug=gomoku) / [点格棋](#/wiki?slug=pencil)，通信协议见 [协议规范](#/wiki?slug=protocol)。

---

## 对局

对齐 [Botzone · 对局](https://wiki.botzone.org.cn/index.php?title=%E5%AF%B9%E5%B1%80) 概念。一局对局（match）由平台裁判引擎推进，Bot 通过 stdin/stdout 行协议参与。

### 创建对局

1. **上传 Bot**（选择游戏类型：holdem / gomoku / pencil）。
2. **挑战**：在挑战页选你的 Bot + 对手方式 → 创建对局 → 引擎跑完。
   - **搜索用户**：搜用户 → 选其该游戏的公开 Bot。
   - **自博弈**：选你自己的另一只同游戏 Bot（同一 owner 两不同 Bot 对战）。
   - **人类亲自上场**：你作为人类玩家对战 Bot（见下「人类对战」）。
3. **锦标赛**：按[赛制模板](#锦标赛)生成对阵，带 `contest_id`。
4. **闲时自动对局**：系统空闲时自动安排 Bot 对战维护天梯（`match_type=ladder`）。
5. **观赛**：进入对局页实时观赛或回放完整对局。

### 对局类型（match_type）

| 类型 | 来源 | 是否更新全局 Glicko-2 |
|------|------|----------------------|
| `challenge` | 用户主动挑战（含自博弈） | ✅ 是 |
| `ladder` | 系统闲时自动对局 | ✅ 是 |
| `contest` | 锦标赛内对局 | ❌ 否（仅计入赛事内积分） |
| `human` | 人类 vs Bot | ❌ 否（人类无评分） |

> **评分隔离**：`contest` 只计入赛事积分榜；`human` 不计 Glicko；`challenge`/`ladder` 更新全局评分。

### 人类对战（match_type=human）

登录用户可作为**人类玩家**亲自上手对战 Bot（挑战页选「人类亲自上场」）：

- **实时对战**：人类每回合在棋盘上点击落子（棋类）或点按 Fold/Check/Call/Raise/Allin 按钮（扑克）。
- **断线可恢复**：中途刷新或重连不丢「轮到我」状态，可从历史继续。
- **决策超时**：Bot 首次超时即技术判负（`completed + reason=timeout + technical_loss=1`）；人类落子等待默认 120 秒 / 回合，超时才回退安全动作（扑克 fold / 棋类判负），连续多次不响应会自动中止。Pencil 还累计双方各自固定的 900 秒总棋钟；Bot 耗尽走技术负，人类耗尽由裁判判负。
- **Bot 崩溃**：人类局中 Bot 若启动失败则对局中止；若已经进入对局后中途崩溃，则按裁判结果结算为 `completed`，并保留 `reason=crash`。
- 不计 Glicko-2 天梯。

### 状态与生命周期

`pending`（排队等并发槽）→ `running`（引擎跑）→ `completed`（正常结束，有胜者与回放）| `aborted`（异常中止）。

> **孤儿对局自愈**：服务重启后，残留的 `running` 与非赛事 `pending` 对局会被自动标记为 `aborted`，不会永久卡死；活跃赛事的 pending 不会被粗暴中止，而由 pairing 对账精确清理/重派。观赛进入对局页先收到当前事件历史快照（迟到者可补看已发生的局面），之后实时推送。

### 错误与后果

| 情况 | holdem | gomoku / pencil |
|------|--------|-----------------|
| Bot 决策超时 | `timeout` 技术判负 | `timeout` 技术判负 |
| 人类决策超时 | 视为 fold；连续超时会中止 | 交给裁判判负 |
| Bot 非法 JSON / 缺 `response` / 类型错误 | `protocol_error` 技术判负 | `protocol_error` 技术判负 |
| 格式正确但游戏内非法动作 | 视为 fold | 判负 |
| **对局中途**进程崩溃 / EOF | **计分判负**（`completed`，崩溃方负，`reason=crash`） | 同左 |
| **启动失败**·Bot-vs-Bot（挑战/天梯/桌赛/赛事） | `completed` + `technical_loss`（崩溃方负） | 同左 |
| **启动失败**·人类对战 | `aborted`（`bot_crashed`） | 同左 |

> 中途崩溃由引擎捕获并产出结果，Bot-vs-Bot 与人类对战都落为 `completed + reason=crash`；启动期失败由编排层处理。所有 Bot-vs-Bot 类型使用同一技术判负契约，只有人类对战的启动失败因没有可继续的 Bot 进程而中止。

### 观赛视觉

三游戏的观赛 / 回放 / 人类对战均采用动画渲染（对齐 botzone.org.cn）：

- **holdem**：发牌翻面、动作浮字、下注 / 弃牌 / 全押标记、筹码与**累计净筹码**、每手结算叠层；人类对战仅亮己方底牌。
- **gomoku**：落子动画 + 最后一手标记；图例含 **BOT 名**。
- **pencil**：未占边灰色细线、已占边着色动画、闭合格归属字；图例含双方名与比分，玩家卡显示每方 15 分钟累计棋钟及超时标记。
- **座位身份**：观赛 / 回放显示双方 **Bot 名**；人类对战的人类座显示 **真人用户名**。
- 统一对局页 `/match/:id`：实时观赛 + 回放；顶栏显示**胜者（名）**、**双方**、德州累计 / 点格比分、**match_type** 徽章、中止原因；Pencil 从回放/SSE 的 `time_used`/`time_out` 事件恢复双方剩余时间。
- 人类对战 `/play/:id`：实时对战页，提供合法操作按钮与 120 秒回合倒计时；Pencil 后端另执行每方 900 秒累计棋钟。

---

## 裁判

对齐 [Botzone · 裁判](https://wiki.botzone.org.cn/index.php?title=%E8%A3%81%E5%88%A4)。裁判负责接收 Bot 的着法、判定**合法性**、推进局面、判定**胜负**与**计分**。

### 判罚一览

| 情况 | 扑克（holdem） | 棋类（gomoku / pencil） |
|------|----------------|------------------------|
| Bot 信封/response 格式错误 | **`protocol_error` 技术判负** | **`protocol_error` 技术判负** |
| Bot 决策超时 | **`timeout` 技术判负** | **`timeout` 技术判负** |
| 格式正确但游戏内非法动作 | 视为 fold | **判负** |
| 对局中途进程崩溃 | **计分判负**（崩溃方负） | 同左 |
| 启动失败 | Bot-vs-Bot → 技术判负；人类对战 → aborted | 同左 |
| 棋盘满 / 资源耗尽 | — | 平局或按点数判胜 |

### 裁判源码公开可查

裁判是**公开可审计的规则定义**——区别于 Bot 的私有黑盒二进制（保护玩家智力成果），裁判源码对**全体玩家透明**。任何访客（无需登录）可在网页「裁判」页（`/judges`）查看每款游戏裁判引擎、行协议、结果契约的完整明文源码。规则透明是平台公正性的基础。

- 网页：顶部导航「裁判」页（`/judges`）
- API：`GET /api/judges`（列表）、`GET /api/judges/{game_id}/source`（源码全文）

### 参考裁判（可本地自测）

仓库提供**独立、无平台依赖**的参考裁判脚本，可在本地自测合法着 / 胜负 / 计分，逻辑与服务端引擎一致：

| 脚本 | 游戏 | 能力 |
|------|------|------|
| [`samples/judges/gomoku_judge.py`](../samples/judges/gomoku_judge.py) | 五子棋 | 合法着、4 方向连五、棋谱回放 |
| [`samples/judges/pencil_judge.py`](../samples/judges/pencil_judge.py) | 点格棋 | 交错网格、占边、成格连走计分 |
| [`samples/judges/holdem_judge.py`](../samples/judges/holdem_judge.py) | 德州扑克 | 七牌最佳五牌评估、raise 下限 |

```bash
python samples/judges/gomoku_judge.py          # 内置演示
python samples/judges/gomoku_judge.py --check  # 交互逐手判定
```

### 各游戏核心判定

- **五子棋**：以刚落的子为中心，4 个方向（横、竖、两斜）任一连续 ≥5 同色即胜（含长连，无禁手）。合法着 = 15×15 内且该点为空。
- **点格棋**：交错网格 `size = 2N-1`，占边后检查相邻格心四边是否全占 → 成格得分并**连走**（对方须回 `{"x":-1,"y":-1}` 的 pass）；双方各有固定 900 秒累计棋钟，耗尽判负。
- **德州扑克**：七牌取最佳五牌（高牌 < 一对 < 两对 < 三条 < 顺子 < 同花 < 葫芦 < 四条 < 同花顺）；raise 最小总额 = 2× 当前下注（首次 = 2bb）。

---

## 段位称号 + 排名趋势

排行榜与 Bot 详情页展示 rating 对应的段位徽章 + 升降趋势箭头。

### 段位体系（per-game）

段位曲线**按游戏独立**配置，当前三游戏默认阈值相同（可各自调整）：

| 段位 | 最低 rating |
|------|------------|
| 大师 | 2200 |
| 专家 | 2050 |
| 高手 | 1900 |
| 熟练 | 1750 |
| 进阶 | 1600 |
| 新手 | 0 |

### 排名变化趋势（rating_delta）

排行榜的 `rating_delta` = 当前 rating − 上一条历史评分：`+N`（绿色 ▲）上升 / `-N`（红色 ▼）下降 / `null`（仅 1 场或无历史）不显示箭头。

- `GET /api/tiers?game_id=`（公开）：该游戏段位定义列表。
- `GET /api/leaderboard`：每行含段位信息 + `rating_delta`（按游戏过滤）。

> 段位（Rating 派生）与下方的**用户 XP 等级**是两套独立系统。

---

## 经验与等级

用户通过平台活动获得经验（XP），累积升级（Level）。对标 Botzone 的 level + 活跃度体系。

### 经验奖励

| 活动 | 经验 |
|------|------|
| 参与一场对局 | 10 |
| 对局胜利（额外） | 15 |
| 锦标赛报名 | 50 |
| 发表评论 | 2 |
| 被关注 | 3 |

> 对局经验仅非 contest 类型发放（contest 内部对局不计）。被关注/评论经验给目标用户。

### 等级阈值

递增曲线：升到 level N 需累计 `100 × N × (N+1) / 2` 经验。

| Level | 0 | 1 | 2 | 3 | 4 | 5 |
|-------|---|---|---|---|---|---|
| 累计 XP | 0 | 100 | 300 | 600 | 1000 | 1500 |

- `GET /api/levels/info`（公开）：经验奖励值 + 等级阈值表。
- `GET /api/auth/me`、`GET /api/users/{name}/profile`：返回当前 xp/level。
- 前端：用户主页显示 Lv.N 徽章 + 经验进度条。

---

## 锦标赛

组织者发布锦标赛时选择一个**赛制模板**（template），由若干**阶段**（stage）顺序组成。管理员可在管理端「赛制模板」页用图形化分阶段表单管理模板（内置可改不可删）。

> 组织者创建锦标赛时所选模板的阶段与对局参数会**冻结快照**进该赛事，之后改模板不影响已创建的赛事。

### 阶段状态机

`draft → open → published → running ⇄ rest → finished`

- **draft**：未开放报名；
- **open**：开放报名（选手派遣 Bot）；`registration_opens_at` 到点自动开放（或组织者手动）；
- **published**：报名截止、**当前阶段/当前轮可确定的排期已发布**、等待开赛。`registration_closes_at` 到点自动出排期（或手动 `/publish`）；选手可看到这一批 pairing 与计划开赛时间（`scheduled_at`）。瑞士、淘汰等后续轮依赖本轮结果，不会在此时伪造“完整赛事对阵表”；
- **running**：比赛中。`starts_at`（或逐场 `scheduled_at`）到点自动开打；
- **rest**：阶段间休息（可换 Bot）。`rest_ends_at` 到点自动恢复；
- **finished**：末阶段完成。

> **组织者手动控制**：到点自动推进之外，组织者始终可手动操作——开放报名（draft→open）、截止报名出排期（→published）、立即开赛（→running）、结束休息进下一阶段。**running/rest 态的「强制结束赛事」仅用于恢复性收尾**：全部对阵都已完成或中止、但自动推进未收敛时才可固化正式名次；平台不会以此按钮作废仍在运行或尚未开始的对阵。

**Bot 换人时机**：选手可在**开赛前**（draft/open/published，已报名者可改派 Bot）和**中场休息**（rest，受 `allow_bot_swap_in_rest` 控制）更换派遣 Bot；但 pairing 一经发布，其 Bot、版本与 seed 均已冻结，published/rest 的换人只影响尚未发布的后续轮次或阶段，不会回写当前排期。**比赛中（running）不可换**（保证公平）。

**时间调度器**后台周期扫描赛事的 `registration_opens_at` / `registration_closes_at` / `starts_at` / `rest_ends_at` / `scheduled_at` 字段，到点自动推进阶段。组织者手动按钮（`/open` `/publish` `/start` `/resume` `/advance`）始终可用——到点自动 + 手动可提前。

赛事时间始终满足 `registration_opens_at ≤ registration_closes_at ≤ starts_at`；三个时刻允许相同（例如组织者立即开放、截止并开赛）。手动早于计划执行时，平台会把相应字段记录为实际推进时刻，不会保留“报名截止晚于比赛开始”的倒挂时间。管理员只修改其中一个时间时，平台也会合并其余已有时间后整体验证，非法修改不会部分保存。

> **逐场排期**：每场对阵（pairing）有独立的 `scheduled_at`。阶段可配 `round_stagger_minutes`（轮次间错峰分钟数）；`scheduled_at` 到点才 dispatch。**排期在「对阵」Tab 的赛程表（ScheduleTable）里查看**。

### 阶段类型（6 种）

| type | 说明 | 专属字段 |
|------|------|----------|
| `round_robin` | 单循环 | — |
| `double_round_robin` | 双循环（先后手对调） | — |
| `group_round_robin` | 蛇形种子分组单循环 | `group_count`、`advance_per_group` |
| `group_double_round_robin` | 分组双循环 | `group_count`、`advance_per_group` |
| `swiss` | 瑞士轮（积分就近配对，避开重复） | `rounds`、`advance_count` |
| `single_elimination` | 单败淘汰（种子位 + bye） | — |

计分：`poker_3_1_0`（胜/平/负 = 3/1/0，扑克）或 `ccgc_2_1_0`（2/1/0，棋类）。通用字段：`key`（阶段标识）、`advance_count`（晋级总人数）、`rest_after_minutes`（阶段后休息分钟）、`allow_bot_swap_in_rest`（休息期可否换 Bot）。`rounds=0`（swiss）= `ceil(log2(n))` 自动确定轮数。

### 规则参数钉死

各游戏规则参数已**钉死固定值，不可配置**：

| game_id | 固定规则 |
|---------|----------|
| holdem | 70 手（`DEFAULT_HANDS=70`） |
| gomoku | 15×15（`BOARD_SIZE=15`） |
| pencil | 6 点（`DEFAULT_N=6`，25 格） |

### 赛程合理性（人数 → 场次 → 推荐）

| 赛制 | 场次公式 |
|------|---------|
| 单循环 | `n*(n-1)/2` |
| 双循环 | `n*(n-1)` |
| 分组循环 | `Σ 每组 n_i*(n_i-1)/2` |
| 瑞士 | `rounds*(n//2)`，`rounds=ceil(log2(n))` |
| 单败淘汰 | `n-1` |

| 人数 | 推荐 | 说明 |
|------|------|------|
| ≤12 | 单/双循环 | 全员互打最公平 |
| 13~32 | 瑞士→淘汰 | swiss 定排名再单败决冠军 |
| 33~128 | 瑞士→淘汰（`advance_count` 控晋级） | 如 64 人 = 6 轮 192 场 + 淘汰 |
| 129~512 | 瑞士→淘汰 | swiss 唯一可行循环式 |

关键约束：循环人数上限默认 12（admin 可调，超限提示改 Swiss）；瑞士 = `ceil(log2(n))` 轮；单败 = n-1 场（最快但运气成分大）。

### 默认内置模板（10 个）

| id | 游戏 | 赛制 |
|----|------|------|
| `holdem_swiss_ko` | 德州 | 瑞士 → 单败（默认） |
| `holdem_rr` | 德州 | 单循环（小规模） |
| `holdem_prelim_swiss` | 德州 | 单阶段瑞士（全员排名，预赛） |
| `holdem_final_ranked` | 德州 | 全员单循环 → Top8 双循环（决赛） |
| `holdem_dup_rr` | 德州 | 复式单循环（同副牌交换座位，2 leg） |
| `gomoku_group_drr_ko` | 五子棋 | 分组双循环 → 单败（默认） |
| `gomoku_swiss_ko` | 五子棋 | 瑞士 → 单败（默认） |
| `board_rr` | 棋类 | 双循环（课堂演示） |
| `pencil_group_drr_ko` | 点格棋 | 分组双循环 → 单败（默认） |
| `pencil_swiss_ko` | 点格棋 | 瑞士 → 单败（默认） |

> 预赛 / 决赛：模板可声明赛事阶段（`preliminary` / `final` / `standalone`），阶段结束计算**全员正式名次**（破同分：积分 → Buchholz Cut1 → Sonneborn-Berger → 直接交手 → 净码）。`holdem_final_ranked`：Stage1 全员单循环；Stage2 把 Top8 循环结果嵌回总榜。

### 对阵图与显示

赛事详情页（`/contests/:id`）的报名列表、积分榜、对阵均显示 **Bot 名 / 用户名**（替换裸 `#ID`），按赛制类型分两种展示：

- **淘汰赛**：树状对阵图（`BracketTree`）——按 `bracket_slot` 排列，每轮一列，胜者高亮、负者灰色划线，横向滚动 + 轮次折叠（大规模可折叠到关注轮）。
- **瑞士 / 循环 / 分组**：按轮次（或分组）折叠的列表——大规模（>60 场或 >6 组）默认收起，展开看明细。

`GET /api/contests/{id}/bracket`（公开）：对阵图聚合数据（含 `stage_idx/round_num/group_id/bracket_slot/match_winner`）。

---

## Bot 详情页

每个 Bot 都有独立详情页 `/bot/:id`，从排行榜、首页最新对局、对局历史等任何出现 Bot 名的地方点击进入。

- **顶部信息卡**：Bot 显示名 / @用户名、游戏标签、简介、所有者（链接用户主页）、版本号、平台（format/os-arch）、创建时间、停用状态。
- **核心指标（4 卡）**：Rating（Glicko-2，含 rd 不确定度）/ 胜率（`(胜+平×0.5)/总`，标注总场）/ 胜 / 负·平。
- **三个 Tab**：
  1. **对局历史**：最近 30 场（时间、对手名→对手 Bot 详情、胜负彩色标记、对局类型、回放链接）。
  2. **对手战绩**：对各对手的胜负表（按交手次数倒序）。
  3. **评分曲线**：Glicko-2 评分随时间变化的折线图。

| 端点 | 说明 |
|------|------|
| `GET /api/bots/{id}/profile` | Bot 档案 + owner + rating + 胜率 |
| `GET /api/bots/{id}/matches?limit=&offset=` | 对局历史（含双方 bot 名） |
| `GET /api/bots/{id}/opponents?limit=` | 对各对手战绩 |
| `GET /api/bots/{id}/rating-history?limit=` | 评分时序（画曲线） |

> 对手战绩的胜负计数在 challenge / ladder 完成时累积（contest 与 human 不更新评分，故不计入）。

---

## 用户主页与搜索

### 用户主页 `/user/:name`

展示某用户的公开档案与战绩，从排行榜、Bot 详情页（owner）、对局列表等任何出现用户名的地方点击进入。

- 头像（无头像显示首字母占位）、显示名 / @用户名 / 角色徽章（管理员/组织者）、简介、注册时间、参与对局总数。
- 总战绩卡片：总胜率、胜场、负/平场、Bot 数。
- Bot 列表（卡片网格，链接 Bot 详情）。
- 查看自己主页时显示「编辑资料」按钮（链接 `/settings`）。

| 端点 | 说明 |
|------|------|
| `GET /api/users/{username}/profile` | 公开档案 + 总战绩聚合 |
| `GET /api/users/{username}/bots` | 该用户公开 Bot 列表 |

### 全局搜索 `/search`

顶栏搜索框 + 独立搜索页，三 tab：**用户**（`/api/users`）、**Bot**（`/api/search?q=&type=bots`，含 owner 名 + rating）、**对局**（`/api/search?q=&type=matches`，按 bot 名搜已完成对局）。

### 资料编辑

| 端点 | 鉴权 | 说明 |
|------|------|------|
| `PUT /api/auth/profile` | require_user | 更新 display_name（≤64）/ bio（≤500） |
| `POST /api/auth/avatar` | require_user | 上传头像（png/jpeg/webp/gif，≤2MB，新覆盖旧） |

---

## 社交：关注与收藏

- **关注用户**：在用户主页（`/user/:name`）点「+ 关注 / 已关注」。被关注者收到 `followed` 通知。主页显示「关注 N / 粉丝 N」。
- **收藏 Bot**：在 Bot 详情页（`/bot/:id`）点「☆ 收藏 / ★ 已收藏（N）」。

| 端点 | 鉴权 | 说明 |
|------|------|------|
| `POST /api/users/{id}/follow` | require_user | 关注（不能关注自己；幂等；触发通知） |
| `DELETE /api/users/{id}/follow` | require_user | 取关 |
| `GET /api/users/{id}/follow-status` | require_user | 是否关注 + follower/following 数 |
| `GET /api/users/{id}/followers` | 公开 | 粉丝列表 |
| `GET /api/users/{id}/following` | 公开 | 关注列表 |
| `POST /api/bots/{id}/favorite` | require_user | 收藏 Bot（幂等） |
| `DELETE /api/bots/{id}/favorite` | require_user | 取消收藏 |
| `GET /api/bots/{id}/favorite-status` | require_user | 是否收藏 + 收藏数 |
| `GET /api/auth/me/favorites` | require_user | 我的收藏列表 |

---

## 评论与点赞

对局与 Bot 详情页支持评论与点赞；首页展示「热门对局」点赞榜。

- **评论**：在 MatchDetail（`/match/:id`）与 BotDetail（`/bot/:id`）底部发表；作者/admin 可删除。评论触发 target owner 的 `comment` 通知。
- **点赞**：评论区头部 ♥ 按钮（幂等）。
- **浏览计数**：打开 MatchDetail 时 +1 `views_count`。
- **点赞榜**：首页「热门对局」展示 `likes_count > 0` 的对局（按点赞倒序）。

端点：`GET /api/comments`、`POST /api/comments`（触发通知）、`DELETE /api/comments/{id}`（作者/admin）；`POST/DELETE /api/likes`、`GET /api/likes/status`；`POST /api/matches/{id}/view`；`GET /api/matches/liked-top`。

---

## 通知系统

站内通知 + 可选邮件提醒。对局完成、被关注、赛事阶段变化、被评论等事件会生成通知，顶栏铃铛实时显示未读数。

| type | 触发场景 | 邮件 pref 字段 |
|------|----------|----------------|
| `match_done` | challenge/table/ladder 对局完成（contest 内部不通知） | `email_match_done` |
| `followed` | 被其他用户关注 | `email_followed` |
| `contest` | 赛事阶段变化 | `email_contest` |
| `comment` | Bot/对局被评论 | `email_comment` |

- **顶栏铃铛**：未读红点 + 下拉最近 10 条 + 「全部已读」+ 链通知列表页；定时轮询未读数。
- **通知列表页** `/notifications`：全部/未读筛选、单条已读、全部已读。
- 邮件提醒默认关闭，可在「设置 → 通知偏好」按类型开启（发送失败不影响站内通知）。

| 端点 | 说明 |
|------|------|
| `GET /api/notifications?unread_only=&limit=&offset=` | 通知列表 + 未读数 |
| `GET /api/notifications/unread-count` | 未读数（铃铛轮询） |
| `POST /api/notifications/read` `{id}` | 单条已读 |
| `POST /api/notifications/read-all` | 全部已读 |
| `GET/PUT /api/notification-prefs` | 通知/邮件偏好 |

---

## 设置与 MyBots 管理

### 个人设置 `/settings`

顶栏用户名处进入（或用户主页「编辑资料」）。四个 tab：

- **资料**：头像上传（≤2MB）、显示名（≤64）、简介（≤500）。
- **密码**：修改密码（旧密码 + 新密码 ≥8）。改后清除所有会话，需重新登录。
- **通知偏好**：4 个邮件提醒开关（对局完成/被关注/赛事/被评论）。
- **我的收藏**：收藏的 Bot 列表。

### MyBots 管理 `/my-bots`

每个 Bot 卡片可：**启用/停用**、**版本管理**（上传新版本 / 查看历史 / 回滚）、**编辑**（改 display_name/description）、**删除**（软删，历史对局保留）。Bot 名链接 Bot 详情；卡片显示当前 **Botzone 运行模式**徽章 + 版本号。

#### 上传 Bot / 运行模式

上传时须选择**游戏类型** + **Botzone 运行模式**：

- **LongRunning（长驻，可选）**：进程整场不重启；首回合发完整历史信封，Bot 响应后输出 `>>>BOTZONE_REQUEST_KEEP_RUNNING<<<` 握手，之后每回合只发单 request。适合有昂贵初始化的 Bot。平台默认仍是 Traditional。
- **Traditional（传统）**：每回合发完整历史信封 `{"requests":[...],"responses":[...]}`，Bot 自重放重建状态。适合无状态、易调试的 Bot。

详见 [协议规范](#/wiki?slug=protocol)。

#### 版本管理（1 Bot 1 游戏，多版本）

一个 Bot 可上传多个版本，随时切换激活（回滚）：`POST /api/bots/{id}/versions`（新版本成为当前）、`GET /api/bots/{id}/versions`（owner/admin 查历史）、`POST /api/bots/{id}/versions/{v}/activate`（回滚，恢复该版本运行模式）。赛事在生成并发布每轮对阵时冻结各 Bot 当时的激活版本，并把版本 ID 一直传到实际对局；普通挑战、自动天梯和人类对战也在创建对局时冻结当时的激活版本。因此，对局进入队列后再上传或回滚不会改变该局实际运行的程序。

| 端点 | 说明 |
|------|------|
| `POST /api/bots` | 上传新 Bot（带 `runtime_mode` Form） |
| `PATCH /api/bots/{id}` | owner 改 display_name/description/is_active |
| `DELETE /api/bots/{id}` | owner 软删 |
| `POST /api/bots/{id}/versions` | owner 上传新版本 |
| `GET /api/bots/{id}/versions` | owner/admin 查版本历史 |
| `POST /api/bots/{id}/versions/{v}/activate` | owner 回滚到指定版本 |
