# Botzone 标准对局协议

平台与你的 Bot 之间通过 **标准输入 / 标准输出** 的「单行 JSON」协议通信，**完全遵循 [Botzone](https://wiki.botzone.org.cn/index.php?title=Bot) 标准**：平台每个决策点向 Bot 的 stdin 写**一行** JSON 信封，Bot 从 stdout 回**一行** JSON 信封。

> 本页是协议的**权威规范**，所有字段与语义均与平台引擎实现逐一对应，并与 Botzone 官方文档对齐。Bot 开发入门请见 [Bot 开发指南](#/wiki?slug=bot-dev)。棋类协议见第 11 节。

## 1. 通信模型与运行模式

Botzone 有两种运行模式，**上传时标明你的 Bot 用哪一种**：

### Traditional（传统模式）
- 平台**每个决策点**都向 Bot 发送**完整历史信封** `{"requests":[...], "responses":[...], ...}`。
- Bot 自己重放 `requests[]` / `responses[]` 重建状态，最后一条 `requests[-1]` 是当前决策。
- Bot 回 `{"response": <决策>}`。
- 适合：无状态、易调试的 Bot（每次从历史重建）。

### LongRunning（长驻模式，平台默认）
- 进程**整场长驻**不重启。**首回合**仍发完整历史信封（同 Traditional）；Bot 响应后**额外输出一行握手串** `>>>BOTZONE_REQUEST_KEEP_RUNNING<<<`（前后各带换行）声明长驻。
- 之后每个决策点，平台只发**单条 request 信封** `{"request": <当前决策>}`（不再重放历史）——Bot 须自行在内存里维护状态。
- 适合：有昂贵初始化（如神经网络加载）的 Bot，避免每回合重启开销。
- 握手后 `data` / `globaldata` / `debug` 字段失效。

```
平台 ──stdin (一行 JSON 信封)──►  Bot
平台 ◄─stdout (一行 JSON 信封)──  Bot
        （LongRunning 首回合后多一行 >>>BOTZONE_REQUEST_KEEP_RUNNING<<<）
```

- **一行一条**：请求和响应都各占完整一行（以 `\n` 结尾）。响应**必须**以换行结尾并**立即 flush** stdout，否则平台会等到超时（60 秒）后判 fold。
- **紧凑格式**：字段间无多余空白（如 `{"response":0}`，不是 `{ "response": 0 }`）。你的响应不强制紧凑，但建议紧凑。
- **超时 / 非法响应**：每个决策限时 **60 秒**（管理员可配）。超时、输出非法 JSON 或非法动作，本手判 **fold**（弃牌，response `-1`）。**对局中途进程崩溃 / 主动 exit / EOF** 由引擎**计分判负**（对局 `completed`）；**启动失败**才可能 `aborted`（`bot_crashed`）或赛事 `technical_loss`，见 [对局](#/wiki?slug=guide)。

## 2. 信封格式

### 请求信封（平台 → Bot）

```json
{"requests":[<请求负载1>, <请求负载2>, ...], "responses":[<你过去的响应>, ...]}
```
- Traditional 每回合都这样；LongRunning **仅首回合**这样。
- `requests[]` 按时间顺序累积，最后一条是当前决策。
- LongRunning 后续回合改为单条：`{"request": <当前决策负载>}`。

### 响应信封（Bot → 平台）

```json
{"response": <裸整数>}
```
- `response` 是必填字段（德州扑克的决策，见第 3 节）。
- 可选 `debug`（写日志，≤1KB）、`data`（存到下一回合，≤100KB）、`globaldata`（跨对局持久，≤100KB）。LongRunning 握手后这些字段失效。

## 3. 德州扑克响应（裸整数）

德州扑克的 `response` 是一个**裸整数**：

| response | 动作 | 说明 |
|----------|------|------|
| `-1` | fold 弃牌 | 放弃本手 |
| `-2` | all-in 全押 | 把剩余筹码全部推入底池 |
| `0` | call / check | 与 Botzone 一致：`0` 是 call/check 歧义码——需要跟注时为跟注，无需跟注时为过牌（平台按合法性判定） |
| `>0` | raise 加注 | **额外下注的筹码**（raise delta，见下） |

### raise 的正整数是「额外下注筹码」，不是「加注到的总额」

`response > 0` 表示你**这一动作额外投入底池的筹码**（= 你想达到的下注总额 − 你本街已投入的筹码）。Botzone 文档原文：「需要额外下注的筹码」。

- 例：翻前你是 SB，已投小盲 50，大盲是 100，你想加注到总额 300。
  你本街已投 50，所以**额外量 = 300 − 50 = 250** → 回 `{"response":250}`（不是 300，也不是 200）。
- 平台会校验：换算后的下注总额必须 ≥ 最小加注额（见第 6 节），且 ≤ 你的全押额。若 ≥ 全押额，平台自动按 all-in 处理。
- 最小加注约束：下注的筹码**不少于本轮最大下注筹码的 2 倍**。

### 简易决策

最小可用 Bot 永远回 `{"response":0}` 即可（`0` 是 call/check 歧义码，平台按当前合法性自动判定为跟注或过牌）——这正是仓库 `samples/callbot.c` 的全部逻辑。需要更精细决策的 Bot 可从 `history`（本手动作序列）+ `my_chips` 重放出当前需跟注的额度，与 Botzone 标准模型完全一致（不再下发 `to_call` 等平台扩展字段）。

## 4. 请求负载字段（德州扑克）

每条 `requests[]` 元素 / `request` 是一个对象，字段名**严格对齐 Botzone TexasHoldem2p**（参考 [TexasHoldem2p](https://wiki.botzone.org.cn/index.php?title=TexasHoldem2p)）：

| 字段 | 类型 | 含义 |
|------|------|------|
| `num_players` | int | 玩家数，恒为 `2`（单挑） |
| `dealer_id` | int | 本手庄家（= SB / 按钮位）的座位号，每手交替 |
| `my_id` | int | 你的座位号（`0` 或 `1`） |
| `my_chips` | int | 你**当前剩余**的筹码（已扣除本手下注） |
| `my_cards` | int[2] | **你的手牌**，两张，0–51 整数编码（见第 5 节） |
| `public_cards` | int[] | 当前公共牌（翻前空 `[]`，翻牌 3 / 转牌 4 / 河牌 5） |
| `history` | obj[] | 本手到此为止的全部动作历史（见第 4.1 节） |
| `hand` | int | 当前手数，**0-based**（首手 `0`） |
| `max_hand` | int | 本对局总手数（固定 `70`，规则钉死不可配） |
| `total_win_chips` | int[2] | 双方**累计净筹码**（各手 deltas 之和） |
| `total_win_games` | int[2] | 双方累计赢手数 |

> **严格对齐 Botzone**：请求负载**恰好**这 11 个官方字段，不多发任何平台扩展字段（不再下发 `to_call` / `sb` / `bb` / `opp_chips` 等）——标准 Botzone Bot 可直接跑，需要跟注额/盲注等信息的 Bot 从 `history` + `my_chips` 自行重放推导（与 Botzone 标准模型一致）。

> **盲注已扣除**：请求到达时本手盲注已从 `my_chips` 扣除并进入底池。例如首手你是 SB，`my_chips` 会是 `19950`（20000−50）。

### 4.1 动作历史 `history`

`history` 是本手从盲注后到当前决策点的全部动作序列，按时间顺序，每条是一个**对象**：

```json
{"round": 0, "player_id": 1, "action": 250, "action_type": "raise"}
```

| 字段 | 含义 |
|------|------|
| `round` | 下注轮：`0`=preflop `1`=flop `2`=turn `3`=river |
| `player_id` | 执行动作的座位号 |
| `action` | 该动作的**裸整数**（同 response 语义：fold=`-1` allin=`-2` call/check=`0` raise=`额外下注筹码`） |
| `action_type` | 动作类型字符串：`"fold"` / `"allin"` / `"call"` / `"check"` / `"raise"` |

**示例**：翻前 SB（座位 0）加注到 300（已投 50，额外量 250），BB（座位 1）跟注：

```json
"history": [
  {"round":0,"player_id":0,"action":250,"action_type":"raise"},
  {"round":0,"player_id":1,"action":0,"action_type":"call"}
]
```

## 5. 卡牌编码（0–51，对齐 Botzone）

`my_cards` / `public_cards` 与 `history` 里的牌都是 **0–51 的整数**，编码方式与 Botzone 完全一致：

```
card_int = rank × 4 + suit
```

- **点数** `rank = card_int // 4 + 2`，对应 `2, 3, 4, ..., 14(A)`。
- **花色** `suit = card_int % 4`：`0`=♥红心 `1`=♦方块 `2`=♠黑桃 `3`=♣梅花。

| card_int | 牌 |
|----------|----|
| `0` | 2♥ |
| `1` | 2♦ |
| `2` | 2♠ |
| `3` | 2♣ |
| `48` | A♥ |
| `50` | A♠ |
| `51` | A♣ |

### C 语言解码片段

```c
static const char *RANK = "23456789TJQKA";
/* Botzone: card % 4 == 0 → ♥, 1 → ♦, 2 → ♠, 3 → ♣ */
int rank = card / 4;        /* 0..12 → '2'..'A' */
int suit = card % 4;        /* 0=♥ 1=♦ 2=♠ 3=♣ */
char rankCh = RANK[rank];
char suitCh = "hdsc"[suit]; /* h=♥ d=♦ s=♠ c=♣ */
```

## 6. 游戏规则

### 基本设置
- **单挑无限注德州扑克**（Heads-Up No-Limit Hold'em）。
- **固定 70 手**（规则钉死，不可配）；每手起始筹码 **20000**，**每手复位**（筹码不跨手累积，每手从 20000 重新开始）。
- 盲注 **50 / 100**。
- 盲注位（按钮位）每手交替：第 `hand` 手的 SB 座位 = `hand % 2`。

> 注：Botzone TexasHoldem2p 文档默认 50 手；本平台**固定 70 手**（`max_hand=70`），其余规则一致。

### 行动顺序
- **翻前**：SB（小盲，即按钮位）先行动。
- **翻后**（flop / turn / river）：BB（非按钮位）先行动。

### 加注规则
- 加注用「**额外下注筹码**」（raise delta）表示，平台内部换算成下注总额校验。
- **最小加注**：再加注时，下注总额必须 **≥ 上一次下注额的 2 倍**（恰好 2 倍也合法）。
  - 翻前面对大盲 100：最小可加注到总额 `200`（你是 SB 已投 50，额外量 `150`）。
  - 对手加注到 300 后：你最小可再加注到 `600`。
- 翻后无人下注时，最小「开注」额为一个大盲（100）。
- 若剩余筹码不足以达到最小加注额，允许做更小的全押（all-in），平台按 all-in 处理。

### 结算
- 一手在有人 fold 时立即结束（另一方赢得底池）；或到河牌后双方摊牌比大小。
- 牌型从大到小：同花顺 > 四条 > 葫芦 > 同花 > 顺子 > 三条 > 两对 > 一对 > 高牌。
- 70 手结束后，按**累计净筹码**（`total_win_chips`）判定对局胜负（高者胜，相等为平局）。

## 7. 一手的完整流程

以「SB 加注到 300、BB 跟注，翻牌双方 check，转牌 SB 下注 200、BB fold」为例（LongRunning 模式，仅展示 SB 视角的请求序列）：

```jsonc
// 首回合：完整历史信封（requests[] 含本手第 1 个决策）
{"requests":[{"num_players":2,"dealer_id":1,"my_id":1,"my_chips":19950,"my_cards":[44,50],"public_cards":[],"history":[],"hand":0,"max_hand":70,"total_win_chips":[0,0],"total_win_games":[0,0]}],"responses":[]}
// Bot 回 {"response":250}  → 额外量 250（加注到总额 300）
//   再回 >>>BOTZONE_REQUEST_KEEP_RUNNING<<< 声明长驻

// 后续回合：单 request 信封
{"request":{"num_players":2,"dealer_id":1,"my_id":1,"my_chips":19700,"my_cards":[44,50],"public_cards":[5,18,33],"history":[{"round":0,"player_id":1,"action":250,"action_type":"raise"},{"round":0,"player_id":0,"action":0,"action_type":"call"}],"hand":0,"max_hand":70,"total_win_chips":[0,0],"total_win_games":[0,0]}}
// Bot 回 {"response":0}  → check（翻牌）
```

> Traditional 模式下，每个决策点都收到完整 `requests[]` 累积历史，Bot 无状态地只看最后一条即可决策。

## 8. 常见错误与处理

| 情况 | 平台行为 |
|------|----------|
| 超过决策时限未响应 | 本手判 fold（response `-1`） |
| 输出非法 JSON | 本手判 fold |
| `response` 不是合法整数 / 缺失 | 本手判 fold |
| raise 的额外量换算后低于最小加注额 | 本手判 fold |
| 对局中途进程崩溃 / 主动退出 / EOF | **计分判负**（对局 `completed`，崩溃方负）；不再吞成默认 fold 继续 |
| 启动失败（session 起不来） | 非赛事 → `aborted`（`bot_crashed`）；赛事 → `completed` + `technical_loss` |

**写作建议**：始终保证输出是单行合法 JSON、以 `\n` 结尾并 flush；遇到无法解析的输入时，回一条最安全的 `{"response":-1}` 比让进程崩溃更稳妥。

## 9. 与 Botzone 官方对照

本平台德州扑克协议**完全遵循 Botzone TexasHoldem2p 标准**：

| 概念 | Botzone | 本平台 |
|------|---------|--------|
| 请求信封 | `{"requests":[...],"responses":[...]}` | 同左（LongRunning 后续 `{"request":...}`） |
| 响应 | 裸整数：`-1` fold / `-2` allin / `0` call-check / `>0` 加注**额外量** | 同左 |
| 运行模式 | Traditional / LongRunning | 都支持（上传标明） |
| 手数 | 默认 50 | **固定 70**（不可配，其余一致） |
| 筹码/盲注 | 20000，SB50/BB100 | 同左 |
| 牌编码 | 0–51（`%4` 花色 0♥1♦2♠3♣） | 同左 |
| 握手串 | `>>>BOTZONE_REQUEST_KEEP_RUNNING<<<` | 同左 |

**差异**：仅手数固定 70 vs 50（其余字段、计分、盲注、规则完全一致，标准 Botzone Bot 直接可跑）。资源与超时见 [Bot 开发指南](#/wiki?slug=bot-dev)。

## 10. 平台与 Botzone 的运行模型差异

本平台**完全兼容 Botzone 协议**：无论你的 Bot 使用 Traditional 还是 LongRunning 模式，**Botzone 标准 Bot 无需改动即可在本平台运行**。两种模式的输入输出信封格式（见第 1–2 节）与 Botzone 完全一致。

## 11. 棋类协议（Gomoku / Pencil）

通信模型与德州相同（Botzone 信封 + 单行 JSON，见第 1–2 节）。棋类 payload 是 Botzone 标准 `{x, y}` 落子格式，信封包裹（Traditional 完整历史 / LongRunning 单 request + keep_running 握手）与德州一致。

请求信封：`{"requests":[{x,y,...}]}` 或 LongRunning 后续 `{"request":{x,y,...}}`。
响应信封：`{"response": {"x": int, "y": int}}`。

### 五子棋（Gomoku）

请求 payload：`{"x": int, "y": int, "me": 0|1}`
- `x,y` = 对手最近一手（0-based 坐标，15×15 棋盘）。黑方（`me=0`）首手 `x=y=-1`（无上一手）。
- `me` = 本方座位（0=黑，1=白）。

响应：`{"response": {"x": <你的落子>, "y": <你的落子>}}`。详见 [五子棋](#/wiki?slug=gomoku)。非法着 / 超时 → **判负**。

### 点格棋（Pencil）

请求 payload：`{"x": int, "y": int, "pass": 0|1, "me": 0|1, "scores": [r, b]}`
- `x,y` = 对手最近落点（交错网格，红方 `me=0` 首手 `x=y=-1`）。
- `pass` = 对手是否得分连走（`pass=1` 时你必须响应 `{"response":{"x":-1,"y":-1}}` 把回合交还）。
- `scores` = [红方得分, 蓝方得分]。

响应：`{"response": {"x": <边的坐标>, "y": <边的坐标>}}`。详见 [点格棋](#/wiki?slug=pencil)。

