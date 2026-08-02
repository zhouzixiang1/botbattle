# 裁判代码说明（仅管理员）

> 本页**仅管理员可见**，不进入公开 Wiki。描述三款游戏裁判引擎的代码位置、规则、可调参数与协议要点。
> 裁判代码本身在 Web 上**只读**；规则参数可在管理端「裁判」Tab 热调（下一局即生效）。代码逻辑改动需走业务代码流程（git 分支）。

## 架构总览

**实现真相在 `bzplat/backend/games/<game>/`**。每游戏自包含：`engine.py`（裁判）+ `protocol.py`（行协议）+ `result.py`（独立结果）+ `tiers.py` + `templates.py` + `spec.py`（装配 `GameSpec`）。

| 游戏 | 裁判实现 | Session 类 |
|------|---------|-----------|
| 德州扑克 | `games/holdem/engine.py` | `MatchSession` |
| 五子棋 | `games/gomoku/engine.py` | `GomokuSession` |
| 点格棋 | `games/pencil/engine.py` | `PencilSession` |

统一入口经 **游戏注册表**（`games.registry` / 兼容层 `engine/registry.py` 委托）：

```text
games.registry.get(game_id).run_session(decide, **params)
# 或：from bzplat.backend.games import run_session
```

每款游戏的 Session 都实现 `run_async(decide) → MatchResult`。结果类型**独立定义、不共享基类**，只靠鸭子契约（见下）。`engine/` 与 `protocol/` 目录仅为旧 import 的 **shim**（经 `_compat/` 转发到 `games/`），**不要**在 shim 里加新逻辑。

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
| `judge_holdem_default_hands` | 70 | 1–1000 | 德州挑战默认手数 |
| `judge_gomoku_board_size` | 15 | 9–19 | 五子棋棋盘边长 |

> 点格棋 N（`n_dots`）**不是**全局参数——由各对局的 match 配置决定，因此不在本页可调参数中。`None` 时由游戏 `GameSpec` / 引擎默认值兜底。

参数贯通链路：`platform_settings` → 编排 judge params → `runner.run_binaries()` → `games` 注册表 `run_session` → 各 Session 构造参数。

## 各游戏裁判要点

### 德州扑克（`games/holdem/engine.py`）

- HU NLHE；盲注 SB/BB 交替；`raise` 为 raise-to-total；min re-raise-to ≥ 2× 上一 raise-to。
- 非法着 / 超时 / 可恢复决策错误 → fold；all-in 后直接发出剩余公共牌结算。
- **进程崩溃 / EOF（`BotCrashedError`）** → 整场对局 `aborted`（`reason=bot_crashed`），不是继续 fold。
- `MatchSession` 一手 = 一轮，按手数循环，最终按筹码差判胜。

### 五子棋（`games/gomoku/engine.py`）

- 15×15（可调 9–19）；黑先（seat 0）；横/竖/斜连续 ≥5 含长连即胜；无禁手。
- 非法着 / 超时 → 判负；棋盘下满无人成五 → 平局。
- 进程崩溃 → 整场 `aborted`（`bot_crashed`）。
- 长驻行协议：请求 `{"v":1,"t":"mv","x","y","me"}`，响应 `{"x","y"}`。

### 点格棋（`games/pencil/engine.py`）

- N=11 点阵 → 交错网格 size=2N-1；红先（seat 0）；占相邻边围成格得分并连走；格多者胜。
- pass 语义：得分连走时通知对方 `pass=1`，对方须响应 `{"x":-1,"y":-1}` 把回合交还。
- 非法着 / 超时 → 判负；进程崩溃 → 整场 `aborted`。

## 改动裁判代码

裁判规则逻辑的修改（非参数）需改 `games/<game>/engine.py`（及相关 protocol/result）源码，按仓库规范：从 `main` 切特性分支 → 改 → 测试（`pytest`）→ GitHub PR 合并 → 删分支。**不要**改 `engine/` shim 冒充实现。Web 上不提供代码编辑能力。
