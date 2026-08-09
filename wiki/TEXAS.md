# 德州扑克

本平台 `game_id`：`holdem`。规则参数固定，Bot 通信使用
[统一信封](#/wiki?slug=protocol)。

## 1. 赛局参数

- 双人无限注德州扑克（Heads-Up No-Limit Hold'em）。
- 一场对局固定 **70 手**，不可通过上传参数或对局配置修改。
- 每手双方起始筹码固定 20000、小盲固定 50、大盲固定 100；每手重新开始。这些参数与 70 手均不可配置。
- 座位 `0` / `1` 轮流担任按钮位与小盲：第 `hand` 手的小盲为 `hand % 2`。
- 每手净输赢累加到 `total_win_chips`；70 手后累计净筹码高者赢，相同为平局。

## 2. 行动顺序

- 翻前：按钮位 / 小盲先行动。
- 翻牌、转牌、河牌：非按钮位 / 大盲先行动。
- 有人弃牌时本手立即结束；双方全押后发完公共牌并摊牌。

## 3. 动作码

Bot 必须输出完整响应对象，例如 `{"response":0}`。`response` 值的含义：

| 值 | 动作 |
|----|------|
| `-1` | fold |
| `-2` | all-in |
| `0` | call / check；裁判按当前状态判定 |
| `>0` | 本动作额外投入的筹码（raise delta） |

正数不是“加注到的总额”。例如本街已投 50、目标总下注 300，应返回
`{"response":250}`。

最小加注按目标总额校验：面对当前下注额时，新的总下注至少达到裁判要求的最小值；
筹码不足时可选择全押。响应格式正确但游戏内动作不合法时，由德州裁判按弃牌处理；
非法 JSON、错误信封或错误 response 类型属于协议技术负，不会伪装成弃牌继续比赛。

## 4. 手牌与牌型

每名玩家两张底牌，桌面最多五张公共牌，从可用七张牌中选择最佳五张。牌型从大到小：

同花顺 > 四条 > 葫芦 > 同花 > 顺子 > 三条 > 两对 > 一对 > 高牌。

支持 A-2-3-4-5 作为最小顺子。完全相同的最佳五张牌平分本手底池。

## 5. 请求 payload

每条 Holdem request 恰好包含 11 个字段：

```json
{"num_players":2,"dealer_id":0,"my_id":0,"my_chips":19950,"my_cards":[48,51],"public_cards":[],"history":[],"hand":0,"max_hand":70,"total_win_chips":[0,0],"total_win_games":[0,0]}
```

| 字段 | 含义 |
|------|------|
| `num_players` | 恒为 2 |
| `dealer_id` | 本手按钮位 / 小盲座位 |
| `my_id` | 本 Bot 座位 |
| `my_chips` | 当前手剩余筹码，盲注和已下注已扣除 |
| `my_cards` | 两张底牌，整数编码 0..51 |
| `public_cards` | 当前公共牌 |
| `history` | 当前手动作历史 |
| `hand` | 当前手序号 0..69 |
| `max_hand` | 恒为 70 |
| `total_win_chips` | 双方累计净筹码 |
| `total_win_games` | 双方累计赢手数 |

平台不发送跟注额、对手筹码等派生字段。Bot 从 `history` 与 `my_chips` 重放。

### history

```json
{"round":0,"player_id":0,"action":250,"action_type":"raise"}
```

- `round`：0 preflop、1 flop、2 turn、3 river。
- `player_id`：行动座位。
- `action`：与 response 动作码相同；raise 时为额外投入筹码。
- `action_type`：`fold` / `allin` / `call` / `check` / `raise`。

## 6. 卡牌编码

牌编码范围 0..51：

```text
card = (rank - 2) * 4 + suit
rank = card // 4 + 2
suit = card % 4
```

花色：`0` 红心、`1` 方块、`2` 黑桃、`3` 梅花。例如 `48` 是 A♥，`50` 是 A♠。

## 7. 快速开始

最简单的合法策略始终返回 `{"response":0}`，即在可过牌时过牌、需要跟注时跟注。
[Bot 开发指南](#/wiki?slug=bot-dev)提供可直接复制的完整 C / Python 程序，以及在 Windows、
Linux、macOS 上构建 Linux x86_64 ELF 的命令。上传时选择的运行模式必须与程序处理信封的
方式一致。
