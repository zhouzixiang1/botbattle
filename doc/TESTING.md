# 测试文档

> 本文档同时记录测试策略与本轮 QA 证据。测试数量随分支持续变化；发布前的权威数字必须来自目标提交上的 `pytest --collect-only -q` 与 `npm run test:e2e -- --list`，不得沿用历史手填数量。

## 1. 测试策略

采用从契约到真实浏览器的多层验证：

| 层级 | 工具 | 范围 | 目的 |
|------|------|------|------|
| **单元测试** | pytest | Store、裁判/适配层、协议、评分、通知、社交、赛制 | 验证底层逻辑与边界 |
| **集成测试** | pytest + TestClient | 鉴权、REST、SSE、WebSocket、生命周期与持久化 | 验证模块协作和错误状态 |
| **架构契约** | pytest 源码扫描 + AST + 导入序 | 游戏注册表、结果鸭子类型、无循环依赖、通用层无游戏分支/静默 game_id 兜底 | 防止解耦架构漂移 |
| **隔离端到端冒烟** | `scripts/e2e_smoke.sh` | 临时 DB/uploads/avatars/logs 下的上传→挑战→赛事；挑战必须 `completed` 且 `result.deltas` 为双数值零和 | 验证核心链路且不写 checkout/主库，`aborted` 不得假通过 |
| **API 关键链路脚本** | `scripts/api_full_test.py` | 注册回滚、播种、上传、挑战、SSE 终态 snapshot、并发、循环赛 | 验证所列 HTTP 业务链路可重复运行；不声称覆盖实时 SSE 增量或全部端点 |
| **真浏览器回归** | Playwright + Chromium | 访客/玩家/组织者/admin 的导航、表单、CRUD、赛事、实时通信 | 用真实 DOM、Console、Network 验证用户行为 |
| **多局/赛事容量脚本** | `scripts/load_test.py` / `contest_stress.py` | 多用户、多游戏、多局终态；draft 名册容量与赛制估算 | 验证所列链路与容量；默认不证明持续打满并发或真实大赛排期 |

## 2. 后端测试范围

配置由 `pyproject.toml` 指定：`testpaths=["bzplat/backend/tests"]`、`pythonpath=["."]`，必须从仓库根运行。测试模块与用例数量会随分支变化；多个独立修复分支汇合后，必须在目标提交重新执行以下命令，不能把任一子分支数字直接相加当成最终证据：

```bash
rg --files bzplat/backend/tests -g 'test_*.py' | wc -l
pytest --collect-only -q
pytest
```

主要守护面：

