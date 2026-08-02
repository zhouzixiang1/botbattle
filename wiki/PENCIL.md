# 点格棋 (Pencil)

对齐 [Botzone · Pencil](https://botzone.org.cn/game/Pencil) / [Wiki · Pencil](https://wiki.botzone.org.cn/index.php?title=Pencil)。  
本平台 `game_id`：**`pencil`**（显示名「点格棋」）。裁判引擎 **已注册**。

## 规则

1. 棋盘由 **N×N 个点**构成；默认 **N=6**（对齐 Botzone 官方裁判 `grid_size=11` 交错维度 → 6 点 → 交错板 `size=2N-1=11` → `(N-1)²=25` 格，奇数无平局）。N 可调（3–15）。
2. 坐标从 0 开始，**先 x 后 y**，原点左上。
3. **红方先手**（seat `0`）；轮流占横/竖相邻两点之间的边（不可越点、不可重边）。
4. 某格四边占满 → **最后占边者得该格**（格属占格者），并 **连走**；未得分则换手。
5. **多数胜**：先占领多数格（`⌈boxes/2⌉`，N=6 时为 13）的一方立即获胜（对齐裁判 `hasPlayerWon`）；全部占完则格多者胜。
6. 非法着 / 超时 / 崩溃 → **对手 2-0 胜**（scores 归一化，对齐裁判）。

## 交错网格模型（实现必读）

将点、边、格放入边长 `size = 2*N - 1` 的交错板（N=6 → **11×11**，对齐 Botzone 裁判 `grid_size=11`）：

| 条件 | 类型 |
|------|------|
| `x%2==0 && y%2==0` | 点 |
| `(x+y)%2==1` | 边（可占） |
| `x%2==1 && y%2==1` | 格子中心 |

合法着：该格当前为「未占边」。占边后检查四邻格心是否四边已满。

> **对齐权威裁判**：Botzone 官方 C++ 裁判的 `grid_size=11` 即此交错维度（6 点 → 25 格）。本平台此前误用 N=11 点（21×21 交错 / 100 格），已纠正为 N=6 点对齐裁判。

## Botzone JSON 协议

Request（对方刚下的边）：

```json
{"x": Number, "y": Number, "pass": Number}
```

- `pass==1`：对方刚得分还要连走 → 本方必须输出 `{"x":-1,"y":-1}`。
- `pass==0`：轮到本方真实占边。
- 红方首手：`{"x":-1,"y":-1,"pass":0}`。

Response：

```json
{"x": Number, "y": Number}
```

### 状态恢复（Python 样例逻辑）

```python
for i in range(len(all_requests) - 1):
    curr_player = 1 - curr_player
    if requests[i].x != -1:
        do_action(requests[i].x, requests[i].y)
    curr_player = 1 - curr_player
    if responses[i].x != -1:
        do_action(responses[i].x, responses[i].y)

lat_req = all_requests[-1]
curr_player = 1 - curr_player
if lat_req["x"] != -1:
    opp_should_continue = do_action(lat_req["x"], lat_req["y"])
curr_player = 1 - curr_player
if lat_req["pass"] == 1:
    resp = (-1, -1)
else:
    resp = choose_legal_edge()
```

## 本平台长驻行协议

请求：

```json
{"v":1,"t":"mv","x":3,"y":4,"pass":0,"me":0,"scores":[1,0]}
```

| 字段 | 含义 |
|------|------|
| `x`,`y` | 对方上一手边坐标；首手 / 连走后通知为 `-1,-1` |
| `pass` | `1` 时必须响应 `{"x":-1,"y":-1}` |
| `me` | 本方座位 |
| `scores` | 当前红/蓝得分 |

响应：`{"x":5,"y":4}`；pass 回合：`{"x":-1,"y":-1}`。

### 连走时序（与 Botzone 对齐）

1. A 占边并得分 → 向 B 发送该边且 `pass=1`，B 回 `-1,-1`。  
2. 再向 A 发送 `x=y=-1, pass=0`，A 继续占边。  
3. 未得分则直接换手，`pass=0`。

## 裁判判定逻辑

服务端 `PencilSession` 判定（对齐 Botzone 官方 C++ 裁判）：
- 必须是未占的边（`GRID_EDGE`）；非法着 / 超时 / 崩溃 → **对手 2-0 胜**（scores 归一化）。
- 占边后检查相邻格心四边是否全占，成格则 **当前玩家得分**（格属该玩家）并连走。
- **多数胜**：先到 `⌈boxes/2⌉` 分（N=6 时 13）立即胜（不等终局）。
- **归属追踪**：每条已占边记 `edge_owner`，每个已闭合格记 `box_owner`；`move` 事件带 `closed_boxes`（本手新闭合格+owner），`match_end` 带 `box_owners` 网格——前端据此着色（已占边按玩家红/蓝，已占格按归属淡红/淡蓝）。

```python
def _box_completed(board, bx, by):
    for dx, dy in ((-1,0),(1,0),(0,-1),(0,1)):
        ex, ey = bx + dx, by + dy
        if board[ex][ey] != GRID_EDGE_USED:  # 四边未全占
            return False
    return True  # 成格 → curr_player 得分并连走
```

> 可用 [`samples/judges/pencil_judge.py`](../samples/judges/pencil_judge.py) 在本地自测占边 / 成格计分（`--check` 交互）。

## 样例 Bot

仓库：`samples/pencilbot.py`；裁判自测见 [`samples/judges/`](../samples/judges/)。完整样例亦见 `refs/botzone/Pencil.html`。

## 默认赛制模板

| template_id | 管线 |
|-------------|------|
| `pencil_group_drr_ko` | 分组双循环 → rest → 单败 |
| `pencil_swiss_ko` | 瑞士 → rest → 单败 |

评分：`ccgc_2_1_0`。小组成绩不带入决赛。

## 参考

1. https://botzone.org.cn/game/Pencil  
2. https://wiki.botzone.org.cn/index.php?title=Pencil  
3. `refs/botzone/Pencil.html`、`Pencil_game.html`
