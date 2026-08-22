# 对局通信协议

平台与 Bot 只通过 stdin/stdout 的**单行消息**通信。请求和响应使用本页定义的唯一 JSON
信封；上传预检与正式对局的首回合格式完全相同。

Bot 上传文件的唯一格式是 Linux x86_64 ELF；Windows PE / `.exe`、macOS Mach-O、
ARM64 ELF 和原始 `.py` 都不属于可运行格式。跨系统构建见
[Bot 开发指南](#/wiki?slug=bot-dev)。

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
- Bot 的每个响应必须是一个 JSON 对象，占一行并立即 flush stdout；整行不得超过
  **64 KiB**，超限按 `protocol_error` 技术判负。
- 响应对象必须包含顶层字段 `response`：

```json
{"response":<本游戏的响应 payload>}
```

`response` 的值由游戏决定：Holdem 是整数动作码；Pencil 是 `x/y` 坐标；Gomoku v2
是带 `action` 的分阶段动作对象。整个响应不能直接输出整数或坐标对象。真正参与请求历史重放、裁判与
结果的始终只有 `response`。正式 Bot 对战还可附带一个可选顶层 `debug` sidecar；除
`response` / `debug` 外的顶层字段全部忽略。

### 私有 debug sidecar

Bot 可在同一响应对象中加入任意 JSON 值（顶层 `null` 等同未提供）：

```json
{"response":0,"debug":{"street":"flop","equity":0.54,"choice":"call"}}
```

`debug` 不参与动作校验，也不会进入 `responses[]`、后续 Bot 请求、对局 `result`、公共回放、
公共观赛/人类对战消息、公开对局日志、五子棋专项棋谱或应用日志；LongRunning 的握手行也不是
debug。上传预检始终丢弃 debug。调试写入失败只会少一条调试记录，不改变动作、胜负或评分。

平台仅在 Bot-vs-Bot 对局进入终态后批量保存经过清洗的副本：单条最多 4 KiB、深度 4、
容器 64 项、256 节点；每座位最多 512 条/128 KiB，每场最多 1024 条/256 KiB。文本会做
NFC 归一化，移除 ANSI、控制字符、双向/不可见格式字符，并对密码、token、cookie、私钥等
敏感键和值脱敏。请不要把凭据或个人信息写入 debug。

读取不是公开能力：普通非赛事对局终局后，双方 Bot 的 owner 都能查看双方记录；赛事组织者
和管理员可在单场终局后查看，Bot owner 必须等整个赛事 `finished` 或 `cancelled`；人类对战
不向 Bot owner 提供记录，管理员可审计空结果。访客和无关用户无权读取，拒绝响应不会透露
记录是否存在。页面仅按纯文本/安全 JSON 展示，不把内容解释为 HTML、Markdown 或链接。

### 公开单场对局日志

三款游戏的对局正常完成或中止、且最终回放已经完整持久化后，任何访客都可从对局页下载
canonical JSON v1 日志。顶层严格为 `format="botbattle.match.log"`、`format_version=1`、
`match` 与 `replay`；其中 `replay` 固定含 `match_id/events/event_count/updated_at`。它只是页面公共
回放的确定性单场快照：未知/诊断事件默认不输出，历史技术故障只保留稳定码和脱敏说明，也不会
导出 Bot 的原始 stdin/stdout、stderr、私有 `debug`、二进制路径、执行配置或令牌。

直播中不提供半局文件。权威 Match 已终止但 `match_end/error` 尾项尚未落稳的短窗口返回 409，
而不是用旧事件前缀拼出貌似完整的日志。该能力严格按单场工作，不恢复已经下线的按月/批量
对局数据集。五子棋另有包含代数坐标、落子编号等派生字段的专项棋谱；两种 JSON 格式互不替代。

上传预检使用所选运行模式的同一首回合信封和同一响应校验；其 `debug` 会被丢弃，
LongRunning 预检也必须
完成握手。预检采用独立的 **8 秒首回合健康检查**，只用于确认程序能启动并完成一次通信，
不占用正式对局的思考时间。尤其是 Pencil 的正式对局仍按每方 900 秒累计棋钟计算。

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

## 4. Gomoku v2 payload

Gomoku 新局固定使用 `ruleset="gomoku_ccgc_2013_five_move_two_v2"`、`protocol_version=2`。上一竞赛代
`gomoku_ccgc_2013_v1` 仅用于历史对局与回放解释，不能用于创建新局。请求是
自包含快照，公共字段如下：

```json
{
  "protocol_version":2,
  "ruleset":"gomoku_ccgc_2013_five_move_two_v2",
  "phase":"normal_play",
  "me":1,
  "color":0,
  "seat_colors":[1,0],
  "board":[[-1,0,1]],
  "pass_allowed":true,
  "last":{"x":7,"y":8,"color":1}
}
```

- `me` 始终是参赛座位 `0/1`；三手交换后座位与棋色可能不同。
- `color` 为当前棋色：`0` 黑、`1` 白；`seat_colors[me]` 与之一致。
- `board` 是完整 `15×15` 列优先数组，`board[x][y]` 为 `-1/0/1`（空/黑/白）。
- `phase` 决定本回合唯一允许的动作；阶段附加字段见下表。

| `phase` | 决策方 | 附加字段 | 合法 `response` |
|---|---|---|---|
| `opening_proposal` | 开局座位 | `fixed_black1={7,7}`、`n_range=[2,2]` | `{"action":"opening","white2":{"x":7,"y":8},"black3":{"x":8,"y":8},"n":2}` |
| `swap_choice` | 另一座位 | `n` | `{"action":"swap","swap":true}` |
| `white4` | 最终白方 | `n` | `{"action":"move","x":6,"y":8}` |
| `black5_candidates` | 最终黑方 | `n` | `{"action":"black5_candidates","points":[{"x":9,"y":9},{"x":5,"y":5}]}` |
| `black5_select` | 最终白方 | `n`、`candidates` | `{"action":"black5_select","index":0}` |
| `normal_play` | 当前棋色 | `pass_allowed`、`last` | `{"action":"move","x":4,"y":4}` 或 `{"action":"pass"}` |

现行规则固定为**五手二打**。为保持 `gomoku_action_v2` wire 兼容，开局请求仍使用
`n_range`，但它固定为单值范围 `[2,2]`；后续阶段请求中的 `n` 都是 `2`。Bot 在 `opening` 中返回其他
`n`，或提交不等于两个黑5候选，都会被裁判判为规则非法。历史回放若已经记录三打、四打，
仍按原事件数量展示，不会重写为二打。

协议层只校验 `black5_candidates.points` 是点数组且每个坐标由两个整数组成；
数量恰好为 2、坐标互不重复、全部为空点，以及在当前四子局面的旋转/镜像
对称下互不同形，均由裁判按当前阶段统一校验。规则非法响应因此按
`illegal_candidates` 判负，而不是传输层 `protocol_error`。

动作仍必须放入标准信封，例如：

```json
{"response":{"action":"pass"}}
```

旧版 `{"response":{"x":7,"y":8}}` 没有 `action`，属于已废弃协议，会以
`protocol_error` 拒绝；平台不会自动猜测阶段或回退到旧规则。

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

棋钟信息只用于页面展示和回放，不会加入 Bot 请求 payload。

## 6. 格式故障与规则非法

| 情况 | 处理 |
|------|------|
| 非法 JSON、顶层非对象 | `protocol_error` 技术判负 |
| 缺少 `response` 或 response 类型错误 | `protocol_error` 技术判负 |
| 附带顶层 `debug` | 只在正式 Bot 对战中作有界私有 sidecar；动作仍只处理 `response` |
| 附带 `data/globaldata` 等其他顶层字段 | 忽略，只处理 `response` |
| stdout 响应行超过 64 KiB | `protocol_error` 技术判负 |
| 顶层整数或裸 `{x,y}` | `protocol_error` 技术判负 |
| Bot 决策超时 | `timeout` 技术判负，首个故障即结束 |
| LongRunning 握手缺失、超时或内容错误 | `protocol_error` 技术判负，不回退 |
| 响应格式正确，但加注或落子违反游戏规则 | 交给对应游戏裁判处理 |
| 平台沙箱自身故障 | 对局中止，不评分 |

Bot 对战中的 Bot 技术负会进入评分与赛事积分；人类对战不计 Bot 评分。平台自身运行故障
不会记到任一 Bot 名下。

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