| 类别 | 代表性测试 |
|------|------------|
| **架构解耦** | `test_result_contract`、`test_game_registry`、`test_import_cycles`、`test_tongyong_layer_no_game_branches`、`test_db_layer_extensibility` |
| **固定规则与协议** | `test_engine`、`test_board_engines`、`test_protocol`、`test_canonical_protocol_docs`、`test_pinned_game_config`、`test_canonical_bot_protocol`、`test_judge_public`；holdem=70+20000+50/100（预检首信封同样 `max_hand=70`）、gomoku=15×15、pencil=N=6+900s；direct session 未知规则参数、赛事阶段未知/错拼字段均拒绝；棋类每个 protocol 只导出自身 API且共享实现随源码公开；schema 与 Wiki 守护唯一严格信封 |
| **可发布样例 Bot** | `test_canonical_protocol_docs` 逐字绑定 Wiki 内嵌的三游戏 C/Python 完整示例与回归源码；`test_sample_bots_runtime` 实际构建三款 C ELF 与三款 PyInstaller 单文件 ELF，校验 Linux x86_64，并让两类产物在 Traditional/LongRunning 下分别跑完整 70 手 Holdem 与两款棋类合法终局；另守护完整历史重放、精确握手和六种 Holdem 策略仅依赖标准 history 字段 |
| **认证/安全/审计** | `test_auth`、`test_store`、`test_security_logging`、`test_logging`、`test_audit_coverage`、`test_real_name`；密码重置覆盖邮箱码/管理员 token、双 Store 并发单赢家、session 删除故障整事务回滚及过期凭据不消费；限流按 IP+方法+路径分桶，版本历史 GET 不得误耗上传 POST 额度 |
| **编排/实时通信** | `test_human_match`、`test_frozen_version_failclosed`、`test_chess_clock`、`test_auto_matcher`、`test_match_seat_names`；GameSpec 棋钟覆盖 Bot-vs-Bot 与人类双方的累计/超时事件；冻结版本缺失、跨 Bot、空路径、快照异常或 SHA-256/文件大小漂移在普通/人机/赛事三路径均须 fail-closed：创建前发现则零 match/task，已排队后发现则 `aborted + version_unavailable`，两者 runner 调用数与评分副作用均为零，任务/人类占用/赛事 pairing 统一收敛；同尺寸覆盖并恢复 mtime 仍须因 ctime 变化使缓存失效并重新哈希，checksum/size 为空的真正历史版本保持可运行；另含 SSE/WS 终态关闭与 shutdown 收敛 |
| **崩溃语义** | 中途崩溃（含 human）=`completed + reason=crash`；Bot-vs-Bot 启动失败=`technical_loss`；human 启动失败=`aborted` |
| **Bot 技术故障** | `test_bot_technical_faults` 覆盖拒绝 `{a:...}`、顶层整数/裸坐标、额外字段、缺 response、非法 JSON/类型、LongRunning 缺失/错误握手、超时、三游戏、duplicate、人机隔离、评分政策、新写 `technical_incident` 事件、bounded result/replay 样本与结构化日志；预检必须走同一首回合信封。`test_matches_pagination` 覆盖只读归一化两种历史事件、敏感旧错误脱敏、REST 回放/SSE snapshot 只输出 `technical_incident`、现行 `technical_incident_*` 字段、`has_technical_incidents` 跨游戏/状态过滤、两个退役查询名显式 400 与 malformed replay；平台故障继续由 `test_audit_coverage` 断言 aborted 且不评分 |
| **二进制目标闸门** | `test_runtime`、`test_binary_visibility` 覆盖仅 ELF64/小端/Linux/x86-64 可写与可执行；历史 PE 行不迁移但 owner/admin 标记不可运行，公开候选过滤，owner/admin 激活与版本回滚均 409 且 DB 不被改写；Playwright 还用真实 PE 上传验证服务端 400 与 UI 错误态 |
| **测试产物隔离** | `test_logging` 断言从 repo CWD + tmp DB 运行时日志落临时目录，`create_app` 默认 upload root 落 DB 同目录，主 checkout 的 `bot_uploads/logs` 不接收测试标记；测试不得手写相对 `bot_uploads` |
| **赛事一致性** | `test_contest_*`、`test_scheduler_*`、`test_swiss_scale`：并发报名/派发、时间线 `opens<=closes<=starts`（含等时刻/部分 PATCH/旧脏数据/零部分写）、发布/开赛 Bot 可用性闸门、版本冻结、两阶段 prepare→bind→start 补偿、admin abort 复位 pairing 且不晋级、单侧缺 Bot 技术判负/双侧缺 Bot 阻塞、published 残缺批次恢复、后续 stage 与 Swiss/KO 后续整轮批次原子提交、Swiss 实际座位轮换、正式榜技术负破同分/完整替换与 `finished+ready=0` 重启补算、安全 finish/delete |
| **管理端安全操作** | 活跃 match 仅可经 orchestrator abort；用户/Bot/赛事存在活跃引用时拒绝硬删；批量指派做字段与归属校验 |
| **QA 隔离** | `test_qa_*`、`test_seed_test_accounts`、`test_qa_script_artifacts`、`test_load_test_seed`：拒绝 50380、主库同路径/同 inode、主 checkout 运行时写目标与错误 Vite 代理；固定凭据账号须精确匹配 namespace/用户名/邮箱/角色/密码，压测不得复用任意管理员 |
| **社交/通知/成长/站点** | `test_notifications`、`test_comments_likes`、`test_social`、`test_xp_level`、`test_tiers`、`test_load_test_seed`、`test_wiki_pages` |

## 3. Playwright 真浏览器回归

### 3.1 套件结构

`bzplat/frontend/e2e/` 当前有 4 个 spec，Playwright 静态收集为 27 条浏览器测试：

