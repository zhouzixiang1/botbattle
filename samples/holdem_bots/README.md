# 德州扑克多策略样例 Bot（Botzone 标准协议）

8 种不同策略的 holdem Bot（用于赛事功能验证：策略多样性让对局结果非全是平局，
能真实排名）。它们使用兼容 [Botzone](https://wiki.botzone.org.cn/index.php?title=Bot) 的核心协议：
Botzone 信封（Traditional 完整历史 / LongRunning 单 request + keep_running 握手），
德州 response 裸整数（`-1` fold / `-2` allin / `0` call-check / `>0` raise 额外量），
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

- 生产 Bot 迁移 `scripts/migrate_bots_to_botzone.py`：把旧协议 holdem Bot 批量替换为
  Botzone 协议样例（8 种风格随机分布，确定性 seed 可复现）。
- 赛事压测/可行性工具：seed 用户时按策略分布分配这些 Bot，让赛制排名有意义。
- 手动上传：`POST /api/bots` 上传任一二进制即可参赛。

## 协议速查

请求信封（平台 → Bot，LongRunning 首回合，严格对齐 Botzone TexasHoldem2p 11 字段）：
```json
{"requests":[{"num_players":2,"dealer_id":0,"my_id":0,"my_chips":19950,"my_cards":[48,0],"public_cards":[],"history":[],"hand":0,"max_hand":70,"total_win_chips":[0,0],"total_win_games":[0,0]}],"responses":[]}
```
后续回合（LongRunning）单 request：`{"request":{...}}`

响应信封（Bot → 平台）：`{"response": <裸整数>}`
- `-1`=fold `-2`=allin `0`=call/check `>0`=raise **额外下注筹码**（= 目标总额 − 本街已投）

这些样例同时支持两种模式，并在首回合响应后输出 LongRunning 握手。本平台默认
Traditional（逐决策重启）；选择 LongRunning 时握手后改收单 request。未握手的兼容
回退是在同一进程发送完整历史，不等于 Traditional 重启。
