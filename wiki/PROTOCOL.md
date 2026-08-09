# 对局通信协议（唯一现行规范）

平台与 Bot 只通过 stdin/stdout 的**单行 JSON 信封**通信。本页是唯一现行协议；
代码、样例、上传预检和正式对局都必须服从这里的同一套格式。

Bot 上传文件的唯一格式是 Linux x86_64 ELF；Windows PE / `.exe`、macOS Mach-O、
ARM64 ELF 和原始 `.py` 都不属于可运行格式。跨系统构建见
[Bot 开发指南](#/wiki?slug=bot-dev)。

以下格式均不属于现行协议：顶层整数（如 `0`）、裸坐标对象（如 `{"x":7,"y":8}`）、
旧 `{"a":...}`、带 `debug` / `data` / `globaldata` 或其他额外字段的响应对象。
平台不会把旧格式转换成新格式，也不会因运行模式标错而回退。

## 1. 运行模式

上传每个 Bot 版本时必须选择一种运行模式。两种模式只改变**进程生命周期和请求历史的
传送方式**，游戏 payload 与响应信封完全相同。

### Traditional（默认）

- 每个决策点启动一个新进程，完成一次响应后停止。
- 每次发送完整历史信封：

```json
{"requests":[<请求 payload>,...],"responses":[<本 Bot 过去的 response payload>,...]}
```

- Bot 从 `requests[]` / `responses[]` 重建状态；`requests[-1]` 是当前决策。

### LongRunning

- 首个决策仍发送与 Traditional 相同的完整历史信封。
- Bot 输出首个 JSON 响应后，必须紧接着单独输出一行精确握手串：

```text
>>>BOTZONE_REQUEST_KEEP_RUNNING<<<
```

- 握手成功后进程整场保留，后续每次只发送：

```json
{"request":<当前请求 payload>}
```

- Bot 必须用进程内存维护后续状态。

握手大小写、尖括号和下划线必须完全一致，并以换行结束。缺失、超时或内容不一致均为
`protocol_error`，平台立即结束对局；不会改发完整历史，也不会切换为 Traditional。

## 2. 行与响应信封

- 平台的每个请求信封占一行。
- Bot 的每个响应必须是一个 JSON 对象，占一行并立即 flush stdout。
- 响应对象只允许一个顶层字段 `response`：

```json
{"response":<本游戏的响应 payload>}
```

`response` 的值由游戏决定：Holdem 是整数动作码；Gomoku / Pencil 是含 `x`、`y`
整数坐标的对象。整个响应不能直接输出整数或坐标对象，也不能附加其他字段。

上传预检使用所选 `runtime_mode` 的同一首回合信封和同一严格响应校验；LongRunning
预检也必须完成握手。预检不是另一套简化协议。

## 3. Holdem payload

Holdem 固定进行 **70 手**；每手双方起始筹码固定 20000、小盲固定 50、大盲固定 100，
每手重新开始。每条请求 payload 恰好包含以下 11 个字段：

| 字段 | 类型 | 含义 |
|------|------|------|
| `num_players` | int | 恒为 `2` |
| `dealer_id` | int | 本手庄家 / 小盲座位，`0` 或 `1` |
| `my_id` | int | 本 Bot 座位，`0` 或 `1` |
| `my_chips` | int | 当前手剩余筹码 |
| `my_cards` | int[2] | 两张底牌，编码 `0..51` |
| `public_cards` | int[] | 公共牌，长度 `0..5` |
| `history` | object[] | 当前手动作历史 |
| `hand` | int | 当前手序号，`0..69` |
| `max_hand` | int | 恒为 `70` |
| `total_win_chips` | int[2] | 双方累计净筹码 |
| `total_win_games` | int[2] | 双方累计赢手数 |

不会下发 `to_call`、`sb`、`bb`、`opp_chips` 等扩展字段；Bot 应从 `history` 与
`my_chips` 重放状态。

动作历史元素格式：

```json
{"round":0,"player_id":1,"action":250,"action_type":"raise"}
```

`round` 为 `0` preflop、`1` flop、`2` turn、`3` river。`action_type` 只可能是
`fold` / `allin` / `call` / `check` / `raise`。

Holdem 的 `response` 值必须是整数动作码：

| 值 | 动作 |
|----|------|
| `-1` | fold |
| `-2` | all-in |
| `0` | call 或 check；裁判按当前下注状态判定 |
| `>0` | 本次**额外投入**的筹码（raise delta） |

例如本街已投 50、目标总下注 300，则响应为 `{"response":250}`。顶层直接输出
`250` 不合法。

### 卡牌编码

```text
card = (点数 - 2) * 4 + 花色
点数 = card // 4 + 2
花色 = card % 4：0=红心，1=方块，2=黑桃，3=梅花
```

## 4. Gomoku payload

棋盘固定 **15×15**，坐标从 0 开始，黑方座位 0 先手。

请求 payload：

```json
{"x":7,"y":7,"me":1}
```

- `x`,`y` 是对手最近一手；黑方首回合为 `-1,-1`。
- `me` 是本方座位，`0` 黑、`1` 白。

响应必须完整包在信封中：

```json
{"response":{"x":7,"y":8}}
```

## 5. Pencil payload

点格棋固定 **N=6**（交错坐标板为 11×11），双方各有固定 **900 秒累计棋钟**。

请求 payload：

```json
{"x":3,"y":4,"pass":0,"me":0,"scores":[1,0]}
```

| 字段 | 含义 |
|------|------|
| `x`,`y` | 对手最近占用的边；首回合或连走通知为 `-1,-1` |
| `pass` | `1` 表示对手得分连走，本方须回传 `-1,-1` |
| `me` | 本方座位，`0` 红、`1` 蓝 |
| `scores` | `[红方得分, 蓝方得分]` |

普通占边响应：

```json
{"response":{"x":5,"y":4}}
```

`pass=1` 时响应：

```json
{"response":{"x":-1,"y":-1}}
```

棋钟事件 `time_used` / `time_out` 只进入回放和 SSE，不会加入 Bot 请求 payload。

## 6. 格式故障与规则非法

| 情况 | 处理 |
|------|------|
| 非法 JSON、顶层非对象 | `protocol_error` 技术判负 |
| 缺少 `response`、额外顶层字段、response 类型错误 | `protocol_error` 技术判负 |
| 顶层整数、裸 `{x,y}`、旧 `{a}` | `protocol_error` 技术判负 |
| LongRunning 握手缺失或错误 | `protocol_error` 技术判负，不回退 |
| 决策或握手超时 | `timeout` / 协议技术负，首个故障即结束 |
| 响应格式正确，但加注或落子违反游戏规则 | 交给对应游戏裁判处理 |
| 平台沙箱自身故障 | `aborted + platform_error`，不评分 |

Bot-vs-Bot 的技术结果进入评分与赛事积分；人类对战不计 Glicko。现行事件名为
`technical_incident`；结果/API 只公开 `technical_incident_count`、
`technical_incidents_by_seat` 与最多 3 条 `technical_incident_samples`，列表过滤参数为
`has_technical_incidents`。历史旧事件仅在服务端内部只读归一化，不作为新写入或第二套对外
字段。诊断不保存 Bot 原始 stdout 或服务器私有路径。

## 7. 最小示例

Traditional 或 LongRunning 首回合输入（Holdem）：

```json
{"requests":[{"num_players":2,"dealer_id":0,"my_id":0,"my_chips":19950,"my_cards":[48,51],"public_cards":[],"history":[],"hand":0,"max_hand":70,"total_win_chips":[0,0],"total_win_games":[0,0]}],"responses":[]}
```

Bot 输出：

```json
{"response":0}
```

若上传版本选择 LongRunning，再输出握手行；若选择 Traditional，平台在读完首个 JSON
响应后结束本次进程。完整可编译样例见 [Bot 开发指南](#/wiki?slug=bot-dev)。