| Spec | 重点 |
|------|------|
| `public-audit.spec.ts` | 公开深链、刷新/前进/后退、404 fallback、登录错误、Network 失败后的错误/空状态 |
| `qa-regression.spec.ts` | 三 viewport 导航、表单边界、Windows PE 真实上传拒绝与历史不可运行 UI、赛事模板切换竞态与跨游戏提交闸门、挑战防重复提交、搜索、版本上传/回滚、SSE 终态/错误原因、点格棋首次计时/首回合超时 UI 契约、未知游戏 fail-closed、棋类人类动作 canonical `response` 信封、人类 Holdem WebSocket、admin abort 回归 |
| `contest-workflow.spec.ts` | 组织者创建→开放→两名浏览器用户报名→发布→开赛→完成→admin 清理 |
| `admin-audit.spec.ts` | admin 9 个 Tab、查询参数/返回数据一致性、关键保存操作与布局 |

运行前必须是 worktree 隔离实例；`beforeAll` 会校验 `/api/health` 的 `qa_instance=true`，Vite 也会拒绝代理到 50380：

```bash
cd bzplat/frontend
npm run test:e2e:install                  # 首次安装 Chromium
BZ_E2E_BASE_URL=http://127.0.0.1:5173 npm run test:e2e -- --list
BZ_E2E_BASE_URL=http://127.0.0.1:5173 npm run test:e2e -- --reporter=line
```

### 3.2 角色、视口与观察面

- 角色：访客、普通玩家、组织者、管理员。
- 视口：Desktop `1440×900`、Laptop `1280×720`、Mobile `390×844`；访客导航覆盖三档，admin 至少覆盖桌面与移动端，核心赛事流程另以 laptop 执行。
- UI：主要导航、按钮、Tab、Dialog、筛选、表单合法/非法/超长输入、重复提交、空状态、错误状态、直接子路由、刷新、返回/前进与根元素横向溢出。
- Console：持续收集 `pageerror` 与 error 级 console；未在精确白名单中的异常直接使测试失败。
- Network：跟踪 request failed 与 4xx/5xx；负向用例只豁免精确预期的请求/状态，关键写操作断言方法、路径、状态和返回结构。深链 reload/back/forward 在继续导航前须等待目标实体 ID 对应的 detail 200 与普通 HTTP quiet window，避免仅凭通用标题把仍在收尾的 fetch 留给下一段导航。
- SSE：断言终态 snapshot 转回放且不重连、`error.message` 持久展示且不被误显示为 completed、异常 `completed` 原因可见、服务端终态后流关闭；纯 mock 点格棋流还断言首次 `time_used` 用 `budget` 初始化未行动方，首次事件即 `time_out` 时显示 `0:00 + 超时`。
- WebSocket：真实人类 Holdem 流程断言单页只建一个连接、发送合法协议并进入终态；admin abort 后 runner 不得覆盖 aborted。

旧的 `browser_verify.py`、`screenshot_verify.py` 仍可做补充，但不能替代上述真实交互、Console 与 Network 断言。

## 4. 本轮 QA 执行证据

以下只记录实际已执行的证据，不把“已收集”写成“已通过”：

| 检查 | 本轮状态 | 证据/说明 |
|------|----------|-----------|
| 隔离端到端冒烟 | **ALL PASSED** | `bash scripts/e2e_smoke.sh` 在临时 DB 与运行时目录完成，未留下服务/临时产物，主文件未变 |
| API 关键链路脚本 | **50 passed / 0 failed** | 隔离运行 `scripts/api_full_test.py`，包含无 SMTP 注册回滚与所列核心 API 链路；SSE 证据为终态 snapshot，不含实时增量 |
| Playwright 收集 | **27 条 / 4 spec** | `npx playwright test --list` 实测 |
| 前端游戏契约定向浏览器回归 | **3 passed** | 独立无数据库 fake API + worktree Vite：未知 `game_id` 显示 unsupported 且不创建 Holdem canvas；Gomoku canvas 点击只发送 `{"response":{"x":int,"y":int}}`；点格棋 HUD 移入游戏包后的首回合棋钟/超时回归仍通过。Console/普通 HTTP Network 监控无非预期异常 |
| 后端协议/文档分支门禁 | **873 passed / 1 skipped / 1 warning（226.48s）** | 独立 worktree 完整执行；skip 因该 worktree 未构建 `frontend/dist`，warning 为既有 Starlette/httpx deprecation；最终整合提交仍须重跑 |
| 后端整合提交收集/完整 pytest | **待最终重采集** | 不把独立分支数字冒充整合结果；发布前用本节命令回填 |
| Playwright 完整执行 | **新增回归前基线 21 passed；27 条整合套件待重跑** | Chromium 单 worker 的旧基线为 2.3m；新增未知游戏、动作契约与 PE 拒绝回归后，必须在最终整合栈重新执行全部 27 条 |
| 前端构建 | **已通过** | `npm run build`（`tsc -b && vite build`），2560 modules transformed |

