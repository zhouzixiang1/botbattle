# 裁判代码说明

> 描述各游戏裁判引擎的代码位置、固定规则与协议要点。
> `/api/judges/{game_id}/source` 按 `GameSpec.source_files` 与 `shared_source_files` 公开纯裁判、适配、协议、结果及其共享实现源码，Web 上**只读**。游戏规则不通过管理后台修改；代码逻辑改动需走业务代码流程（git 分支）。

## 架构总览

**实现真相在 `bzplat/backend/games/<game>/`**。每游戏严格分层：`<game>_judge.py` 是纯游戏规则（零平台依赖），`engine.py` 是裁判与平台协议之间的适配层（调用 `decide`、驱动纯裁判并发出事件）；同包还包含 `protocol.py`（行协议）、`result.py`（独立结果）、`templates.py` 与装配 `GameSpec` 的 `spec.py`。

| 游戏 | 纯裁判规则 | 平台适配 / Session |
|------|------------|--------------------|
| 德州扑克 | `games/holdem/holdem_judge.py` | `games/holdem/engine.py` / `MatchSession` |
| 五子棋 | `games/gomoku/gomoku_judge.py` | `games/gomoku/engine.py` / `GomokuSession` |
| 点格棋 | `games/pencil/pencil_judge.py` | `games/pencil/engine.py` / `PencilSession` |

统一入口经 **游戏注册表**（`games.registry`）：

```text
games.registry.get(game_id).run_session(decide, **params)
# 或：from bzplat.backend.games import run_session
```

每款游戏的 Session 都实现 `run_async(decide) → MatchResult`。结果类型**独立定义、不共享基类**，只靠鸭子契约（见下）。Pencil 的 x/y JSON 原语使用随公开源码返回的 `games/_board_protocol.py`；Gomoku v2 的开局/交换/五手二打/PASS 判别联合由自身 `protocol.py` 实现。

## 解耦契约

裁判与赛制/编排层之间**无直接耦合**，全靠最小结果契约（`tests/test_result_contract.py` 守护）：

- `RoundResult.winners`：座位号列表，空表示平局。
- `RoundResult.deltas`：长度 2 的零和分差（德州=筹码差，棋类=±1 或得分差）。

编排层 `matches/orchestrator.py` 与赛制层 `contests/manager.py` **只读这两个字段**（及 `rounds`/`events`/`winner`），不碰扑克的 pot/board/holes 或棋类棋盘。

平台落库与公开 API 使用另一层稳定契约：`rounds_played`、`deltas`、`normalized_delta`，只能由
`matches/result_contract.py` 构造。`GameSpec.progress_from_events` 计算本游戏进度，
`GameSpec.normalize_delta` 把座位 1 原始分差转换成该游戏可比较的归一值。Holdem 的归一值是
一条对局记录的组合筹码分差除以大盲，并非按固定手数折算的速率；复式记录的 `rounds_played`
累加两场，但每场胜负独立产生赛事计分记录。

## 固定规则常量

| 游戏 | 固定规则 |
|------|----------|
| Holdem | 每个计分场 70 手；每手起始筹码 20000；小盲 50；大盲 100 |
| Gomoku | 15×15；26 种指定开局；三手交换；五手二打；黑方长连/三三/四四禁手 |
| Pencil | N=6；红先；每方累计棋钟 900 秒 |

这些值不存入 `platform_settings`，不接受 match_config、admin 或直接 `run_session` kwargs 覆盖；未知参数立即报错。Holdem 上传预检与正式首请求均发送 `max_hand=70`。修改规则常量属于游戏规则变更，必须同时修改裁判/契约、测试与 Wiki 并走代码评审。Pencil 棋钟链路为 `GameSpec.time_budget_per_side` → orchestrator → `run_binaries`/`run_bot_vs_human` → `time_used/time_out` 事件。

## 各游戏裁判要点

### 德州扑克（规则 `holdem_judge.py`，适配 `engine.py`）

