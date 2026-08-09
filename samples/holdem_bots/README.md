# 德州扑克多策略样例 Bot（平台唯一 JSON 协议）

8 种不同策略的 holdem Bot（用于赛事功能验证：策略多样性让对局结果非全是平局，
能真实排名）。它们使用平台唯一的标准信封：Traditional 完整历史，或 LongRunning
首回合完整历史 + 精确 keep-running 握手 + 后续单 request；响应对象只能包含
`response`，其值为整数动作码（`-1` fold / `-2` allin / `0` call-check / `>0` raise 额外量），
牌编码 0-51（`%4` 花色 0♥1♦2♠3♣）。详见 [协议规范](../../wiki/PROTOCOL.md)。

| Bot | 策略 | 预期表现 |
|-----|------|---------|
| `foldbot` | 永远弃牌 | 最弱，几乎必输（赛制排名兜底验证用） |
| `allinbot` | 永远全押 | 极端激进，要么大赢要么大输 |
| `callbot` | 永远跟注/过牌 | 基线（在 `samples/callbot_linux_amd64`，被动） |
| `raisebot` | 永远最小加注 | 激进但可预测 |
| `randombot` | 随机合法动作 | 不可预测，波动大 |
| `tightbot` | 保守：只玩中等以上底牌(≥10)，翻后被动 | 中等偏稳 |
| `loosebot` | 散漫：几乎每手进池，偶尔小加注 | 长期小亏 |
| `aggressivebot` | 无人下注则加注，否则 call/check（在 `samples/aggressivebot_bin`） | 中等偏激进 |

## 编译

```bash
bash samples/holdem_bots/gen.sh
```

产物：
- 本目录 6 个：`foldbot/allinbot/raisebot/randombot/tightbot/loosebot`（linux-amd64 ELF）
- 顶层 2 个：`samples/callbot_linux_amd64`、`samples/aggressivebot_bin`

## 用途

- 赛事压测/可行性工具：seed 用户时按策略分布分配这些 Bot，让赛制排名有意义。
- 手动上传：`POST /api/bots` 上传任一二进制即可参赛。

## 协议速查

请求信封（平台 → Bot，LongRunning 首回合，固定 11 字段）：
```json
{"requests":[{"num_players":2,"dealer_id":0,"my_id":0,"my_chips":19950,"my_cards":[48,0],"public_cards":[],"history":[],"hand":0,"max_hand":70,"total_win_chips":[0,0],"total_win_games":[0,0]}],"responses":[]}
```
后续回合（LongRunning）单 request：`{"request":{...}}`

响应信封（Bot → 平台）：`{"response": <整数动作码>}`，顶层只允许该字段。
- `-1`=fold `-2`=allin `0`=call/check `>0`=raise **额外下注筹码**（= 目标总额 − 本街已投）

这些样例同时支持两种模式，并在首回合响应后输出 LongRunning 握手。本平台默认
Traditional（逐决策重启）；选择 LongRunning 时必须精确握手，随后改收单 request。
缺失或错误握手会作为协议故障终止，不会回退到另一种进程模型。
