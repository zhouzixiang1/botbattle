# 德州扑克 (TexasHoldem2p)

对齐 [Botzone · TexasHoldem2p](https://wiki.botzone.org.cn/index.php?title=TexasHoldem2p)。  
本平台 `game_id`：**`holdem`**。

> 下文 **§1–§4** 的标题层级、内容要素与 Botzone 原页一致（简介 / 游戏规则 / 交互 / 样例）。  
> **§5 起** 为平台适配（长驻行协议、与 Botzone 差异、裁判与样例路径）。权威协议字段见 [协议规范](#/wiki?slug=protocol)。

---

## 目录

1. [简介](#简介)
   - [人类玩家操作说明](#人类玩家操作说明)
   - [作者](#作者)
2. [游戏规则](#游戏规则)
   - [玩家位置与下注次序](#玩家位置与下注次序)
   - [牌局进行流程](#牌局进行流程)
   - [下注规则](#下注规则)
   - [牌型和大小](#牌型和大小)
   - [分数计算](#分数计算)
3. [游戏交互方式](#游戏交互方式)
   - [提示](#提示)
   - [具体交互内容](#具体交互内容)
   - [初始化数据](#初始化数据)
4. [游戏样例程序](#游戏样例程序)
   - [Python 版本](#python-版本botzone-协议)
5. [本平台适配](#本平台适配)
   - [与 Botzone 差异一览](#与-botzone-差异一览)
   - [本平台长驻行协议](#本平台长驻行协议)
   - [裁判与本地自测](#裁判与本地自测)
   - [赛制模板](#赛制模板)

---

## 简介

**德州扑克**（Texas hold'em）是世界上流行的一种扑克游戏，也是国际扑克比赛的正式竞赛项目之一。游戏用一副 52 张牌（不含 Joker），目标是在摊牌时自己的两张手牌大或等于其他所有未盖牌玩家，或是透过加注迫使其他玩家弃牌，赢家获得底池中的筹码。

一局德州扑克一般由 2～10 人组成，**本游戏为德州扑克的二人游戏版本**，且采用**不限注**的比赛方式。

比赛规定：

- 每人**初始筹码数 20000**
- **小盲注 50**，**大盲注 100**
- 比赛开始时，随机选定庄家位置；庄家的下家担任小盲注，庄家的下下家担任大盲注
- 每小局结束后庄家转移到下家

对于二人比赛版本，一场 Bot 比赛共包含 **50 小局**（Botzone 默认；本平台默认手数见 [§5](#与-botzone-差异一览)）。

正式比赛中，Bot 无法向用户存储空间中写入数据。

### 人类玩家操作说明

在自己的回合，可以点击按钮来选择**弃牌、全押、跟注/过牌、加注**这 4 种操作中的一种。

加注时，需要输入加注的筹码数量（注意是**额外增加**的筹码数量，**不是**要增加到的筹码数量）。加注筹码数需要**不少于本轮最大加注的 2 倍**，且**小于**自己剩余的筹码数。

若当前自己剩余的筹码数不足以跟注或加注，或者是对手全押，则只能选择弃牌或全押。

> 本平台人类对战界面同样提供 Fold / All-in / Call·Check / Raise；**本平台 Bot 协议完全照 Botzone**（raise response 的正整数 = 额外下注筹码 / 增量，与人类 UI / Botzone 一致），见 [§5](#本平台适配)。

### 作者

Botzone 原页：播放器、裁判程序、Python 样例程序：**dhbloo**。

本平台：裁判引擎、紧凑行协议与观赛 canvas 由 Botbattle 维护。

---

## 游戏规则

德州扑克的具体规则请参考 [德州扑克 · 维基百科](https://zh.wikipedia.org/zh-hans/%E5%BE%B7%E5%B7%9E%E6%92%B2%E5%85%8B)。

### 玩家位置与下注次序

从 0 号玩家开始按照顺时针方向围成一圈坐在一张桌子前。对于 N 个玩家的比赛，玩家 i 的下家为 `(i+1) % N`。

开始比赛时裁判随机选定一个玩家作为初始庄家，庄家的下家担任小盲注，庄家的下下家担任大盲注。每小局结束后庄家转移到当前庄家的下家。

> 二人时：`(dealer+1)%2` 为小盲，`(dealer+2)%2` 为大盲（大盲恰为庄家本人）。本平台 `d` 字段表示**本手小盲座位**（亦为按钮位），见协议。

### 牌局进行流程

一场比赛包括多个小局，每个小局为独立的手牌，并且每小局所有玩家**起始筹码固定为 20000**（每手复位，不跨手累积——对齐 Botzone）。每手结算的**净输赢累加**为整场累计净筹码，最终按累计净筹码判定胜负（净筹码高者胜，相等为平局）。

首先，被选定为小盲注和大盲注的玩家分别下小盲注（50）和大盲注（100）的筹码。之后每个玩家均被发到 **2 张底牌**，并进入叫注环节。

每小局包含 4 轮叫注：**翻牌前（Preflop）、翻牌（Flop）、转牌（Turn）、河牌（River）**。

1. **翻牌前（Preflop）**：从大盲注玩家的下家开始，按照顺时针方向进行下注。  
   （二人时即小盲先行动。）
2. **翻牌（Flop）**：翻开桌面上的 **3 张**公共牌，从庄家的下家（小盲）开始按照顺时针方向进行下注。如果小盲注已弃牌，则从小盲注的下家开始，依次类推。
3. **转牌（Turn）**：翻开桌面上的 **1 张**公共牌，从庄家的下家小盲注开始按照顺时针方向进行下注。
4. **河牌（River）**：翻开桌面上的 **1 张**公共牌，从庄家的下家小盲注开始按照顺时针方向进行下注。本轮结束后摊牌，所有在场玩家亮出手牌，比较大小，决定谁能赢得底池筹码。

每个阶段所有在场玩家都会进行至少一圈下注；如果已完成一圈下注的同时，所有在场玩家本轮均下了相同的筹码，则进入下一轮。

如果其他玩家弃牌到只剩一个在场玩家，则该玩家直接赢得底池筹码。如果在场玩家均选择全押（Allin），则直接进入摊牌阶段（发完剩余公共牌后比较）。

### 下注规则

每次下注时玩家均有 5 种可能的选择：弃牌（Fold）、跟注（Call）、过牌（Check）、加注（Raise）、全押（Allin）。

1. **弃牌（Fold）**：舍弃手中的牌不再下注，放弃已投入底池的筹码退出该局。
2. **跟注（Call）**：与上一位在场玩家下注相同的筹码。
3. **过牌（Check）**：如果本轮前面没有玩家下注，则可以选择不下注，并将下注机会交给下一位在场玩家。
4. **加注（Raise）**：下注**额外**的筹码，要求下注的筹码不少于本轮最大下注筹码的 **2 倍**。如果本轮前面没有玩家下注，则不少于**大盲注**。  
   （Bot 协议中 raise response 的正整数即此「额外筹码」，与 Botzone / 人类 UI 语义一致，见 §5。）
5. **全押（Allin）**：将手中的所有筹码全部下注。

跟注和加注时需要保证手上剩余的筹码**大于**所需下注额，否则只能选择弃牌或全押。**错误的下注为非法操作，会被视为弃牌。**

### 牌型和大小

德州扑克牌型大小依序为：

**同花顺 > 四条 > 葫芦 > 同花 > 顺子 > 三条 > 两对 > 对子 > 高牌。**

1. **同花顺（Straight Flush）**：五张同花色的连续数字牌。同时有同花顺时，数字最大者为赢家。公牌开出同花顺为最大时，则所有未盖牌的牌手平手平分底池。
2. **四条（Four of a Kind）**：四张相同数字的扑克牌，第五张是剩下牌组中最大的一张牌。若有一家以上持有四条，则比较第五张牌，最大者为赢家。公牌开出四条为最大时，则所有未盖牌的牌手平手平分底池。
3. **葫芦（Full House）**：三张相同数字及任何两张其他相同数字的扑克牌组成。如果同时有多人拿到葫芦，三张相同数字中数字较大者为赢家。五张牌数字都一样，则平分底池。
4. **同花（Flush）**：五张不按顺序但相同花色的扑克牌组成。如果不只一人有此牌组，则牌面数字最大的人赢得该局；如果最大数字相同，则由第二、第三、第四或者第五张牌来决定胜负。公牌的同花就是最大的同花牌型时，平分底池。
5. **顺子（Straight）**：五张连续数字扑克牌组成。如果不只一人有此牌组，则五张牌中数字最大的赢得此局。如果五张牌数字都相同，平分底池。（含 A-2-3-4-5 轮子。）
6. **三条（Three of a Kind）**：三张相同数字和两张不同数字的扑克牌组成。如果不只一人有此牌组，则三张牌中数字最大者赢得该局。如果五张牌数字都相同，则平分底池。
7. **两对（Two Pair）**：两对数字相同但两两不同的扑克和一张杂牌组成，共五张牌。如果不只一人持有此牌型，持有数字比较大的对子者为赢家；若较大数字对子相同，则比较小对子；如果两对对子数字都相同，那么第五张牌数字较大者赢；如果连第五张牌数字也相同，则平分底池。
8. **一对（One Pair）**：两张相同数字的扑克牌和另三张无法组成牌型的杂牌组成。如果不只一人持有此牌型，则持有较大数字对子者为赢家；如果对牌数字相同，则依序比较剩下的三张牌；如果五张牌都一样，则平分底池。
9. **高牌（High Card）**：无法组成以上任一牌型的杂牌。如果不只一人抓到此牌，则比较数字最大者；如果数字最大的相同，则依序比较第二、第三、第四和第五大的；如果五张牌都相同，则平分底池。

![德州扑克牌型示意](/wiki-assets/TexasHoldemHandType.jpg)

### 分数计算

比赛采用 **累积赢得筹码数量 ÷ 大盲注（100）** 作为最后的分数，并按照分数从高到低给玩家排名。

本平台天梯另用 **Glicko-2** 按对局结果更新 Bot 评分；展示用净筹码 / BB 与 Botzone 计分口径一致时可对照 `earnings` 字段。

---

## 游戏交互方式

### 提示

Botzone 上本游戏与其它游戏一样使用 [Bot 交互](https://wiki.botzone.org.cn/index.php?title=Bot) 模型，**只支持 JSON 交互**。

Botzone 默认：**请注意程序有计算时间限制，每步要在 1 秒内完成！**

本平台：整场**长驻**进程 + 一行 JSON；默认决策超时由管理员配置（常见默认 **60s**）。超时 / 非法动作 → **fold**；对局中途进程崩溃 / EOF → **计分判负**（`completed`）；启动失败见 [对局](#/wiki?slug=guide)。详见 [协议规范](#/wiki?slug=protocol)。

### 具体交互内容

在交互中，游戏中的所有牌使用 **0–51** 共 52 个正整数进行编号。对应关系如下：

| 牌号 | 牌面 | 牌号 | 牌面 | 牌号 | 牌面 | 牌号 | 牌面 |
|------|------|------|------|------|------|------|------|
| 0 | 红桃2 | 1 | 方块2 | 2 | 黑桃2 | 3 | 草花2 |
| 4 | 红桃3 | 5 | 方块3 | 6 | 黑桃3 | 7 | 草花3 |
| 8 | 红桃4 | 9 | 方块4 | 10 | 黑桃4 | 11 | 草花4 |
| 12 | 红桃5 | 13 | 方块5 | 14 | 黑桃5 | 15 | 草花5 |
| 16 | 红桃6 | 17 | 方块6 | 18 | 黑桃6 | 19 | 草花6 |
| 20 | 红桃7 | 21 | 方块7 | 22 | 黑桃7 | 23 | 草花7 |
| 24 | 红桃8 | 25 | 方块8 | 26 | 黑桃8 | 27 | 草花8 |
| 28 | 红桃9 | 29 | 方块9 | 30 | 黑桃9 | 31 | 草花9 |
| 32 | 红桃10 | 33 | 方块10 | 34 | 黑桃10 | 35 | 草花10 |
| 36 | 红桃J | 37 | 方块J | 38 | 黑桃J | 39 | 草花J |
| 40 | 红桃Q | 41 | 方块Q | 42 | 黑桃Q | 43 | 草花Q |
| 44 | 红桃K | 45 | 方块K | 46 | 黑桃K | 47 | 草花K |
| 48 | 红桃A | 49 | 方块A | 50 | 黑桃A | 51 | 草花A |

换算：

```text
suit  = card % 4     # 0 红桃, 1 方块, 2 黑桃, 3 草花
rank  = card // 4 + 2  # 2..14（A=14）
```

本平台协议中的 `mc` / `pc` 整数编码与上表**一致**（经 `protocol.encode_card` 映射）。

#### Request（Botzone）

每次下注操作只有一个 Bot 会收到 request。Bot 收到的 request 是一个 JSON 对象，表示当前牌局的信息和历史下注记录，以及比赛累积数据。格式如下：

```json
{
  "num_players": 2,
  "dealer_id": 0,
  "my_id": 1,
  "my_chips": 19800,
  "my_cards": [51, 23],
  "public_cards": [3, 8, 41],
  "history": [
    {
      "round": 0,
      "player_id": 1,
      "action": 0,
      "action_type": "call"
    },
    { "round": 0, "player_id": 0, "action": 0, "action_type": "check" },
    { "round": 1, "player_id": 1, "action": 0, "action_type": "check" },
    { "round": 1, "player_id": 0, "action": 100, "action_type": "raise" }
  ],
  "hand": 3,
  "max_hand": 50,
  "total_win_chips": [1200, -1200],
  "total_win_games": [2, 1]
}
```

| 字段 | 含义 |
|------|------|
| `num_players` | 玩家数量（本游戏固定 2） |
| `dealer_id` | 本局庄家 ID |
| `my_id` | 我的 ID |
| `my_chips` | 本局我的剩余筹码数 |
| `my_cards` | 本局我的底牌（0–51） |
| `public_cards` | 本局已翻出的公共牌 |
| `history[]` | 本局历史下注；`round`：0 preflop / 1 flop / 2 turn / 3 river；`action`：-1 fold / -2 allin / 0 call·check / &gt;0 raise **增量**；`action_type`：`fold` / `allin` / `call` / `check` / `raise` |
| `hand` | 本局是比赛中的第几局，**从 0 开始** |
| `max_hand` | 比赛一共有多少局 |
| `total_win_chips` | 每个玩家累积赢得筹码数 |
| `total_win_games` | 每个玩家累积赢得局数 |

#### Response（Botzone）

Bot 所需要输出的 response 是一个**整数**，表示自己要下注的选择。共有 4 种可能：

| 选择类型 | 对应 Response | 说明 |
|----------|---------------|------|
| 弃牌（Fold） | `-1` | 舍弃手中的牌不再下注。此操作后 Bot 本局不会再收到任何 request。 |
| 全押（Allin） | `-2` | 将手中的所有筹码全部投入底池。此操作后 Bot 本局不会再收到任何 request。 |
| 跟注/过牌（Call/Check） | `0` | 与上一位在场玩家下注相同的筹码；若本轮前面还没有玩家下注，则表示不下注。 |
| 加注（Raise） | **大于 0 的正整数** | 数字表示需要**额外**下注的筹码，要求不少于上一位在场玩家下注筹码的 2 倍，且小于自己剩余的筹码数。 |

选择跟注和加注时需要保证自己剩余的筹码数**大于**需要下注的筹码数。注意如果自己的剩余筹码数不足以跟注或加注，或是本轮有玩家全押，则只能选择弃牌或全押。

#### 非法操作（Botzone）

当 Bot 返回非法的 response 时，裁判程序会将其视为**弃牌**。包括：

1. Bot 在执行过程中崩溃或超时。
2. 返回的 response 不是整数，或者不是上述 4 种选择中的任意一种。
3. 返回的 response 是跟注操作，但是自己剩余筹码不大于需要跟注的筹码数。
4. 返回的 response 是加注操作，但是下注筹码少于本轮最大下注筹码的 2 倍；或者作为本轮首位加注玩家时，下注筹码少于大盲注；或者自己剩余筹码不大于需要跟注的筹码数。
5. 本局比赛有玩家选择全押，但是自己返回了除了弃牌或全押以外的其他操作。

### 初始化数据

在手动启动游戏时，可以给裁判程序传入如下 JSON 对象，控制比赛参数（Botzone）：

```json
{
  "max_hand": 50
}
```

`max_hand`：比赛总局数；双人模式默认为 **50**，六人模式默认为 18。

本平台手数**固定 70 手**（规则钉死，不可配），不经 Botzone 的 `initdata` 字段。

---

## 游戏样例程序

建议仔细阅读样例程序的注释。

### Python 版本（Botzone 协议）

以下为 Botzone 原页 Python 样例（Botzone 信封 + 整型 response）。**本平台完全兼容 Botzone 标准，此样例经少量适配（首回合响应后输出 `>>>BOTZONE_REQUEST_KEEP_RUNNING<<<` 握手即可长驻）即可直接上传**。详见 [§5](#本平台适配) 与仓库 `samples/`。

```python
import json
import random

N_PLAYERS = 2
SMALL_BLIND = 50
BIG_BLIND = 100

def card_suit(card): return card % 4
def card_number(card): return card // 4 + 2
def next_player(player, offset): return (player + offset) % N_PLAYERS

def get_action(input):
    N_PLAYERS = input["num_players"]  # 比赛中一共有多少玩家, 固定为2
    my_id = input["my_id"]  # 我的玩家ID, 0或1

    # ==============================================================================
    # 恢复当前牌面信息
    round = 0  # 当前轮次 0: preflop, 1: flop, 2: turn, 3: river
    round_bet = BIG_BLIND  # 当前轮次的最大下注
    round_raise = 2 * round_bet  # 当前轮次最小加注到的筹码数
    round_action_count = 0  # 当前轮次已经叫注的玩家数
    player_bets = [0] * N_PLAYERS  # 当前轮次每个玩家已经下注的筹码数
    player_bets[next_player(input["dealer_id"], 1)] = SMALL_BLIND
    player_bets[next_player(input["dealer_id"], 2)] = BIG_BLIND

    for h in input["history"]:
        id, action, type = h["player_id"], h["action"], h["action_type"]
        if type == "fold":
            player_bets[id] = -1  # 标记已弃牌
        elif type == "allin":
            round_bet = -2  # 标记本轮只能全押或弃牌
            player_bets[id] = -2  # 标记已全押
        elif type == "call" or type == "check":
            player_bets[id] = round_bet  # 跟注
            round_action_count += 1
            round_bet_left = [bet for bet in player_bets if bet >= 0]
            # 在场玩家均等注时进入下一轮
            if (round_action_count >= len(round_bet_left) and
                    round_bet_left.count(round_bet) == len(round_bet_left)):
                round += 1  # 轮次+1，进入下一轮
                round_bet = 0  # 重置当前轮次的最大下注
                round_raise = BIG_BLIND  # 每轮开始最小加注的筹码数均为大盲注
                round_action_count = 0
                player_bets = [0 if bet >= 0 else bet for bet in player_bets]
        elif type == "raise":
            player_bets[id] += action  # 加注（增量）
            round_bet = max(round_bet, player_bets[id])
            round_raise = max(round_raise, 2 * action)
            round_action_count += 1

    # ==============================================================================
    # 生成可能的动作: (动作, 概率)
    # 动作包括 -1: 弃牌, -2: 全押, 0: 过牌或跟注, >0: 加注的数量
    possible_actions = []
    possible_actions.append((-1, 0.15))  # 弃牌
    possible_actions.append((-2, 0.05))  # 全押

    if round_bet >= 0:
        if round_bet - player_bets[my_id] < input["my_chips"]:
            possible_actions.append((0, 0.6))  # 过牌或跟注
        if round_raise < input["my_chips"]:
            max_raise_amount = min(round_raise * 4, input["my_chips"] - 1)
            raise_amount = random.randint(round_raise, max_raise_amount)
            possible_actions.append((raise_amount, 0.2))  # 加注
    else:
        # 有人全押, 本轮只能全押或弃牌
        if (round == 0 and input["hand"] == input["max_hand"] - 1 and
                input["total_win_chips"][my_id] - player_bets[my_id] < 0):
            possible_actions.append((-2, 0.8))
        else:
            possible_actions.append((-1, 0.8))

    actions = [p[0] for p in possible_actions]
    weights = [p[1] for p in possible_actions]
    return random.choices(actions, weights)[0]

requests = json.loads(input())["requests"]
action = get_action(requests[-1])
print(json.dumps({"response": action}))
```

本平台可直接上传的样例见仓库：`samples/callbot.c`、`samples/callbot.py` 及 `samples/holdem_bots/`（8 种风格）等（Botzone 信封 + 裸整数 `{"response":0}` / `{"response":250}`）。

---

## 本平台适配

### 与 Botzone 对照（本平台完全遵循 Botzone 标准）

| 项 | Botzone TexasHoldem2p | 本平台 `holdem` |
|----|----------------------|-----------------|
| 进程模型 | Traditional 每回合启停 / LongRunning 长驻 | **整场长驻**（不每回合重启）；Botzone 标准 Bot 无需改动可直接跑 |
| 运行模式 | Traditional / LongRunning | 都支持（上传时标明） |
| 默认手数 | 50（`max_hand`） | **固定 70**（不可配；其余规则一致） |
| 每手筹码 | **每手固定 20000 复位** | **同左**：每手复位 20000，不跨手累积；按各手净输赢累加（累计净筹码）判定胜负 |
| 决策时限 | 约 1s/步 | 管理员可配（默认 60s） |
| Request | Botzone 信封 `{"requests":[...]}` / `{"request":...}` | **同左**（字段全名 `num_players`/`my_cards`/`history`/`total_win_chips`…，与 Botzone 一致） |
| Response | 信封 `{"response": <裸整数>}`；raise=**额外量（增量）** | **同左**（裸整数 `-1` fold / `-2` allin / `0` call-check / `>0` raise 额外量） |
| 非法操作 | 视为弃牌 | 同（fold） |
| 计分展示 | 累积赢码 / BB | 对局 `earnings` / 天梯 Glicko-2 |
| 人类 UI Raise | 额外增量 | 界面加注语义对齐人类说明；Bot 协议同 Botzone（raise=额外量） |

### 本平台协议（完全照 Botzone）

权威全文：[协议规范](#/wiki?slug=protocol)。摘要：

**请求信封（平台 → Bot，LongRunning 首回合 / Traditional 每回合）：**

```json
{"requests":[{"num_players":2,"dealer_id":0,"my_id":0,"my_chips":19950,"my_cards":[48,51],"public_cards":[],"history":[],"hand":0,"max_hand":70,"total_win_chips":[0,0],"total_win_games":[0,0]}],"responses":[]}
```

核心字段（全名，对齐 Botzone TexasHoldem2p 11 字段）：`num_players` / `dealer_id` / `my_id` / `my_chips` / `my_cards`(0-51) / `public_cards`(0-51) / `history`(对象数组) / `hand` / `max_hand` / `total_win_chips` / `total_win_games`。**不发送任何平台扩展字段**——需要跟注额/盲注/对手筹码的 Bot 从 `history` + `my_chips` 自行重放推导。

LongRunning 后续回合单 request：`{"request":{...}}`。

**响应信封（Bot → 平台）：`{"response": <裸整数>}`**

| response | 动作 |
|----------|------|
| `-1` | fold |
| `-2` | allin |
| `0` | call / check（平台按当前下注合法性自动判定为跟注或过牌） |
| `>0` | raise **额外下注筹码**（= 目标总额 − 本街已投筹码） |

```json
{"response":250}
```

最小可用策略（同 `samples/callbot.c`）：永远 `{"response":0}`（call/check）。

最小加注总额（引擎内部语义，平台校验用）：

```python
def min_raise_to(current_bet, bb):
    return bb if current_bet == 0 else current_bet * 2
```

LongRunning 握手：首回合响应后输出 `>>>BOTZONE_REQUEST_KEEP_RUNNING<<<` 声明长驻（否则每回合收到完整历史信封，Traditional 等效）。

### 裁判与本地自测

服务端裁判：从 2 张底牌 + 最多 5 张公共牌取**最佳五牌**比较；非法 / 超时 → fold；对局中途进程崩溃 → 计分判负（本手全筹码给对手后结束）；牌力顺序与 [§牌型和大小](#牌型和大小) 一致（含 A-5 轮子）。

本地自测：

- [`samples/judges/holdem_judge.py`](../samples/judges/holdem_judge.py) — 七牌最佳五牌、raise 合法性
- 裁判源码对全体玩家公开（见 [裁判](#裁判) / Wiki「裁判」页）

观赛 / 回放事件：`hand_start` / `deal_hole` / `deal_board` / `action` / `settle` / `match_end` 等，见 [对局](#/wiki?slug=guide)。

### 赛制模板

| template_id | 管线 |
|-------------|------|
| `holdem_rr` 等 | 见 [赛制模板](#/wiki?slug=guide) 与管理员 Templates |

---

## 参考

1. [Botzone · TexasHoldem2p](https://wiki.botzone.org.cn/index.php?title=TexasHoldem2p)（内容与章节对齐来源）
2. 本地快照：`refs/botzone/TexasHoldem2p.html`
3. 站内 [协议规范](#/wiki?slug=protocol)、[Bot 开发指南](#/wiki?slug=bot-dev)、[对局](#/wiki?slug=guide)
4. 参考裁判：[`samples/judges/`](../samples/judges/)
5. 牌型图：`/wiki-assets/TexasHoldemHandType.jpg`
