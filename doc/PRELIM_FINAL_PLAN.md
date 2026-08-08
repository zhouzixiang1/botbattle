# 预赛与决赛赛事体系实施计划（历史归档）

> **归档说明（2026-08-03）**：本文件为预赛/决赛体系落地过程中的**实施计划草稿**，仅作历史与决策追溯。  
> **现行权威文档**：[`wiki/GUIDE.md`](../wiki/GUIDE.md)、[`doc/DESIGN.md`](./DESIGN.md)。  
> 文中「现状差距」表格描述的是落地**前**代码事实，勿当作当前实现。

| 项 | 内容 |
|----|------|
| 状态 | **已落地并归档**（PR #69-75 实现预赛/决赛；模板 seed 对账等后续 PR 已合入） |
| 日期 | 2026-08-02（起草）；2026-08-03 自仓库根 `plan.md` 迁入 `doc/` |
| 范围 | 德州扑克内置预赛/决赛模板 + 赛事身份/冻结/排名/组织者名单等底座 |
| 工作流 | 从 `main` 切特性分支，分 PR 合并；禁止本地直合 main |
| 相关代码 | `bzplat/backend/contests/`、`games/holdem/`、`matches/`、`runtime/`、`store/`、`frontend/src/pages/Contest*.tsx` |
| 相关文档 | 以 wiki/GUIDE、doc/DESIGN 为准（本文件不再同步维护） |

---

## 1. 目标

在现有多游戏 Bot 对战平台上，交付可运营的「预赛 + 决赛」双 contest 体系：

1. **预赛**：单阶段瑞士，公开报名，打完给出**全员唯一正式名次**。
2. **决赛**：独立 contest，公开报名，**无报名人数上限**；组织者加减人；赛制为全员单循环 → Top8 双循环，给出全员唯一正式名次。
3. 两场互不强制导入名册；可用 `source_contest_id` 软链便于导航。
4. 名册管理主身份为**该场组织者**（`contests.organizer_id`），不是 admin 后台专属能力。

非目标（本计划不做）：资格自动导入、confirm/候补递补、预赛第二阶段「200 人精排」、把组织者名单操作做成仅 Admin Tab 可用。

---

## 2. 现状（代码事实）

基于当前仓库实现（`ContestManager` / `schema` / `limits` / `api_routes`）：

| 能力 | 现状 | 差距 |
|------|------|------|
| 多 stage 状态机 `draft→open→published→running⇄rest→finished` | 有 | 可复用（published 为时间编排新增的排期已发布态） |
| 瑞士 / RR / DRR / 分组 / 单败 | 有 | 预赛只需瑞士；决赛需大 RR 例外 |
| 自行报名 `POST /api/contests/{id}/register` | 仅 `open` | 决赛同样走此路径；**不加** capacity 拒绝 |
| 组织者 open/start/resume | 校验 `organizer_id` | 名单加减尚未对组织者开放 |
| Admin `entries/bulk`、删 entry | `/api/admin/...` | 仅超管/测试；需迁到组织者 contest API |
| `bot_versions` 表 | 上传时写入 | 赛事 pairing/match **未冻结**；`_run_match` 读最新 `binary_path` |
| `standings()` | 按 **`bot_id`**，排序 `(-points,-net_chips)` | 换 Bot 丢分；无 Buchholz 等；无 official 全员榜 |
| `dispatch()` | 可改 pending pairing 的 bot | 违反「已发布轮冻结」 |
| 超时/崩溃 | 超时多变为 fold 继续；崩溃常 `aborted` | 需技术判负 + completed |
| `FULL_RR_MAX_N`（默认 12） | `ContestManager._guard_full_rr` | 决赛全员 RR 会被拒，需模板级 `allow_large_round_robin` |
| `BOT_CPUS` | 硬顶 `1.0`；ceiling=`cpu//4` | 万人赛 ETA 按此估算；若改 0.5 须同步 AGENTS.md |
| 内置德州模板 | 仅 `holdem_swiss_ko`、`holdem_rr` | 需新增预赛/决赛模板 |
| `estimate()` | 各 stage 用同一报名人数 | 须按 `advance_count` 传播 |

---

## 3. 产品规则

### 3.1 双 contest

- 预赛、决赛是两个独立 `contests` 行，不是同一 contest 的两个外层 stage。
- `phase`：`preliminary` / `final` / `standalone`（默认）。
- `source_contest_id`：可选软链；创建/展示用；**不复制 entry**。

