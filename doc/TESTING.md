# 测试文档

> 本文档包含测试计划（策略/范围/用例设计）与测试报告（执行结果/缺陷/结论），证明平台满足验收准则。

## 1. 测试策略

采用**多层测试金字塔**，从单元到端到端全覆盖：

| 层级 | 工具 | 范围 | 目的 |
|------|------|------|------|
| **单元测试** | pytest | 后端模块（Store/引擎/协议/赛制/通知/社交/评分） | 验证各模块逻辑正确 |
| **集成测试** | pytest + TestClient | API 端点（鉴权/请求/响应/状态码） | 验证模块间协作 |
| **架构契约** | pytest 源码扫描 + AST + 导入序 | 游戏解耦 / 结果鸭子类型 / 无循环依赖 / match_config+result 双 JSON 通路 | 防止通用层重新耦合 + 死列/具名参数回退 |
| **端到端冒烟** | `scripts/e2e_smoke.sh` | 完整流程（注册→上传→挑战→赛事） | 验证核心链路打通 |
| **API 全量测试** | `scripts/api_full_test.py` | HTTP API 业务正确性（鉴权/上传/挑战/SSE 一致性/并发） | 验证 API 层端到端正确 |
| **大规模压测** | `scripts/load_test.py` | 60 用户 × 8 阶段全端点 | 验证功能正确性 + 并发承受 |
| **浏览器验收** | `scripts/browser_verify.py` / `screenshot_verify.py` | 路由渲染 + 暗色 + 移动端 + 截图 | 验证前端渲染与交互 |

## 2. 测试范围

### 2.1 后端单元/集成测试（55 个 `test_*.py`）

配置：`pyproject.toml` 设 `testpaths=["bzplat/backend/tests"]`，`pythonpath=["."]`，**须从仓库根运行 `pytest`**。
（`tests/` 为预留路径，当前用例均在 `bzplat/backend/tests/`。）

| 类别 | 测试文件 |
|------|----------|
| **架构契约（解耦守护）** | `test_result_contract`、`test_game_registry`、`test_import_cycles`、`test_tongyong_layer_no_game_branches`、`test_despecialization`、`test_physical_reorg`、`test_db_layer_extensibility` |
| **数据层 / 迁移** | `test_store`、`test_db_migration`（含 FK 全局开 + 孤儿清理 + 去重索引 + 删孤儿表） |
| **认证 / 安全** | `test_auth`、`test_security_logging`、`test_logging`、`test_audit_coverage`（含赛事崩溃判责）、`test_real_name`、`test_no_private_bot` |
| **引擎 / 协议** | `test_engine`、`test_board_engines`、`test_result_types`、`test_protocol`、`test_judge_params` |
| **运行时** | `test_runtime`、`test_runtime_settings` |
| **编排 / 人类** | `test_human_match`（含 resolve_human_turn 竞态）、`test_auto_matcher`、`test_match_seat_names`、`test_matches_pagination`、`test_api_game_filter`、`test_organizer_add_entry` |
| **赛事** | `test_contest_templates`、`test_contest_stages`、`test_contest_bracket`、`test_contest_entry_identity`、`test_contest_ranking`、`test_contest_runtime`、`test_contest_template_seed`、`test_contest_version_freeze`、`test_game_templates`、`test_admin_assign_entries`、`test_prelim_final`、`test_swiss_scale` |
| **社交 / 通知 / 成长** | `test_notifications`、`test_comments_likes`、`test_social`、`test_user_profile_search`、`test_user_search`、`test_bot_profile`、`test_xp_level`、`test_tiers`、`test_settings_mybots` |
| **数据与站点** | `test_matchpacks_site`、`test_load_test_seed`、`test_wiki_pages` |

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

## 3. 测试执行结果（最新核对）

> 数字会随开发演进；下列为文档对齐时点（2026-08）的实测/收集结果。合并前请以本机 `pytest` 为准。

### 3.1 后端 pytest
```
pytest --collect-only → 462 tests collected（随游戏/功能增长）
pytest 须从仓库根运行；54 个 test_*.py 模块
```
（含人类对战、审计、安全日志、游戏注册表/结果契约/通用层无分支等守护测试。）

### 3.2 大规模压测（60 用户）
```
历史基线：152 passed / 0 failed / 1 warns（约 40.9s）
```
- 全 8 阶段执行，无任何阶段跳过。
- 唯一 soft-warn：auto-match scheduler 在压测时序下可能未触发（逻辑由 `test_auto_matcher.py` 单测覆盖）。
- 方法：`BZ_RATE_LIMIT=0` 关限流 + `--no-throttle` + `BZ_BOT_LOCAL=1`。

### 3.3 浏览器验收
```
browser_verify / screenshot_verify：关键路由 + 明暗主题 + 移动端布局
```

### 3.4 前端构建
```
✓ npm run build（tsc -b && vite build；主包 gzip 量级约百 KB，recharts 等重依赖分 chunk）
```

## 4. 压测专项分析

### 4.1 并发承受
- 60 用户同时活跃，多 Bot；三游戏对局并发。
- 人类对战 WebSocket 三游戏可并发完成。

### 4.2 瓶颈与优化
- **挑战限流**：同 IP 共享 `/api/matches/challenge` = 8 req/60s。压测用 `BZ_RATE_LIMIT=0`（生产应开启）。
- **Bot 执行**：Docker 启动或本机 subprocess 是单场主要耗时；压测可用 `BZ_BOT_LOCAL=1`。
- **代码分割**：前端 lazy 路由 + 重依赖隔离。

## 5. 缺陷与结论

### 5.1 已知非阻塞项
| 项 | 说明 | 处理 |
|----|------|------|
| auto-match scheduler 时序 | 压测环境后台 idle 计时不稳定 | 软断言；单测 `test_auto_matcher.py` 覆盖 |
| 多 worker 限流 | 内存滑动窗口仅单进程有效 | 多 worker 需换 Redis 等共享存储 |

### 5.2 测试结论
**平台功能完整、架构契约有自动化守护，满足验收准则**：
- 后端测试模块 55 个、用例规模 462（以 `pytest --collect-only` 为准）。
- 解耦契约由专用测试守护（结果鸭子类型 / 注册表 = schema / 无反向 import / 通用层无 game 分支）。
- 压测与浏览器脚本覆盖核心业务路径。
- 功能需求（账号/Bot/对局/观赛/回放/人类对战/排行/赛事/社交/通知/经验/管理/数据集）均有对应测试入口。

> 返回 [doc/INDEX.md](./INDEX.md)
