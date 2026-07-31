# 德州扑克 (TexasHoldem2p)

对齐 [Botzone · TexasHoldem2p](https://wiki.botzone.org.cn/index.php?title=TexasHoldem2p)。  
本平台 `game_id`：**`holdem`**。

## Botzone 规则摘要

- 2 人无限注；52 张无 Joker。
- 每手起始筹码 **20000**（每手复位）；SB **50** / BB **100**。
- Botzone 默认约 **50** 手（`initdata.max_hand`）；本平台默认 **70** 手。
- 动作：Fold / Call / Check / Raise / Allin。
- Botzone Raise 为**额外加注额（增量）**；非法操作视为弃牌。
- 计分：累积赢得筹码 / BB(100)。

## Botzone 交互

- 仅 JSON；牌编码 **0–51**：`suit=card%4`，`rank=card//4+2`。
- Request 含 `my_cards`, `public_cards`, `history`, `hand`, `max_hand` 等。
- Response：**单个整数**：`-1` fold，`-2` allin，`0` call/check，`>0` raise 增量。

## 本平台协议

见 [协议规范](#/wiki?slug=protocol)。要点差异：

| 项 | Botzone | 本平台 |
|----|---------|--------|
| 进程 | 每回合启停 | 长驻行协议 |
| Response | 整型 | `{"a":"f\|c\|k\|r\|all","x"?}` |
| Raise | 增量 | **`x` = raise-to-total** |
| 默认手数 | 50 | 70 |

牌型图（已迁入）：见 `wiki/assets/TexasHoldemHandType.jpg`（若存在）。

## 裁判判定逻辑

服务端 `MatchSession` 判定：从 2 张底牌 + 最多 5 张公共牌中取**最佳五牌**比较；
非法动作视为 fold。牌力类别（高 → 低）：同花顺 > 四条 > 葫芦 > 同花 > 顺子 > 三条 > 两对 > 一对 > 高牌。
raise 最小总额（raise-to）= 2× 当前下注（首次 = 2bb）：

```python
# raise 下限（engine/game.py）
def min_raise_to(current_bet, bb):
    return bb if current_bet == 0 else current_bet * 2
```

> 可用 [`samples/judges/holdem_judge.py`](../samples/judges/holdem_judge.py) 在本地自测七牌最佳五牌评估、牌力比较与 raise 合法性。

## 参考

1. https://wiki.botzone.org.cn/index.php?title=TexasHoldem2p  
2. `refs/botzone/TexasHoldem2p.html`  
3. 站内 `PROTOCOL.md`  
4. 参考裁判：[`samples/judges/`](../samples/judges/)