### 3.2 报名与名册

- 预赛、决赛均为公开 `register`（自选 Bot）。
- **决赛无报名人数上限**（不设 `capacity_max`，register 不因人数满员失败）。
- 名册由**该场组织者**在未开赛（`draft`/`open`）时加人、删人、批量加人；`admin` 可代理。
- `running` / `rest` / `finished` 后禁止改名册。
- 同用户同 contest 最多一个 entry（现有 `UNIQUE(contest_id, user_id)` 保留）。

### 3.3 身份与换 Bot

- 排名/积分/对手历史键为 `contest_entry.id`，不是 `bot_id`。
- 轮间可换 Bot 或新版本；**仅下一尚未发布轮**生效；已发布轮的 bot、version、seed 冻结。
- 运行时必须用冻结的 `bot_version_id → binary_path`。

### 3.4 正式排名

- 两场结束都必须落库全员 `official_rank`（唯一、连续），可 API/CSV/前端展示。
- 奖项、`suggested_finalist`（预赛可选）仅为标注，不驱动决赛名册。

### 3.5 时长口径

- 预赛 N=10000、约 14 轮 ≈ 70000 场：在现行半负载并发下为**数日～一周**量级，禁止宣传「一天打完」。
- 决赛场次随实际报名人数 M 变化：`M*(M-1)/2 + 8*7`（Top8 双循环 56 场）；M=50 时约 1281 场，通常一天内可完成。组织者需自行控制决赛规模，避免过大 RR。

---

## 4. 模板设计

### 4.1 预赛 `holdem_prelim_swiss`

名称建议：德州：预赛（瑞士全员排名）

- `phase=preliminary`，公开报名，无人数上限。
- **唯一 stage**：`type=swiss`，`rounds=0`（`ceil(log2(n))`），`poker_3_1_0`。
- 无 `advance_count`、无第二阶段、无 top-200 精排。
- 破同分：`points → buchholz_cut1 → sonneborn_berger → head_to_head → net_bb_per_100 → technical_losses → public_seed`。
- `bot_swap_mode=next_round`；默认 `replay_mode=summary`；建议 70 手 + duplicate。
- 结束后：official 1..N + 奖项百分比 + 可选标注前 50 为 suggested_finalist。

### 4.2 决赛 `holdem_final_ranked`

名称建议：德州：决赛（循环→Top8）

- `phase=final`，公开报名，**无人数上限**。
- Stage1：`round_robin`，`allow_large_round_robin=true`，`advance_count=8`，较长手数 + duplicate。
- Stage2：`double_round_robin`（Top8），`ranking_mode=replace_top`，`ranking_scope=8`。
- 合成榜：1..8 取自 Top8；9..M 取自 Stage1 未晋级者相对序。
- 普通赛仍受 `FULL_RR_MAX_N=12` 保护；仅本 builtin（或 admin 校验模板）允许大 RR。开赛前 UI/`estimate` 必须展示「当前 M 人 → 场次」，避免组织者误开超大规模循环。

---

## 5. 技术方案

### 5.1 数据模型（幂等迁移）

- `contests`：`phase`、`source_contest_id`、`policy_json`、`official_results_ready`、seed 承诺/揭示字段等。不设报名用 `capacity_max`。
- `contest_templates.policy_json`；创建时冻结进 contest。
- `contest_entries`：`selected_bot_version_id`；排名键用 `id`；删 Bot 不得 CASCADE 抹成绩。
- 新建：`contest_entry_dispatches`、`contest_rounds`（或等价）、`contest_official_results`。
- `contest_pairings`：`entry_*`、`bot_*_version_id`、`pairing_seed`、`published_at`。
- `contest_stage_results`：唯一键改为 `(contest_id, stage_idx, entry_id)`。
- `matches_*`：version、seed、耗时、technical_loss、replay_mode。

### 5.2 排名模块

新增 `bzplat/backend/contests/ranking.py`：stage 积分、破同分、决赛 `replace_top` 合并、奖项、persist。禁止再只按 points+net_chips。

### 5.3 瑞士扩容

重写/强化 `stages.swiss_pairings`：entry 主体、确定性 seed、积分组、避重、座位平衡、bye；目标接近 O(N log N)；附 10k 纯编排压测（不启 Docker）。

### 5.4 轮次冻结与 dispatch

发布轮时写入 pairing 快照；dispatch 只影响未发布轮；orchestrator 读 version 路径。

