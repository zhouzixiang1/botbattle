# 德州扑克多策略样例 Bot

6 种不同策略的 holdem Bot（用于赛事功能验证：策略多样性让对局结果非全是平局，
能真实排名）。协议同 `samples/callbot.c`（紧凑 JSON 行：stdin 请求 / stdout `{"a":...,"x"?}` 响应）。

| Bot | 策略 | 预期表现 |
|-----|------|---------|
| `foldbot` | 永远弃牌 | 最弱，几乎必输（赛制排名兜底验证用） |
| `callbot` | 永远跟注/过牌 | 基线（在 `samples/callbot_linux_amd64`，被动） |
| `loosebot` | 散漫：几乎每手进池，偶尔小加注 | 长期小亏 |
| `tightbot` | 保守：只玩中等以上底牌(≥10)，翻后被动 | 中等偏稳 |
| `raisebot` | 永远最小加注 | 激进但可预测 |
| `randombot` | 随机合法动作 | 不可预测，波动大 |
| `allinbot` | 永远全押 | 极端激进，要么大赢要么大输 |

## 编译

```bash
bash samples/holdem_bots/gen.sh
```

产物：`foldbot/raisebot/allinbot/randombot/tightbot/loosebot`（linux-amd64 ELF）。

## 用途

- 赛事压测/可行性工具 `scripts/contest_stress.py`：seed 500 用户时按策略分布
  分配这些 Bot（如均匀 6 种），让赛制排名有意义（非全是平局）。
- 手动上传：`POST /api/bots` 上传任一二进制即可参赛。

## 协议速查

请求（平台 → Bot）：`{"v":1,"t":"act","h":0,"H":70,"id":0,"mc":[36,48],"pc":[],"to":50,"c":19950,...}`
- `to`：跟注额（0=可 check）；`c`：己方剩余筹码；`mc`/`pc`：手牌/公共牌（card_int = rank*4 + judge_suit）

响应（Bot → 平台）：`{"a":"f|c|k|r|all","x"?}`
- `f`=弃牌 `c`=跟注 `k`=过牌 `r`=加注(`x`=raise-to-total) `all`=全押
