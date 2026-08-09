# 点格棋 (Pencil)

对齐 [Botzone · Pencil](https://botzone.org.cn/game/Pencil) / [Wiki · Pencil](https://wiki.botzone.org.cn/index.php?title=Pencil)。  
本平台 `game_id`：**`pencil`**（显示名「点格棋」）。裁判引擎 **已注册**。

## 规则

1. 棋盘由 **N×N 个点**构成；**固定 N=6**（规则钉死，不可配；对齐 Botzone 官方裁判 `grid_size=11` 交错维度 → 6 点 → 交错板 `size=2N-1=11` → `(N-1)²=25` 格，奇数无平局）。
2. 坐标从 0 开始，**先 x 后 y**，原点左上。
3. **红方先手**（seat `0`）；轮流占横/竖相邻两点之间的边（不可越点、不可重边）。
4. 某格四边占满 → **最后占边者得该格**（格属占格者），并 **连走**；未得分则换手。
5. **多数胜**：先占领多数格（`⌈boxes/2⌉`，N=6 时为 13）的一方立即获胜（对齐裁判 `hasPlayerWon`）；全部占完则格多者胜。
6. 双方各有独立、固定 **900 秒（15 分钟）累计棋钟**；Bot-vs-Bot 与人类对战使用同一时限，不能靠换回合重置。
7. 格式正确但非法着、或**人类**棋钟耗尽 → 裁判判对手 2-0 胜；Bot 棋钟耗尽 / 协议错误则由平台立即记 `timeout` / `protocol_error` 技术负。对局中途进程崩溃 → **计分判负**（对手胜，`reason=crash`，对局 `completed`）；启动失败见 [对局](#/wiki?slug=guide)。

### 棋钟与界面

- 平台只在某一方实际思考时累计该座位的用时。一次决策成功返回后，事件流追加 `time_used {seat,used,remaining,budget}`；900 秒耗尽时追加 `time_out {seat,used,budget}` 并判该方负（着法是否合法仍由裁判另行判断）。
- 这些是平台回放 / SSE 事件，**不会发进 Bot 的 stdin，也不改变下方 Botzone 请求/响应字段**。
- 观赛与回放页的双方玩家卡显示剩余时间；第一条计时事件到来时，尚未行动的一方按共同 `budget=900` 初始化，不会误显示 `0:00`；超时方显示 `0:00` 和「超时」标记。
- 人类对战页仍显示通用的 **120 秒单回合倒计时**；后端同时累计 Pencil 每方 900 秒总预算，以先到的限制为准。

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

### Traditional 状态恢复（样例逻辑）

```python
board = make_empty_board()
for req in envelope["requests"]:       # 对手已经占过的边（含当前 request）
    mark_used(req["x"], req["y"])
for resp in envelope["responses"]:     # 自己过去占过的边
    mark_used(resp["x"], resp["y"])

current = envelope["requests"][-1]
if current["pass"] == 1:
    resp = (-1, -1)
else:
    resp = choose_legal_edge()
```

## 本平台双模式行协议

平台默认 Traditional：每次重启进程并发送完整 `requests[]/responses[]`。显式选择 LongRunning 后，首回合发送完整历史；Bot 响应并输出标准握手后，后续发送单 request：

```json
{"request":{"x":3,"y":4,"pass":0,"me":0,"scores":[1,0]}}
```

| 字段 | 含义 |
|------|------|
| `x`,`y` | 对方上一手边坐标；首手 / 连走后通知为 `-1,-1` |
| `pass` | `1` 时必须响应 `{"response":{"x":-1,"y":-1}}` |
| `me` | 本方座位 |
| `scores` | 当前红/蓝得分 |

响应信封：`{"response":{"x":5,"y":4}}`；pass 回合：`{"response":{"x":-1,"y":-1}}`。

信封与 `{x,y}` 落子兼容 Botzone；`me`、`scores` 是本平台附加状态字段。LongRunning 未握手时平台最多等待 1 秒，然后在同一进程继续发送完整历史作兼容回退；这不等于 Traditional 重启。详见 [协议规范](#/wiki?slug=protocol)。

### 连走时序（与 Botzone 对齐）

1. A 占边并得分 → 向 B 发送该边且 `pass=1`，B 回 `-1,-1`。  
2. 再向 A 发送 `x=y=-1, pass=0`，A 继续占边。  
3. 未得分则直接换手，`pass=0`。

## 裁判判定逻辑

服务端裁判判定（对齐 Botzone 官方 C++ 裁判）：
- 必须是未占的边（`GRID_EDGE`）；格式正确但非法着由裁判判对手 2-0。人类耗尽 900 秒累计棋钟同样由裁判判负；Bot 耗尽则持久化为 `completed + reason=timeout + technical_loss=1`。对局中途进程崩溃 → 计分判负（`reason=crash`）。
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

仓库同时提供：

- `samples/pencilbot.c`：可编译的 C 源码；正确重放 Traditional 完整历史，并支持 LongRunning 握手与增量 request。
- `samples/pencilbot.py`：便于阅读和本地调试的等价 Python 源码，`.py` 文件不能直接上传。
- `samples/pencilbot_linux_amd64`：执行 `bash samples/build_sample.sh` 后生成的可上传 Linux ELF。上传时游戏选择 `pencil`，Traditional（默认）或 LongRunning 均可。

裁判自测见 [`samples/judges/`](../samples/judges/)。

## 默认赛制模板

| template_id | 管线 |
|-------------|------|
| `pencil_group_drr_ko` | 分组双循环 → rest → 单败 |
| `pencil_swiss_ko` | 瑞士 → rest → 单败 |

评分：`ccgc_2_1_0`。小组成绩不带入决赛。

## 参考

1. https://botzone.org.cn/game/Pencil  
2. https://wiki.botzone.org.cn/index.php?title=Pencil  
3. 仓库样例与服务端裁判源码（见上文链接）
