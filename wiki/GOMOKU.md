# 五子棋 (Gomoku)

对齐 [Botzone · Gomoku](https://wiki.botzone.org.cn/index.php?title=Gomoku) / [五子棋](https://wiki.botzone.org.cn/index.php?title=%E4%BA%94%E5%AD%90%E6%A3%8B)。  
本平台 `game_id`：**`gomoku`**。

## 规则

- 棋盘 **15×15**；坐标整数 `x,y`，范围 `[0,14]`，先 x 后 y，原点左上。
- **黑方先手**（本平台 seat `0`）。
- 横、竖、斜任一方向 **连续 ≥5** 同色即胜（**长连也算胜**）。
- **无禁手**（与 Renju-Official 不同）。
- 非法着（越界 / 占用）或超时 → **判负**。
- 棋盘下满且无人成五 → **平局**。

## Botzone JSON 协议

Request（对方落子）：

```json
{"x": Number, "y": Number}
```

黑方首手：`{"x": -1, "y": -1}`。

Response：

```json
{"x": Number, "y": Number}
```

Botzone 每回合启停，输入含完整 `requests` / `responses` 历史。状态恢复（C++ 样例逻辑）：

```cpp
int turnID = input["responses"].size();
for (int i = 0; i < turnID; i++) {
    placeAt(requests[i].x, requests[i].y);   // 对手历史
    placeAt(responses[i].x, responses[i].y); // 己方历史
}
placeAt(requests[turnID].x, requests[turnID].y); // 本回合最新对手着
```

`placeAt`：仅当 `x>=0 && y>=0` 才落子（跳过首手 `-1,-1`）。

## 本平台双模式行协议

整场对局进程不退出；裁判每步推送一行 Botzone 信封，Bot 回一行信封。

请求信封（Traditional 完整历史 / LongRunning 单 request）：

```json
{"request":{"x":7,"y":7,"me":0}}
```

| 字段 | 含义 |
|------|------|
| `x`,`y` | 对方上一手；黑方首手为 `-1,-1` |
| `me` | 本方座位 `0` 黑 / `1` 白 |

响应信封：

```json
{"response":{"x":7,"y":8}}
```

信封与 `{x,y}` 落子兼容 [Botzone](https://wiki.botzone.org.cn/index.php?title=Bot)，`me` 是本平台附加字段。平台默认 Traditional，LongRunning 需握手。详见 [协议规范](#/wiki?slug=protocol)。


## 事件（观赛 / 回放）

- `match_start`：`size`, `first`
- `turn` / `move`：落子
- `illegal`：非法着
- `match_end`：`winner`, `reason`（`five` / `draw` / `illegal` / `error`）

## 裁判判定逻辑

服务端裁判判定：15×15 内、该点为空才合法；非法着 / 超时判负。连五判定：

```python
_DIRS = ((1, 0), (0, 1), (1, 1), (1, -1))  # 横、竖、两斜

def check_win(board, x, y, player):
    for dx, dy in _DIRS:
        count = 1
        for sign in (1, -1):
            cx, cy = x + sign * dx, y + sign * dy
            while 0 <= cx < 15 and 0 <= cy < 15 and board[cx][cy] == player:
                count += 1; cx += sign * dx; cy += sign * dy
        if count >= 5:  # 连续 ≥5（含长连，无禁手）
            return True
    return False
```

> 可用 [`samples/judges/gomoku_judge.py`](../samples/judges/gomoku_judge.py) 在本地自测合法着 / 胜负（`--check` 交互逐手）。

## 样例 Bot

仓库：`samples/gomokubot.py`（随机空点）；裁判自测见 [`samples/judges/`](../samples/judges/)。

## 默认赛制模板

| template_id | 管线 |
|-------------|------|
| `gomoku_group_drr_ko` | 分组双循环 → rest → 单败 |
| `gomoku_swiss_ko` | 瑞士 → rest → 单败 |

评分：`ccgc_2_1_0`（胜 2 / 平 1 / 负 0）。

## 一手交换五子棋（Gomoku-Swap1）

对齐 Botzone 游戏「一手交换五子棋」——用一手交换削弱黑先优势：黑方下一手后，白方可选择交换（典型 Swap 变体）。

> **状态**：本地调研未下载到完整 Wiki 规则正文（`allpages` / 游戏列表仅有条目）。引擎**尚未**以独立 `game_id` 实现，当前请使用标准五子棋。规则正文补全后：协议在标准 Gomoku 上增加白方可选 `{"x":-1,"y":-1}` 换手；`game_id` 另议（如 `gomoku_swap1`）。Wiki 标题：[Gomoku-Swap1](https://wiki.botzone.org.cn/index.php?title=Gomoku-Swap1)。

## 参考

1. https://wiki.botzone.org.cn/index.php?title=Gomoku  
2. 本地：`refs/botzone/Gomoku.html`  
3. 相关：一手交换五子棋（Swap1）见上节