## 5. 可靠性与恢复专项

- **对局重启恢复**：上一进程遗留的 running 与非赛事 pending 对局统一中止；活跃赛事 pending 由 pairing 对账精确恢复，避免把可重派赛事粗暴 abort。
- **评分恰好一次 + 顺序屏障**：`match_rating_settlements` 的 claim 与双方 rating/history/pair_stats 同事务；失败整体回滚。覆盖 M1 评分事务失败后 M2 触发补算，必须严格按 `(created_at,id)` 得到与 M1→M2 正常路径完全一致的 rating/history；并发后处理不越序且通知/XP 不重复。启动恢复共用同一顺序机制，只补评分，任一早场失败即停止，重复调用无副作用。
- **赛事两阶段派发与阶段推进**：prepare match → 原子绑定 pairing → start task；任一步失败须删除/解绑本次精确对象。服务重启可清理未绑定幽灵、复位死 pairing，并只在整个 published 批次尚未启动时原子重建残缺排期。后续 stage 的全部 pairing（含版本快照、bye、排期）与 `current_stage_idx/status` 同事务提交；Swiss/KO 懒生成的后续轮先构造完整 rows，再在 `BEGIN IMMEDIATE` 内复核 contest/stage/上一轮/目标轮并一次追加。两类批次均须以第二行故障注入验证零 partial，重试只生成一个完整批次。Swiss 还须验证按 entry 累计实际 seat0、`color_first=1` 落库前交换 A/B、challenge 收到同一实际顺序。
- **正式榜破同分**：从 pairing 的实际 A/B 与 completed match 的 `technical_loss/winner` 识别技术负 entry；其余破同分项相同时，技术负次数较少者必须排前。
- **正式榜发布恢复**：`contest_official_results` 的清旧、全量插入与 `official_results_ready=1` 同事务；中途注入失败不得留下 partial。若进程在赛事先写 `finished` 后、榜事务提交前退出，启动对账须对 `finished+ready=0` 幂等补算，恢复后公开接口不再返回 409，重复启动不重写已就绪结果。
- **管理操作**：admin abort 必须先取消并等待 runner 收敛；赛事局保留 aborted 历史同时将 pairing 复位/重派，2 人 KO 不得固定晋级座位 0。直接改 running/completed 被拒绝。赛事 `finished/cancelled` 终态不可回退或互转；`finished` 与已有正式榜的历史记录不可删除，`published` 删除明确走“取消未开打排期后删除”。runtime 多字段 PATCH 用“整体验证→单事务写→提交后热更新”故障注入验证无部分生效。相关成功/失败管理写（含移除赛事报名）须进入 audit log。日志 API 按结构化记录过滤，ERROR/关键字命中时保留完整 traceback，上送响应不得含服务端绝对路径。
- **QA 写隔离**：后端 CLI 在 Store、日志和静态目录创建前校验 DB/端口/运行时目标；前端代理与 Playwright health guard 构成第二、第三道保护。

## 6. 发布门槛

最终验收至少需要：完整 `pytest`、`npm run build`、目标提交静态收集出的全部 Playwright、隔离 `e2e_smoke.sh` 全部通过；同时检查浏览器 Console/Network、QA 后端日志、主 DB hash/mtime 与 50380 服务未被触碰。若任一项未执行或失败，结论只能是“待验证”，不能写成“已验收”。

> 返回 [doc/INDEX.md](./INDEX.md)
