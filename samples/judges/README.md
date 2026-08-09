# 参考裁判（samples/judges）

Bot 作者可本地运行的**参考裁判**，逻辑与本平台服务端引擎一致，便于自测
合法着 / 胜负判定 / 手牌评估，**无需启动平台**。这些脚本独立、无平台依赖。

> 平台真正裁判在服务端 `bzplat/backend/games/<game>/engine.py`；此处仅作参考实现。

| 脚本 | 游戏 | 能力 |
|------|------|------|
| `gomoku_judge.py` | 五子棋 | 合法着校验、4 方向连五判定、棋谱回放 |
| `pencil_judge.py` | 点格棋 | 交错网格、占边合法校验、成格连走计分 |
| `holdem_judge.py` | 德州扑克 | 七牌最佳五牌评估、牌力比较、raise 下限合法性 |

## 用法

```bash
# 五子棋：内置演示（黑五连胜），或 --check 交互逐手判定
python samples/judges/gomoku_judge.py
python samples/judges/gomoku_judge.py --check

# 点格棋：N=2 成格演示，或 --check 交互占边
python samples/judges/pencil_judge.py
python samples/judges/pencil_judge.py --check

# 德州：手牌评估 + 动作合法性演示
python samples/judges/holdem_judge.py
```

裁判功能说明见 [平台功能指南 · 裁判](../../wiki/GUIDE.md)，完整规则见
[五子棋](../../wiki/GOMOKU.md)、[点格棋](../../wiki/PENCIL.md)、[德州](../../wiki/TEXAS.md)。