- HU NLHE；盲注 SB/BB 交替；Bot 协议 raise response 的正整数 = **额外下注筹码**（raise delta，引擎内部转 raise-to-total 校验，min re-raise-to ≥ 2× 上一 raise-to）。
- 格式正确但下注不合法 → fold；Bot 信封/response 格式错误或超时由平台层在进入裁判前立即技术判负。现行 `holdem_hu_nlhe_allin_v2` 把 all-in 水位记为本街此前投入与剩余筹码之和；精确耗尽筹码的 call 进入 all-in 状态。覆盖短码全压的一方合法集仅为 fold/call，可只 call 匹配额并保留余额；为兼容同一 `holdem_action_v1` 的既有 Bot，此时 `response=-2` 规范化为精确 call，不产生虚假超额 all-in。下注关闭后只剩一名可行动玩家时直接发出剩余公共牌，不再逐街请求单人 check。
- **对局中途进程崩溃 / EOF（`BotCrashedError`）** → 引擎内**计分判负**（崩溃方本手全筹码给对手，对局 `completed`，`reason=crash`），不是继续 fold 跑完。
- **启动失败**由编排层处理：所有 Bot-vs-Bot 类型统一记为 `completed` + `technical_loss`，崩溃方判负；人类对战则记为 `aborted`（`bot_crashed`）。
- `MatchSession` 一手 = 一轮，每个 session 固定循环 70 手并按本场累计净筹码判胜；复式会新建
  两个同牌换座 session，两场独立判胜，组合计分差只作后置破同分。

### 五子棋（规则 `gomoku_judge.py`，适配 `engine.py`）

- **固定 15×15**（不可通过 match_config 或 admin 调整）；座位 0 提交 26 种之一的指定开局，五手候选数固定为 2，座位 1 行使三手交换权；最终白方落白4、最终黑方提交正好两个不同形的黑5候选、最终白方选择唯一黑5。历史回放中的三打、四打事件按原值展示，不参与新局规则判定。
- 黑方恰好五连优先获胜；否则长连、三三、四四判负。白方连续 ≥5 胜；第五子后允许 PASS，两方连续 PASS 或满盘为和棋。
- 指定开局几何与普通落子在 `gomoku_judge.py`，黑方禁手的递归真三/四检测在同一纯规则包的 `forbidden.py`，两者均不依赖平台层。
- 格式正确但非法着 → 裁判判负；Bot 协议错误/超时由平台层技术判负。棋盘下满无人成五 → 平局。
- 对局中途进程崩溃 → 计分判负（对手胜，`reason=crash`）。
- 唯一现行协议为 `gomoku_action_v2`：请求自包含 `phase/me/color/seat_colors/board`，响应是带 `action` 的开局/交换/落子/候选/选择/PASS 判别联合。旧 x/y-only 二进制版本已退役，不作兼容回落（见 [协议规范](#/wiki?slug=protocol)）。

### 点格棋（规则 `pencil_judge.py`，适配 `engine.py`）

- **固定 N=6** 点阵 → 交错网格 size=2N-1=11；红先（seat 0）；占相邻边围成格得分并连走；格多者胜。
- pass 语义：得分连走时通知对方 `pass=1`，对方须响应 `{"response":{"x":-1,"y":-1}}` 把回合交还。
- Bot-vs-Bot 与人类对局均为每方累计 900s；决策成功发 `time_used`，总预算耗尽发 `time_out` 并判当前方负。人类侧同时有默认 120s 逐回合防挂机保护。
- 格式正确但非法着、人类棋钟耗尽 → 裁判判负；Bot 协议错误/棋钟耗尽由平台层技术判负。对局中途进程崩溃 → 计分判负（`reason=crash`）。MatchViewer 玩家卡由事件流显示剩余时间和超时徽章。

## 改动裁判代码

纯规则逻辑的修改应落在 `games/<game>/<game>_judge.py`；只有协议桥接、`decide` 调度或事件映射才改同包 `engine.py`，协议/结果契约变化再同步 `protocol.py` / `result.py`。按仓库规范：从 `main` 切特性分支 → 修改 → `pytest` → GitHub PR 合并 → 删分支。Web 上不提供代码编辑能力。