### 5.5 组织者名单 API

挂在 contest 路由（与 open/start 同权限模型）：

| 方法 | 路径 | 权限 |
|------|------|------|
| POST | `/api/contests/{id}/entries` | 该场组织者或 admin |
| POST | `/api/contests/{id}/entries/bulk` | 同上 |
| DELETE | `/api/contests/{id}/entries/{user_id}` | 同上 |

可参考现有 admin bulk 实现，但**主路径不是** `/api/admin/...`。前端做在 `ContestDetail.tsx`。

可选：`POST /api/contests/{prelim_id}/create-companion-final` 只建空决赛并软链，不拷贝报名。

正式结果：`GET /api/contests/{id}/official-results` 及 csv/json 导出。

### 5.6 运行时与调度

- Duplicate：经 `GameSpec.build_match_plan`，通用 runner 跑 legs；禁止通用层 `if game_id=="holdem"`。
- 时间银行 + 动作硬超时 → `BotForfeitError` → match completed 技术负。
- 有界调度泵；索引 `(contest_id, stage_idx, round_num, status)`；同 contest 推进加锁。
- 预赛 summary 回放；决赛 full。
- 资源：默认维持 `BOT_CPUS=1.0` 与现网一致；若改为 0.5 须同步 `limits.py`、BinaryRunner、AGENTS.md、doc/RUNTIME.md（单独决策，不静默漂移）。

---

## 6. 交付阶段（PR 拆分）

| 阶段 | 分支 | 交付 | 验收 |
|------|------|------|------|
| P0 | `feat/contest-entry-identity` | standings/pairings/stage_results 以 entry 为键；FK 策略修正 | 换 Bot 不丢历史分；旧模板仍可跑 |
| P1 | `feat/contest-version-freeze` | rounds + dispatch 表；冻结 version/seed；orch 读冻结路径 | 已发布轮不被 dispatch 改写 |
| P2 | `feat/contest-ranking-official` | ranking.py；official_results；导出 API | 全员唯一名次；破同分单测 |
| P3 | `feat/swiss-scale` | 可扩展瑞士；estimate 传播 n；10k 编排 bench | 场次公式与性能达标 |
| P4 | `feat/contest-runtime-policy` | 时间银行、技术负、duplicate、replay_mode | 契约测试 + 兼容 challenge/ladder |
| P5 | `feat/prelim-final-contests` | 两模板；phase/软链；组织者名单；调度泵；前端；doc/wiki | 端到端：报名→组织者裁员→开赛→全员榜 |

每阶段：从 main 拉分支 → 实现 → `pytest`（动前端再 `npm run build`）→ `gh pr create` → 合并后删分支。

---

## 7. 测试与文档清单

**测试（按阶段补齐）**

- 迁移幂等；entry 身份；version 冻结；官方榜唯一连续。
- 预赛：单阶段；N=10000 dry-run 场次 70000；suggested_finalist 不产生决赛 entry。
- 决赛：无人数上限仍可 register；组织者加减人；非组织者拒绝；running 后拒改名册；大 RR 仅白名单模板。
- 运行时/调度/解耦守护（`test_import_cycles` 等）不回归。

**文档**

- `wiki/GUIDE.md`：双 contest、单阶段瑞士、决赛无报名上限、组织者名单、ETA。
- `doc/RUNTIME.md`：若调整 CPU/并发。
- `doc/DESIGN.md`、`doc/TESTING.md`、`README.md` 能力一览。
- 本文件在合并 P5 后可将状态改为「已落地」，并在 DESIGN 中吸收稳定架构段。

---

## 8. 风险

| 风险 | 缓解 |
|------|------|
| 决赛无上限导致超大 RR | estimate + 开赛前明示场次；组织者删人；仅白名单模板绕过 `FULL_RR_MAX_N` |
| SQLite + 7 万场写放大 | 预赛 summary 回放；有界并发 |
| 换 Bot 语义与旧 dispatch 不兼容 | P1 明确 next-round；测试锁行为 |
| 单 PR 过大 | 严格执行 P0–P5 拆分 |

---

## 9. 完成定义

全部阶段合并进 main 后：

1. 内置预赛/决赛模板可用。
2. 组织者可在比赛详情管理名册；决赛报名无人数上限。
3. 两场均可导出全员唯一正式名次。
4. 版本/种子按轮冻结；轮间可换 Bot。
5. 无资格导入流；pytest 通过；前端改动则 build 通过；文档已同步。
