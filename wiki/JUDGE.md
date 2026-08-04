# 裁判

对齐 [Botzone · 裁判](https://wiki.botzone.org.cn/index.php?title=%E8%A3%81%E5%88%A4) / Judge。

## 裁判概念

裁判（judge）负责：接收 bot 的着法、判定**合法性**、推进局面、判定**胜负**与**计分**。
任何非法着（越界、占用、非边、不符合规则）或决策超时 / 异常，裁判会给出明确判罚。

| 情况 | 扑克（holdem） | 棋类（gomoku / pencil） |
|------|----------------|------------------------|
| 非法着 | 视为 fold | **判负** |
| 决策超时 | 视为 fold | **判负** |
| 可恢复的决策异常（坏 JSON 等） | 视为 fold | **判负** |
| **对局中途**进程崩溃 / EOF（`BotCrashedError`） | **计分判负**（对局 `completed`，崩溃方负） | **同左**（对手胜，`reason=crash`） |
| **启动失败**（进程起不来） | 非赛事 → `aborted`（`bot_crashed`）；赛事 → `completed` + `technical_loss` | **同左** |
| 棋盘满 / 资源耗尽 | — | 平局（点数相同）或按点数判胜 |

## Botzone 裁判模型

Botzone 裁判每回合启停，输入大致为 `{"log":[...], "initdata":...}`：

- 继续对局：`{"command":"request","content":{"0": req, ...},"display":...}`
- 结束：`{"command":"finish","content":{"0": score, "1": score},"display":...}`
- `initdata`：首回合可写回种子等，之后固定

> Botzone Wiki 的 C++ 样例是 8×8 黑白棋（Reversi），非五子棋/点格棋。

## 本平台裁判

本平台**无独立「裁判程序」二进制**；由服务端 **`games/<game>/` 裁判模块**扮演裁判，经 `GameSpec` 注册表调度，进程内直接推进局面：

| 游戏 | 裁判模块（实现路径） |
|------|----------|
| 德州扑克（holdem） | `MatchSession`（`bzplat/backend/games/holdem/engine.py`） |
| 五子棋（gomoku） | `GomokuSession`（`bzplat/backend/games/gomoku/engine.py`） |
| 点格棋（pencil） | `PencilSession`（`bzplat/backend/games/pencil/engine.py`） |

> 真实现全在 `games/<game>/`。旧的 `bzplat/backend/engine/` 包已删除，不再存在 shim。

Bot 通过 Docker / 本地长驻进程交互：引擎调用 `decide(player_idx, request)` →
BinaryRunner 往 bot 的 stdin 写一行 JSON 请求、从 stdout 读一行 JSON 响应。

## 裁判源码公开可查

裁判是**公开可审计的规则定义**——区别于 Bot 的私有黑盒二进制（保护玩家智力成果），
裁判源码对**全体玩家透明**。任何访客（无需登录）都可在网页「裁判」页查看每款游戏
裁判引擎（`engine.py`）、行协议（`protocol.py`）、结果契约（`result.py`）的完整明文源码：

- 网页：顶部导航「裁判」页（`/judges`）
- API：`GET /api/judges`（裁判列表）、`GET /api/judges/{game_id}/source`（源码全文）

规则透明是平台公正性的基础——玩家可核对每一手判罚是否符合代码、验证裁判无可利用漏洞。

## 参考裁判（可本地自测）

仓库提供**独立、无平台依赖**的参考裁判脚本，Bot 作者可在本地直接运行，
自测合法着 / 胜负 / 计分，逻辑与服务端引擎一致：

| 脚本 | 游戏 | 能力 |
|------|------|------|
| [`samples/judges/gomoku_judge.py`](../samples/judges/gomoku_judge.py) | 五子棋 | 合法着、4 方向连五、棋谱回放 |
| [`samples/judges/pencil_judge.py`](../samples/judges/pencil_judge.py) | 点格棋 | 交错网格、占边、成格连走计分 |
| [`samples/judges/holdem_judge.py`](../samples/judges/holdem_judge.py) | 德州扑克 | 七牌最佳五牌评估、raise 下限 |

```bash
python samples/judges/gomoku_judge.py          # 内置演示
python samples/judges/gomoku_judge.py --check  # 交互逐手判定
```

## 各游戏核心判定逻辑（代码片段）

### 五子棋：连五判定

以刚落的子 `(x,y)` 为中心，4 个方向（横、竖、两斜）任一连续 ≥5 同色即胜（含长连，无禁手）：

```python
_DIRS = ((1, 0), (0, 1), (1, 1), (1, -1))

def check_win(board, x, y, player):
    for dx, dy in _DIRS:
        count = 1
        for sign in (1, -1):
            cx, cy = x + sign * dx, y + sign * dy
            while 0 <= cx < 15 and 0 <= cy < 15 and board[cx][cy] == player:
                count += 1
                cx += sign * dx; cy += sign * dy
        if count >= 5:
            return True
    return False
```

合法着 = 在 15×15 内且该点为空；非法着判负。

### 点格棋：成格判定

交错网格 `size = 2N-1`：偶偶=点、奇偶/偶奇=边、奇奇=格心。占边后检查相邻格心四边是否全占：

```python
def _box_completed(board, bx, by):
    for dx, dy in ((-1,0),(1,0),(0,-1),(0,1)):
        ex, ey = bx + dx, by + dy
        if board[ex][ey] != GRID_EDGE_USED:
            return False
    return True
```

成格 → 当前玩家得分并**连走**（对方须回 `{"x":-1,"y":-1}` 的 pass）。

### 德州扑克：手牌评估与 raise 下限

七牌取最佳五牌（类别：高牌 < 一对 < 两对 < 三条 < 顺子 < 同花 < 葫芦 < 四条 < 同花顺）；
raise 最小总额 = 2× 当前下注（首次 = 2bb）：

```python
# raise 下限（逻辑示意；实现见 games/holdem/engine.py）
def min_raise_to(current_bet, bb):
    return bb if current_bet == 0 else current_bet * 2
```

详见 [运行时](#/wiki?slug=runtime)、[协议](#/wiki?slug=protocol)、各游戏 wiki 页。
