# 裁判代码说明

> 描述各游戏裁判引擎的代码位置、规则、可调参数与协议要点。
> `/api/judges/{game_id}/source` 按 `GameSpec.source_files` 公开纯裁判、适配、协议与结果源码，Web 上**只读**；规则参数可在管理端「裁判」Tab 热调（下一局即生效）。代码逻辑改动需走业务代码流程（git 分支）。

## 架构总览

**实现真相在 `bzplat/backend/games/<game>/`**。每游戏严格分层：`<game>_judge.py` 是纯游戏规则（零平台依赖），`engine.py` 是裁判与平台协议之间的适配层（调用 `decide`、驱动纯裁判并发出事件）；同包还包含 `protocol.py`（行协议）、`result.py`（独立结果）、`tiers.py`、`templates.py` 与装配 `GameSpec` 的 `spec.py`。

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

每款游戏的 Session 都实现 `run_async(decide) → MatchResult`。结果类型**独立定义、不共享基类**，只靠鸭子契约（见下）。`engine/`/`protocol/`/`_compat/` shim 已删除，真实现全在 `games/`。

## 解耦契约

裁判与赛制/编排层之间**无直接耦合**，全靠最小结果契约（`tests/test_result_contract.py` 守护）：

- `RoundResult.winners`：座位号列表，空表示平局。
- `RoundResult.deltas`：长度 2 的零和分差（德州=筹码差，棋类=±1 或得分差）。

编排层 `matches/orchestrator.py` 与赛制层 `contests/manager.py` **只读这两个字段**（及 `rounds`/`events`/`winner`），不碰扑克的 pot/board/holes 或棋类棋盘。

## 可调规则参数（热生效）

规则参数存在 `platform_settings`，编排层每局热读，下局立即用新值。默认值与各引擎/GameSpec 常量对齐。

| 参数 | 默认 | 范围 | 说明 |
|------|------|------|------|
| `judge_holdem_starting_stack` | 20000 | 1000–1000000 | 德州起始筹码 |
| `judge_holdem_sb` | 50 | 1–10000 | 德州小盲注 |
| `judge_holdem_bb` | 100 | 2–20000 | 德州大盲注（须 > SB） |

> 游戏规则参数（手数/棋盘边长/点阵边长）已**钉死固定值**，不再是 admin 可调项：
> holdem 固定 70 手、gomoku 固定 15×15、pencil 固定 6 点。原 `judge_holdem_default_hands` /
> `judge_gomoku_board_size` 设置项已移除。

Pencil 另由 `GameSpec.time_budget_per_side=900.0` 固定每方累计 15 分钟棋钟；这是游戏固有运行契约，不在 `platform_settings` 中热调。

参数贯通链路：`platform_settings` → 编排 judge params → `runner.run_binaries()` → `games` 注册表 `run_session` → 各 Session 构造参数。棋钟链路独立为 `GameSpec.time_budget_per_side` → orchestrator → `run_binaries`/`run_bot_vs_human` → `time_used/time_out` 事件。

## 各游戏裁判要点

### 德州扑克（规则 `holdem_judge.py`，适配 `engine.py`）

- HU NLHE；盲注 SB/BB 交替；Bot 协议 raise response 的正整数 = **额外下注筹码**（raise delta，引擎内部转 raise-to-total 校验，min re-raise-to ≥ 2× 上一 raise-to）。
- 非法着 / 超时 / 可恢复决策错误 → fold；all-in 后直接发出剩余公共牌结算。
- **对局中途进程崩溃 / EOF（`BotCrashedError`）** → 引擎内**计分判负**（崩溃方本手全筹码给对手，对局 `completed`，`reason=crash`），不是继续 fold 跑完。
- **启动失败**由编排层处理：所有 Bot-vs-Bot 类型统一记为 `completed` + `technical_loss`，崩溃方判负；人类对战则记为 `aborted`（`bot_crashed`）。
- `MatchSession` 一手 = 一轮，按手数循环，最终按累计净筹码判胜。

### 五子棋（规则 `gomoku_judge.py`，适配 `engine.py`）

- **固定 15×15**（不可通过 match_config 或 admin 调整）；黑先（seat 0）；横/竖/斜连续 ≥5 含长连即胜；无禁手。
- 非法着 / 超时 → 判负；棋盘下满无人成五 → 平局。
- 对局中途进程崩溃 → 计分判负（对手胜，`reason=crash`）。
- Botzone 标准协议：请求信封 `{"request":{"x","y","me"}}`，响应信封 `{"response":{"x","y"}}`（信封包裹见 [协议规范](#/wiki?slug=protocol)）。

### 点格棋（规则 `pencil_judge.py`，适配 `engine.py`）

- **固定 N=6** 点阵 → 交错网格 size=2N-1=11；红先（seat 0）；占相邻边围成格得分并连走；格多者胜。
- pass 语义：得分连走时通知对方 `pass=1`，对方须响应 `{"x":-1,"y":-1}` 把回合交还。
- Bot-vs-Bot 与人类对局均为每方累计 900s；决策成功发 `time_used`，总预算耗尽发 `time_out` 并判当前方负。人类侧同时有默认 120s 逐回合防挂机保护。
- 非法着 / 超时 → 判负；对局中途进程崩溃 → 计分判负（`reason=crash`）。MatchViewer 玩家卡由事件流显示剩余时间和超时徽章。

## 改动裁判代码

纯规则逻辑的修改应落在 `games/<game>/<game>_judge.py`；只有协议桥接、`decide` 调度或事件映射才改同包 `engine.py`，协议/结果契约变化再同步 `protocol.py` / `result.py`。按仓库规范：从 `main` 切特性分支 → 修改 → `pytest` → GitHub PR 合并 → 删分支。真实现全在 `games/`（旧顶层 `engine/` 包已删除），Web 上不提供代码编辑能力。
