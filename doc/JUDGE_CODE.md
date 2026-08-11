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

每款游戏的 Session 都实现 `run_async(decide) → MatchResult`。结果类型**独立定义、不共享基类**，只靠鸭子契约（见下）。Gomoku/Pencil 的同构 JSON 原语只在 `games/_board_protocol.py` 实现一次并随公开源码返回；两款游戏的 `protocol.py` 分别只导出自身 builder。

## 解耦契约

裁判与赛制/编排层之间**无直接耦合**，全靠最小结果契约（`tests/test_result_contract.py` 守护）：

- `RoundResult.winners`：座位号列表，空表示平局。
- `RoundResult.deltas`：长度 2 的零和分差（德州=筹码差，棋类=±1 或得分差）。

编排层 `matches/orchestrator.py` 与赛制层 `contests/manager.py` **只读这两个字段**（及 `rounds`/`events`/`winner`），不碰扑克的 pot/board/holes 或棋类棋盘。

平台落库与公开 API 使用另一层稳定契约：`rounds_played`、`deltas`、`normalized_delta`，只能由
`matches/result_contract.py` 构造。`GameSpec.progress_from_events` 计算本游戏进度，
`GameSpec.normalize_delta` 把座位 1 原始分差转换成该游戏可比较的归一值。Holdem 的归一值是
整场筹码分差除以大盲，并非按固定手数折算的速率；复式对局的 `rounds_played` 累加全部 leg。

## 固定规则常量

| 游戏 | 固定规则 |
|------|----------|
| Holdem | 70 手；每手起始筹码 20000；小盲 50；大盲 100 |
| Gomoku | 15×15；黑先；无禁手；连续不少于 5 子即胜 |
| Pencil | N=6；红先；每方累计棋钟 900 秒 |

这些值不存入 `platform_settings`，不接受 match_config、admin 或直接 `run_session` kwargs 覆盖；未知参数立即报错。Holdem 上传预检与正式首请求均发送 `max_hand=70`。修改规则常量属于游戏规则变更，必须同时修改裁判/契约、测试与 Wiki 并走代码评审。Pencil 棋钟链路为 `GameSpec.time_budget_per_side` → orchestrator → `run_binaries`/`run_bot_vs_human` → `time_used/time_out` 事件。

## 各游戏裁判要点

### 德州扑克（规则 `holdem_judge.py`，适配 `engine.py`）

- HU NLHE；盲注 SB/BB 交替；Bot 协议 raise response 的正整数 = **额外下注筹码**（raise delta，引擎内部转 raise-to-total 校验，min re-raise-to ≥ 2× 上一 raise-to）。
- 格式正确但下注不合法 → fold；Bot 信封/response 格式错误或超时由平台层在进入裁判前立即技术判负。all-in 后直接发出剩余公共牌结算。
- **对局中途进程崩溃 / EOF（`BotCrashedError`）** → 引擎内**计分判负**（崩溃方本手全筹码给对手，对局 `completed`，`reason=crash`），不是继续 fold 跑完。
- **启动失败**由编排层处理：所有 Bot-vs-Bot 类型统一记为 `completed` + `technical_loss`，崩溃方判负；人类对战则记为 `aborted`（`bot_crashed`）。
- `MatchSession` 一手 = 一轮，按手数循环，最终按累计净筹码判胜。

### 五子棋（规则 `gomoku_judge.py`，适配 `engine.py`）

- **固定 15×15**（不可通过 match_config 或 admin 调整）；黑先（seat 0）；横/竖/斜连续 ≥5 含长连即胜；无禁手。
- 格式正确但非法着 → 裁判判负；Bot 协议错误/超时由平台层技术判负。棋盘下满无人成五 → 平局。
- 对局中途进程崩溃 → 计分判负（对手胜，`reason=crash`）。
- 唯一现行协议：请求信封 `{"request":{"x","y","me"}}`，响应信封 `{"response":{"x","y"}}`；裸坐标对象不合法（见 [协议规范](#/wiki?slug=protocol)）。

### 点格棋（规则 `pencil_judge.py`，适配 `engine.py`）

- **固定 N=6** 点阵 → 交错网格 size=2N-1=11；红先（seat 0）；占相邻边围成格得分并连走；格多者胜。
- pass 语义：得分连走时通知对方 `pass=1`，对方须响应 `{"response":{"x":-1,"y":-1}}` 把回合交还。
- Bot-vs-Bot 与人类对局均为每方累计 900s；决策成功发 `time_used`，总预算耗尽发 `time_out` 并判当前方负。人类侧同时有默认 120s 逐回合防挂机保护。
- 格式正确但非法着、人类棋钟耗尽 → 裁判判负；Bot 协议错误/棋钟耗尽由平台层技术判负。对局中途进程崩溃 → 计分判负（`reason=crash`）。MatchViewer 玩家卡由事件流显示剩余时间和超时徽章。

## 改动裁判代码

纯规则逻辑的修改应落在 `games/<game>/<game>_judge.py`；只有协议桥接、`decide` 调度或事件映射才改同包 `engine.py`，协议/结果契约变化再同步 `protocol.py` / `result.py`。按仓库规范：从 `main` 切特性分支 → 修改 → `pytest` → GitHub PR 合并 → 删分支。Web 上不提供代码编辑能力。
