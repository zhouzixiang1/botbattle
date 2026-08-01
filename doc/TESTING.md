# 测试文档

> 本文档包含测试计划（策略/范围/用例设计）与测试报告（执行结果/缺陷/结论），证明平台满足验收准则。

## 1. 测试策略

采用**五层测试金字塔**，从单元到端到端全覆盖：

| 层级 | 工具 | 范围 | 目的 |
|------|------|------|------|
| **单元测试** | pytest | 后端模块（Store/引擎/协议/赛制/通知/社交/评分） | 验证各模块逻辑正确 |
| **集成测试** | pytest + TestClient | API 端点（鉴权/请求/响应/状态码） | 验证模块间协作 |
| **端到端冒烟** | `scripts/e2e_smoke.sh` | 完整流程（注册→上传→挑战→赛事） | 验证核心链路打通 |
| **API 全量测试** | `scripts/api_full_test.py` | HTTP API 业务正确性（鉴权/上传/挑战/SSE 一致性/并发） | 验证 API 层端到端正确 |
| **大规模压测** | `scripts/load_test.py` | 60 用户 × 8 阶段全端点 | 验证功能正确性 + 并发承受 |
| **浏览器验收** | `scripts/browser_verify.py` (Playwright) | 17 桌面路由 + 4 暗色 + 3 移动端 + 6 功能断言 = 30 项 | 验证前端渲染与交互 |

## 2. 测试范围

### 2.1 后端单元/集成测试（26 个测试文件）

| 测试文件 | 覆盖模块 |
|----------|---------|
| test_store | Store 数据层（增删改查/迁移自愈） |
| test_auth | 注册/登录/验证码/重置密码 |
| test_engine | 德州引擎（牌局/胜负/deltas） |
| test_board_engines | 五子棋 + 点格棋引擎 |
| test_result_types | RoundResult/MatchResult 解耦契约 |
| test_protocol | json/board 协议编解码 |
| test_runtime / test_runtime_settings | BinaryRunner（local）+ 运行时配置热更新 |
| test_judge_params | 裁判规则参数热调（棋盘/筹码/盲注） |
| test_contest_templates / test_contest_stages / test_contest_bracket | 赛制模板 + 阶段状态机 + 单败对阵图 |
| test_human_match | 人类 vs Bot（WebSocket/Future） |
| test_auto_matcher | 闲时自动对局调度 |
| test_notifications | 通知管理器 |
| test_comments_likes / test_social | 评论点赞 / 关注收藏 |
| test_user_profile_search / test_user_search / test_bot_profile | 用户主页 + 搜索 + Bot 详情 |
| test_matchpacks_site / test_xp_level / test_tiers / test_settings_mybots | 数据集/经验等级/段位/设置 |
| test_load_test_seed / test_logging | 压测种子 / 日志配置 |

> 配置：`pyproject.toml` 设 `testpaths=["bzplat/backend/tests","tests"]`，`pythonpath=["."]`，**须从仓库根运行 `pytest`**（当前 216 passed，含 test_human_match.py 8 项与 test_audit_coverage.py 8 项，无需 `--ignore`）。

### 2.2 大规模压测 8 阶段覆盖矩阵（`scripts/load_test.py`）

| 阶段 | 内容 |
|------|------|
| 0 基础 | 公开读 + 鉴权 + 通知 + 评论/点赞/浏览 + 改密码 |
| 1 Bot | 上传/版本/激活/更新/删除 + profile/opponents/rating-history |
| 2 对局 | 三游戏（holdem/gomoku/pencil）混跑 + 自博弈 |
| 3 SSE | 实时观赛事件流（snapshot + 增量） |
| 4 人类 | WebSocket /play 三游戏 + 不计 Glicko + per-user ≤1 |
| 5 赛事 | create/open/register/dispatch/start/finished/detail/bracket 全生命周期 |
| 6 auto-match | ladder 闲时调度（admin 开关 + 催化配置） |
| 7 Admin | users/bots/matches/contests/settings/judges/templates/email/logs 全端点 |

## 3. 测试执行结果（最新一轮）

### 3.1 后端 pytest
```
216 passed, 1 warning in ~46s
```
（含 test_human_match.py 8 项人类对战测试 + test_audit_coverage.py 8 项审计补充测试，已纳入常规套件，无需 `--ignore`）

### 3.2 大规模压测（60 用户）
```
压测完成：152 passed / 0 failed / 1 warns，总耗时 40.9s
```
- **全 8 阶段执行**，无任何阶段跳过。
- **152 项断言全通过，0 失败**。
- 唯一 warn：auto-match scheduler 在压测时序下未触发（**软断言**，逻辑由 `test_auto_matcher.py` 单测覆盖）。
- 方法：`BZ_RATE_LIMIT=0` 关限流 + `--no-throttle` 跳过节流 + `BZ_BOT_LOCAL=1` 本机跑 ELF 加速。

### 3.3 浏览器验收（Playwright）
```
浏览器功能验收：30 passed / 0 failed
```
- 明色桌面端：17 路由全部渲染正常。
- 暗色模式：4 页（home/leaderboard/botdetail/login）body 背景确认为深色 OKLCH。
- 移动端（375px）：3 页渲染正常，汉堡菜单可见。
- 关键功能：段位徽章、表格列、验证码组件、主题切换、全局搜索入口 全通过。

### 3.4 前端构建
```
✓ built（主包 index.js gzip ~115KB，无 >500KB chunk 警告）
```

## 4. 压测专项分析

### 4.1 并发承受
- 60 用户同时活跃，180 个 Bot。
- 三游戏对局并发执行（holdem/gomoku/pencil 混跑），全部 completed 无 aborted。
- 人类对战 WebSocket 三游戏并发完成。

### 4.2 瓶颈与优化
- **挑战限流**：dev 服务同 IP 共享，`/api/matches/challenge` = 8 req/60s。压测时用 `BZ_RATE_LIMIT=0` 关闭（生产环境应开启）。
- **Bot 执行**：每场对局需起 Bot 进程（Docker ~1s 或本机 subprocess），是单场主要耗时。压测用 `BZ_BOT_LOCAL=1` 加速。
- **代码分割**：前端主包从 974KB 优化到 365KB（gzip 115KB），recharts 隔离。

## 5. 缺陷与结论

### 5.1 已知非阻塞项
| 项 | 说明 | 处理 |
|----|------|------|
| `test_human_match.py` 时序 | WebSocket/Future 测试曾因时序卡住（pre-existing） | PR #24 修复人类对战治本后已稳定，8 项纳入常规套件（不再 `--ignore`） |
| auto-match scheduler 时序 | 压测环境后台 scheduler 连续 idle 计时不稳定 | 软断言，单测 `test_auto_matcher.py` 覆盖 |

### 5.2 测试结论
**平台功能完整、质量达标，满足验收准则**：
- 后端 200 单元/集成测试全通过。
- 60 用户大规模压测 152 项全通过、0 失败、40.9s 完成。
- 浏览器 30 项全通过（明暗 + 移动端）。
- 所有功能需求（账号/Bot/对局/观赛/回放/人类对战/排行/赛事/社交/通知/经验/管理/数据集）均有测试覆盖且通过。

> 返回 [doc/INDEX.md](./INDEX.md)
