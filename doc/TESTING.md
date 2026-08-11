# 测试文档

> 本文档同时记录测试策略与本轮 QA 证据。测试数量随分支持续变化；发布前的权威数字必须来自目标提交上的 `pytest --collect-only -q` 与 `npm run test:e2e -- --list`，不得沿用历史手填数量。

## 1. 测试策略

采用从契约到真实浏览器的多层验证：

| 层级 | 工具 | 范围 | 目的 |
|------|------|------|------|
| **单元测试** | pytest | Store、裁判/适配层、协议、评分、通知、社交、赛制 | 验证底层逻辑与边界 |
| **集成测试** | pytest + TestClient | 鉴权、REST、SSE、WebSocket、生命周期与持久化 | 验证模块协作和错误状态 |
| **架构契约** | pytest 源码扫描 + AST + 导入序 | 游戏注册表、裁判结果鸭子类型、持久化结果唯一 builder、无循环依赖、通用层无游戏分支/静默 game_id 兜底 | 防止解耦架构漂移 |
| **隔离端到端冒烟** | `scripts/e2e_smoke.sh` | 临时 DB/uploads/avatars/logs 下的上传→挑战→赛事；挑战必须 `completed`，且 `result` 具有 `rounds_played`、双数值零和 `deltas` 与有限 `normalized_delta` | 验证核心链路且不写 checkout/主库，`aborted` 不得假通过 |
| **API 关键链路脚本** | `scripts/api_full_test.py` | 挑战精确接收 202 request，校验 opaque `public_id`，轮询到 claim 后再读取 Match；轮询共享 1 秒节拍、校验 HTTP/JSON 并按 `Retry-After` 退避；排行榜同样严格校验 HTTP 200、JSON 对象/对象列表，以 `rated_matches` 验证已有计分样本，并按 `ranking_min_matches` 核对 `ranking_eligible` 与 1-based `rank`，不要求已下架的榜单 `matches_played`；顺序单局 360 秒、双槽争用单局 720 秒，四局并发共享 1440 秒 claim+完成绝对截止，三场循环赛共享 1080 秒截止；并发请求由持久队列自动补槽，不再按 admission 429 客户端重提 | 尚未在目标 HEAD 实跑；取消/重试由 `test_execution_queue` 覆盖，实时 SSE 增量与全部端点仍需其他层覆盖，不得把历史结果计入本轮发布证据 |
| **真浏览器回归** | Playwright + Chromium | 访客/玩家/组织者/admin 的导航、表单、CRUD、赛事、实时通信 | 用真实 DOM、Console、Network 验证用户行为 |
| **多局/赛事容量脚本** | `scripts/load_test.py` / `contest_stress.py` | 多用户、多游戏、多局终态；load 固定 12 场必须全部接受且全部取得终态，429 耗尽、POST 异常或 waiter 超时均硬失败；draft 名册容量与赛制估算 | 验证所列链路与容量；默认不证明持续打满并发或真实大赛排期 |

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
| **架构解耦** | `test_result_contract`、`test_game_registry`、`test_import_cycles`、`test_tongyong_layer_no_game_branches`、`test_db_layer_extensibility`；守护所有完成写路径只经 `matches/result_contract.py`，公共结果固定为 `rounds_played`、`deltas`、`normalized_delta`，各 GameSpec 自行提供进度与归一函数 |
| **持久化与迁移** | `test_db_migration`、`test_store_schema_idempotency`、`test_execution_queue`；除既有结果/排名迁移外，守护旧队列孤儿与状态歧义 fail-closed、旧 active 转 `manual:` pause、execution job/attempt/control 不重复，以及 39 个现行 trigger 的同定义零 DDL/`schema_version` 稳定、过期修复、对象类型冲突与事务回滚。这里验证逻辑幂等，不把 Store 二次打开宣称为 DB 文件字节不变 |
| **固定规则与协议** | `test_engine`、`test_board_engines`、`test_protocol`、`test_canonical_protocol_docs`、`test_pinned_game_config`、`test_canonical_bot_protocol`、`test_judge_public`；holdem=70+20000+50/100（预检首信封同样 `max_hand=70`）、gomoku=15×15、pencil=N=6+900s；direct session 未知规则参数、赛事阶段未知/错拼字段均拒绝；棋类每个 protocol 只导出自身 API且共享实现随源码公开；schema 与 Wiki 守护唯一严格信封 |
| **可发布样例 Bot** | `test_canonical_protocol_docs` 逐字绑定 Wiki 内嵌的三游戏 C/Python 完整示例与回归源码；`test_sample_bots_runtime` 实际构建三款 C ELF 与三款 PyInstaller 单文件 ELF，校验 Linux x86_64，并让两类产物在 Traditional/LongRunning 下分别跑完整 70 手 Holdem 与两款棋类合法终局；另守护完整历史重放、精确握手和六种 Holdem 策略仅依赖标准 history 字段 |
| **认证/邮件/安全/审计** | `test_auth`、`test_mail`、`test_store`、`test_security_logging`、`test_logging`、`test_audit_coverage`、`test_real_name`；邮件守护默认发件人、三条模板的 Botbattle 多游戏品牌与真实渲染结果；密码重置覆盖邮箱码/管理员 token、双 Store 并发单赢家、session 删除故障整事务回滚及过期凭据不消费；限流按 IP+方法+路径分桶，版本历史 GET 不得误耗上传 POST 额度 |
| **平台通信/Bug 反馈** | `test_communications_feedback`；覆盖验证码只存短期行引用、worker 才渲染/SMTP、确定性 Message-ID、通知真相+兼容投影、participant/admin RBAC、私有用户/admin thread 详情 `no-store/no-referrer`、固定去重广播快照与二次批准、worker `BEGIN IMMEDIATE + running CAS + 消息/邮件投影` 单事务、全部渠道终态后才 completed、仅 delivery 重试也重新打开 completed、`claim→cancel→resolve` 不调用 SMTP、cancelled+sending 启动恢复不重新 queued、resolve 准入后才允许在途 SMTP 完成、admin 群发历史/详情脱敏/失败项有界幂等重试、严格诊断白名单（含主题）、captcha 访客反馈创建/认证详情/追踪 no-store、追踪回复与附件错误/缺失 token/未知编号统一 404、访客 token 与另一账号 Authorization 混用拒绝且不落附件、图片 magic+MIME+下载二次完整性校验、前端 identity epoch/AbortController/冻结认证结构守卫、追加式事件，以及二次打开不伪造旧通知会话 |
| **编排/实时通信** | `test_execution_queue`、`test_authoritative_terminal_events`、`test_human_match`、`test_frozen_version_failclosed`、`test_chess_clock`、`test_match_seat_names`、`test_qa_script_artifacts`；全来源必须先入持久队列，排队阶段没有 Match，claim 才原子建 Match/index/replay/policy/attempt；人机与其他来源共享 match slots 且占 1 sandbox unit。`test_match_seat_names` 还守护 `/api/matches`、Bot 对局历史、搜索、热门四个公共记录端点使用同一嵌套身份：每个座位都能区分“用户拥有的 Bot”与“实际真人”，真人身份不得错误复用 Bot owner，自博弈在四个入口都保持同一 Bot/owner 的两个独立座位，展示层不得任意归为 Bot 自身胜负；列表、详情与 SSE/WS 响应均用正向白名单阻止扁平 JOIN 字段、关联外键与 `_replay_events_json` 外泄，详情/SSE 仅可额外保留交互必需的 `human_seat`。`test_claim_version_loss_has_truthful_retry_and_auto_lifecycle` 守护 claim 前版本失效的 manual/human retryable、auto non-retryable 与 decision 取消；`test_claim_version_loss_backs_off_contest_pairing` 守护 contest non-retryable、pairing 复位且至少退避 30 秒。`test_terminal_runtime_retryability_is_owned_by_request_source` 守护运行期中断只允许 manual/human 通用重试，auto/contest 由各 producer/状态机负责。`test_finalize_never_releases_capacity_for_non_terminal_match` 守护非终态 Match 不能越过 settling 释放容量。其余 GameSpec 棋钟、SSE/WS 权威终态、公开字段、shutdown 收敛与真实棋类交互维度保持覆盖 |
| **崩溃语义** | 中途崩溃（含 human）=`completed + reason=crash`；Bot-vs-Bot 启动失败=`technical_loss`；human 启动失败=`aborted` |
| **Bot 技术故障与回放边界** | `test_bot_technical_faults` 覆盖拒绝 `{a:...}`、顶层整数/裸坐标、缺 response、非法 JSON/类型，同时断言带 `debug` 等额外顶层字段的响应只提交 `response`；另覆盖 LongRunning 缺失/错误握手、超时、三游戏、duplicate、人机隔离、评分政策、新写 `technical_incident` 事件、bounded result/replay 样本与结构化日志；预检必须走同一首回合信封。`test_canonical_bot_protocol` 还守护 Pencil 超时提示明确区分“ELF 已启动”和“未按 JSON 响应”，指出旧 SAU 文本协议、换行/flush 与开发文档，同时保持 8 秒硬门不变。`test_matches_pagination` 覆盖 metadata 与结构化 replay 分离、精确字段、长回放不进入详情、无 replay/畸形 JSON、漂移 index 对应物理 match 缺失 404、活动人类公开脱敏，以及列表/详情只用 JSON1 incident 投影恢复两种历史事件；同时守护敏感旧错误脱敏、REST replay/SSE snapshot 只输出 `technical_incident`、现行 `technical_incident_*` 字段、`has_technical_incidents` 跨游戏/状态过滤与两个退役查询名显式 400。平台故障继续由 `test_audit_coverage` 断言 aborted 且不评分 |
| **私有 Bot 调试** | `test_bot_debug_private` 覆盖 Traditional/LongRunning 提取、握手失败不提交、预检丢弃、64 KiB 传输/解析双闸门、超深 JSON/超长整数按 `protocol_error` 归责、4 KiB/深度/节点/容器/座位/整场上限及饱和后 O(1) 预闸门、NFC/ANSI/control/bidi 清理、敏感键/JWT/Bearer/query token/带空格或未闭合引号赋值/复合 Cookie 与 Set-Cookie/private-key 脱敏、终局广播前原子写、写失败不改结果且日志不含内容；普通双方 owner 对称权限、赛事 organizer/admin 单场权限、Bot owner 延迟到赛事终态、赛事外键缺失或身份异常 fail-closed、人类/访客/无关用户拒绝、不泄漏存在性、删除 match/bot/user 无孤儿、迁移幂等；公开 match/list/search/replay/SSE/result 与审计内容边界均有断言 |
| **二进制目标与上传容量闸门** | `test_runtime`、`test_binary_visibility`、`test_frozen_version_failclosed`、`test_mybot_versions`、`test_settings_mybots`、`test_user_search`、`test_security_logging` 覆盖仅 ELF64/小端/Linux/x86-64 可写与可执行；owner 的 `/api/bots/mine` 库存视图包含 inactive Bot 以便恢复，Store 默认与公开候选仍只取 active；纯 ASGI body limiter 守护 Bot/反馈附件/头像精确 POST 路径的 51/6/3 MiB 原始请求硬顶、Content-Length 立即 413、伪小/缺失长度的 chunk 累计、越界 chunk 不下传/后续断开、真实 disconnect 与非目标路径透传，并对三路真实 multipart 强制低阈值 rollover 后断言唯一 413、端点零调用和 spool 已关闭；Bot/头像认证先于 body receive，Bot 新建/版本共用事件循环异步单槽，首请求停在 multipart receive 时第二请求须在零 body read 下返回 `503 upload_busy`，等待中与 worker 中取消均不得早释/泄漏 permit 或饿死事件循环；四个手工 multipart 端点还须用显式 OpenAPI `requestBody` 保留 required/binary 字段和 400/413/503 等响应契约，端点只读 `MAX+1`。真实 x64 PE 必须在镜像检查及 Docker 启动前拒绝且不建立 session；Linux 镜像缺失只允许在 Bot 计时前单飞拉取并复核 `linux/amd64`，registry/拉取超时归平台故障，`docker run` 固定 `--pull=never --entrypoint /app/bot`；历史 PE 及主库同形态 `elf/空/空 + version unknown/空/空` 不迁移但 owner/admin 标记不可运行，并从公开候选、搜索、排行榜、自动排位 producer 与赛事候选过滤；owner/admin 激活与版本回滚均 409 且 DB 不被改写；无 checksum/size 的旧版本缺文件也在 job/claim 门禁 `version_unavailable`；Playwright 还用真实 PE 上传验证服务端 400 与 UI 错误态 |
| **代码唯一配置** | `test_runtime_settings`、`test_execution_queue`、`test_contest_templates`、`test_contest_template_seed`：旧 runtime KV 不能覆盖启动值，fresh app 不再 seed 同名键，runtime PATCH/admin template CRUD 为 404，只读诊断和公开模板均标记 `source=code/mutable=false`；历史模板表不 seed/对账且无法覆盖注册表；自动排位只是 producer，旧 daily/cooldown/stale/idle/reserve/max-per-round 真值全部迁移删除，唯一管理员开关是 `execution_control.auto_enabled` |
| **测试产物隔离** | `test_logging` 断言从 repo CWD + tmp DB 运行时日志落临时目录，`create_app` 默认 upload root 落 DB 同目录，主 checkout 的 `bot_uploads/logs` 不接收测试标记；测试不得手写相对 `bot_uploads` |
| **赛事一致性** | `test_contest_*`、`test_scheduler_*`、`test_swiss_scale`：并发报名/派发、时间线 `opens<=closes<=starts`、`starts_at=NULL` 的手动开赛闸门、pairing 只创建 `source=contest` job、claim 时才原子建 Match 并绑定 pairing、赛事共享份额与完成一场补一条、版本冻结、admin abort/重启补偿、后续 stage 与 Swiss/KO 批次原子提交、正式榜恢复与安全 finish/delete（含 published 赛事仍有 durable request 时拒绝删除，不能留下孤儿 job）；detail/bracket 还须守护真轮空、legacy 双 Bot pairing、历史 Bot 删除三态的 `is_bye` 相反语义，以及唯一报名身份的只读恢复不会清空阶段榜；`test_contest_showcase` 继续守护六个只读快照与真实裁判回放，不让 showcase 后台任务污染通用队列 |
| **管理端安全操作** | 活跃 match 仅可经 orchestrator abort；用户/Bot 一旦有历史或活跃参赛引用只能停用、不得硬删，赛事按状态与引用拒绝危险删除；批量指派做字段与归属校验 |
| **QA 隔离** | `test_qa_*`、`test_seed_test_accounts`、`test_qa_script_artifacts`、`test_load_test_seed`、`test_runtime_settings`：拒绝 50380、主库同路径/同 inode、主 checkout 运行时写目标与错误 Vite 代理；每个实例使用唯一 `BZ_INSTANCE_KEY`；隔离 QA 的代码能力门强制禁用自动 producer，复制库中的 `execution_control.auto_enabled` 不能绕过且开启 API 返回 409；同名 QA Bot 复用还必须满足规范路径、执行位、ELF 元数据与内容完整性 |
| **社交/通知/成长/站点** | `test_notifications`、`test_comments_likes`、`test_social`、`test_xp_level`、`test_load_test_seed`、`test_wiki_pages`；覆盖 actor/target 不存在或在 API 预检后竞态删除时，关注/收藏/评论/点赞/取消点赞都由 `BEGIN IMMEDIATE` 写事务稳定返回 404，XP 与通知无副作用；删实体/用户清孤儿且即时同步对局点赞缓存；自评论不通知自己；通知使用展示层 1-based 座位；偏好 REST 严格 boolean，浏览器忽略迟到初始 GET、按字段串行合并快速点击并让最后一次操作成为服务端/UI 真值 |
| **单游戏数值排行榜契约** | `test_numeric_ranking`、`test_leaderboard_density`、`test_pagination`、`test_response_field_whitelist`、`test_rating_rebuild`；Store/API 缺失或未知 `game_id` 必须拒绝，1-based 全局名次/百分位、Rating/RD、95% 区间、计分资格与样本概览由后端权威生成，分页名次保持全局连续，公开资格不读取 auto bootstrap 目标；评分变化只读同游戏历史快照。最近对局必须同时存在于历史 reason、同 game 索引和物理 completed 行，且该 Bot 确为任一座位参赛方。公开行不返回定性标签、内部累计分差、重复 game_id 或恒定平台三元组 |

## 3. Playwright 真浏览器回归

### 3.1 套件结构

`bzplat/frontend/e2e/` 当前静态有 6 个 spec（含新增全局执行队列 spec）；旧主线 56 条基线不能外推，最终测试条数与通过数在目标 HEAD 的统一门禁中回填：

| Spec | 重点 |
|------|------|
| `public-audit.spec.ts` | 公开深链、刷新/前进/后退、404 fallback、登录错误、Network 失败后的错误/空状态 |
| `qa-regression.spec.ts` | 三 viewport 导航与单层页面 gutter、受保护页面访客门禁（不得先发无意义 401）、表单与超长文本/横向溢出边界（含 MyBots 320px 编辑态）、Windows PE 真实上传拒绝与历史不可运行 UI、赛事模板切换竞态与跨游戏提交闸门、挑战防重复提交、搜索、版本上传/回滚、MatchViewer metadata→replay 门控/单请求/失败保留元数据、活动对局零 replay 请求、SSE 首帧失败退出 loading、未知游戏零 replay 请求、冻结 `rated/rating_reason` 与 marker `rating_settled` 分离，覆盖“预计计分/待结算/已计分/已中止未计分”四态及同 owner 中性说明、私有 Bot debug 默认折叠/纯文本安全/4 KiB 长文本移动端不溢出/无权限不请求/跨路由迟到响应隔离/新路由权限详情未返回时旧面板同步消失，以及受控 SSE 直播从事件 1 顺播/持续高频推流不饿死游标/超过 4000 条无损重连/暂停与终局不跳/显式跳转、德州有首手才显示 X/70、canonical `match_end.deltas` 驱动 MatchViewer 与 HumanPlay、Holdem 盲注/底池/all-in raise-to reducer 契约、生产回放 `20260809205002-ede64ea8` 的 70 手真实结算与末手完整事件 HUD、多比例三列/横排/堆叠/折叠/sticky/零横溢出、复式 140 手/换座/leg 边界与真人 HUD 无底牌文本，Pencil 非法终局 2:0 归一、点格棋首次计时/首回合超时 UI 契约、Pencil 横纵边端点与 `2×cell` 格几何、线上 `(5,5)` 格心事故 fixture 零发送、`pass=1` 禁棋盘并只发 `(-1,-1)`、`move/pass/turn` 逐事件行动方与强制让行真值、方向键选择/坐标播报/Enter canonical 提交、同长度 snapshot scene 替换与倒计时重渲染不中断动画、320/390/844/1024/1312/1600/1920/2048/2560 多比例方形棋盘与滚动/时序协作、局面概览数值、Safari 截图同尺寸的 2048×1024/1152 Chromium 布局回归（观赛与真人完整棋盘、长时序独立滚动）、真实 Pencil 人机连续三条合法边（自动处理 Bot 成格让行）、未知游戏 fail-closed、棋类人类动作 canonical `response` 信封、人类 Holdem WebSocket、admin abort 回归 |
| `contest-workflow.spec.ts` | 组织者创建→开放→两名浏览器用户报名→发布→开赛→完成→admin 清理 |
| `admin-audit.spec.ts` | admin 7 个业务 Tab、查询参数/返回数据一致性、关键保存操作与布局；唯一 `execution_control.auto_enabled` 开关的双向切换、manual/contest 不受影响文案和全来源长文本队列；赛事时间按状态收口、空值/显式 `NULL`、保存失败原位反馈、真实隔离库重载与 audit、Dialog 滚动与三视口；断言不存在运行时/赛制模板 Tab 与对应写 API |
| `leaderboard-density.spec.ts` | 访客/普通用户/组织者/admin × Desktop/Laptop/Mobile；每个 context 精确断言 `/api/auth/me` 的匿名状态或 username/role，非访客三类各只登录一次并复用独立 storageState；全来源 active/queued、双容量与长 Bot/所有者/暂停原因折行；桌面七列表头、移动列表卡、公开排名/无名次计分样本分区、滚动中可操作的 Radix tabs/表头 sticky、慢响应切游戏立即清旧概览、根元素零横溢出、三游戏显式请求与 Console/Network clean |
| `execution-queue.spec.ts` | Challenge 的 202 request、同一 `public_id` 轮询/刷新恢复、queued 取消、interrupted 重试、Match 出现后跳转；排行榜全来源队列/双容量、安全暂停、离线 stale 与公开字段白名单。文件存在不代表目标 HEAD 已执行通过 |

运行前必须是 worktree 隔离实例；`beforeAll` 会校验 `/api/health` 的 `qa_instance=true`，Vite 也会拒绝代理到 50380：

```bash
cd bzplat/frontend
npm run test:e2e:install                  # 首次安装 Chromium
BZ_E2E_BASE_URL=http://127.0.0.1:5173 npm run test:e2e -- --list
BZ_E2E_BASE_URL=http://127.0.0.1:5173 npm run test:e2e -- --reporter=line
```

### 3.2 角色、视口与观察面

- 完整的角色 × 页面 × 操作清单见 [BROWSER_ACCEPTANCE.md](./BROWSER_ACCEPTANCE.md)；新增页面或角色能力时必须先更新该矩阵，再补自动化落点。
- 角色：访客、普通玩家、组织者、管理员。
- 视口：Desktop `1440×900`、Laptop `1280×720`、Mobile `390×844`；访客导航与 Admin 全部业务区均覆盖三档，核心赛事流程另以 laptop 执行。德州生产回放 HUD 另覆盖 `2560×1080`、`1920×1080`、`1760×900`、`1600×900`、`1536×900`、`1366×768`、`1280×800`、`1024×768`、`390×844`、`320×568`；断言自定义 `3xl=1760px` 及以上三列、1280–1759px HUD 横排在牌桌上方且时序在右、窄屏堆叠、16:9、时序折叠/页面滚动 sticky、真人 HUD 无底牌文本和根元素零横向溢出；复式 fixture 另断言 2×70=140 手总进度、第二局物理座位映射、leg 边界不沿用旧动作和无伪整场胜者。Pencil 观赛/人机另用 Chromium 覆盖 `2560×1080`、`1920×1080`、`2048×1024/1152`、`1600×900`、`1536×1080`、`1366×768`、`1312×700`、`1024×768`、`844×390`、`390×700`、`320×568`；断言 21:9、16:9、4:3、短横屏和手机竖屏按三栏/双栏/堆叠重排，方形 canvas 同时受可用宽高约束，局面概览数值与当前事件一致且不高于棋盘、长时序独立滚动、折叠后不保留空右轨、根元素零横向溢出且页面上下滚动后仍可用。真实 WebKit 结果必须单独记录，不把同尺寸 Chromium 结果冒充其他引擎证据。
- 图片识别：自动化为每个页面保存首屏、滚动中段、页尾的 Desktop/Mobile 截图；Admin、Challenge、排行榜、对局/人机、赛事详情、邮箱/反馈再保存 Laptop 截图。逐图审查统一 token/字号/间距、组件比例、有效信息密度、空白、对齐、文字溢出与不自然换行、sticky 层级、内外滚动和滚动后的整体构图；有问题时修改共享布局/原语并在相同视口与滚动位置复拍。DOM、bounding box 或无横溢出断言不能代替这一步。
- UI：主要导航、按钮、Tab、Dialog、筛选、表单合法/非法/超长输入、重复提交、空状态、错误状态、直接子路由、刷新、返回/前进与根元素横向溢出。
- 私有 debug：先验证授权详情才请求独立 API；默认折叠、两座位 Tab、turn/leg、复制入口、4 KiB 连续长文本在 390px 与页面滚动后不横溢，HTML/Markdown/URL 只作文本；无权限面板与网络请求均不存在。
- Console：持续收集 `pageerror` 与 error 级 console；未在精确白名单中的异常直接使测试失败。
- Network：跟踪 request failed 与 4xx/5xx；负向用例只豁免精确预期的请求/状态，关键写操作断言方法、路径、状态和返回结构。深链 reload/back/forward 在继续导航前须等待目标实体 ID 对应的 detail 200 与普通 HTTP quiet window，避免仅凭通用标题把仍在收尾的 fetch 留给下一段导航。
- SSE/回放加载：终态先取 metadata、再且只取一次 `/replay`；回放失败保留已渲染的身份/结果。活动对局只开 EventSource，不请求 `/replay`；首帧即失败不得永久 loading，原生重连后 snapshot 仍可恢复。未知游戏不下载事件；id 切换清旧状态并中止在途 metadata/replay。除终态 snapshot 转回放且不重连外，以可控 EventSource 批次断言初始停在事件 1、新事件先增加分母再逐条播放；另以 50ms 连续推流覆盖事件增长频率持续高于 700ms 播放速度，游标仍须在推流结束前前进，防 timer 被 `total` 变化反复取消。初始 4101 条、重连增长到 4351 条的回归同时断言事件 1、暂停游标和新增尾部全部保留，不允许后缀裁剪。暂停与普通重连 snapshot 不改变游标、终局不跳尾且可继续顺播、显式跳结局/重播从事件 1 生效；德州只有看到 `hand_start` 后才显示 X/70，零手 `admin_aborted/platform_error` 不显示假进度。后端重连测试须证明运行 snapshot 来自完整内存前缀而非节流落库点，经过与 REST 相同的字段投影/真人座位脱敏；终态状态必须忽略尚未释放的旧前缀并从 Store 合成唯一终局。另断言公开 error 只按稳定 reason 显示中文且恶意/缺失 reason 的 message 不参与语义、运行态空 reason 不预称 completed、异常 completed 原因可见、服务端终态后流关闭；纯 mock 点格棋流还断言首次 `time_used` 用 `budget` 初始化未行动方，首次事件即 `time_out` 时显示 `0:00 + 超时`。
- WebSocket：真实人类 Holdem 流程断言单页只建一个连接、发送合法协议并进入终态；真实 Pencil 人机连续三回合从 canvas 合法边中心点击，逐帧核验 canonical response 与裁判 `move`，并以线上事故棋盘 fixture 证明点/格心/已占边/棋盘外为零发送；成格后的 `pass=1` fixture 断言 canvas 为 inactive、点边零帧、确认让行精确一帧 `(-1,-1)`；admin abort 在取消 runner 后仍须向既有连接送达权威终态，且 runner 不得覆盖 aborted。
- Admin 赛事时间：`draft` 可改开放/截止/开赛，`open` 只可改未来的截止/开赛，`published` 只可改开赛，其余状态只读；无值时控件保持空白并展示手动语义，已有自动开赛时间可通过开关提交显式 `starts_at:null`，与省略字段保留旧值区分。`published` 尚无 `match_id` 时按发布轮次错峰规则在同一事务重排当前阶段 pending pairing；显式 `null` 同步清空，已有绑定或任一写入失败时赛事与逐场排期均不部分更新。Dialog 直接展示保存错误，真实隔离库用例还验证重载后的 `NULL` 与成功 audit 记录。

### 3.3 文档边界审计

公开 `wiki/` 只保留 `INDEX`、统一协议、Bot 上手/编译、平台使用与三游戏规则/示例；未出现 worktree、PR、仓库构建脚本、部署、pytest/npm 等平台工程说明。`BOT_DEV.md` 对 Windows/Linux/macOS 的 C/Python 指南最终都产出 Linux x86_64 ELF，未承诺运行 PE、Mach-O 或源码。协议中的 `BOTZONE_REQUEST_KEEP_RUNNING` 是必须逐字输出的 LongRunning 握手常量，不是新旧平台对比或兼容入口。

工程架构、测试、部署、运行配置、裁判代码位置和本轮清理盘点均在 `doc/`。机器可读 `contracts/` 当前只为 Holdem 提供完整 payload schema；Gomoku/Pencil 仅在 Wiki 与运行时测试中约束 payload，属于 P2 待补的契约覆盖差距，但不代表存在第二套协议。

旧的 `browser_verify.py`、`screenshot_verify.py` 仍可做补充，但不能替代上述真实交互、Console 与 Network 断言。

## 4. 历史执行证据与当前发布门禁

以下旧行只记录它们各自产生时的提交/分支证据，不能外推到当前全局执行队列重构。当前目标 HEAD
必须重新跑完整门禁；“测试存在”“历史通过”或定向运行均不能写成当前发布通过：

| 检查 | 本轮状态 | 证据/说明 |
|------|----------|-----------|
| 全局执行队列重构发布门禁 | **待验证** | 当前文档只定义目标维度，尚未取得同一目标 HEAD 的完整 `pytest`、前端 build、Playwright 浏览器矩阵、Console/Network/后端日志与安全收尾证据。必须覆盖 202 持久请求、刷新恢复、取消/重试/RBAC、混合来源容量与 aging、人机 1 sandbox unit、赛事共享份额、Docker namespace 清理/不确定暂停/重启补偿、旧 active 手工 pause、`legacy-unverified` rebuild No-Go、39 trigger schema-idempotency；未执行项保持“待验证” |
| 隔离端到端冒烟 | **ALL PASSED** | 本分支 `bash scripts/e2e_smoke.sh` 在 `/tmp` 临时 DB 与运行时目录完成，退出后回收自己的服务和目录；写目标不在主仓库 |
| API 关键链路脚本 | **50 passed / 0 failed** | 全新临时库隔离运行 `scripts/api_full_test.py`，包含无 SMTP 注册回滚、全局并发上限精确接纳、超额 429 与释放后补槽等核心 API 链路；SSE 证据为终态 snapshot，不含实时增量 |
| Playwright 收集 | **待最终整合门禁回填 / 5 spec** | 主线基线为 56 条；本次整合新增私有 Bot debug 回归，功能收口后统一执行 `npx playwright test --list` 和完整套件，不把分支历史静态数冒充目标 HEAD 证据 |
| 通信/Bug 最小定向回归 | **32 passed / 1 warning（8.47s）** | `test_communications_feedback.py + test_auth.py + test_notifications.py + test_mail.py + test_security_logging.py::test_captcha_not_logged_in_plaintext`；使用临时 SQLite/附件目录，未启动服务、未连 SMTP；warning 为既有 Starlette/httpx deprecation |
| 通信身份/缓存增量快检 | **9 passed / 1 warning；tsc 通过** | 本轮仅执行 `test_communications_feedback.py`、`python -m py_compile` 与 `tsc -b --pretty false`；新增私有 thread 缓存头、混合访客 token/认证用户拒绝、前端身份 epoch/冻结认证结构守卫。未执行 build 或 Playwright，不记为浏览器证据；warning 为既有 Starlette/httpx deprecation |
| 通信生产副本迁移 | **通过** | 主库只读 `cp --reflink=never` 到工作树后 inode `12754843 → 19286808`；新代码连续打开副本两次，原 31 张表/13111 行按迁移前原列的全行哈希与行数均不变，第二次 schema hash 稳定；新增 10 表全为 0 行，旧通知投影列全 NULL，最终 41 表、`integrity_check=ok`、`foreign_key_check=0` |
| Holdem 响应式最终整合门禁 | **pytest 1096 passed / 1 warning（234.38s）；Playwright 56/56 passed（4.5m）；build 2563 modules** | 基于 `main@7c63fb843` 的单一整合树；完整浏览器套件含四角色排行榜、Pencil 响应式仪表盘、真实 `20260809205002-ede64ea8` Holdem 回放、复式 140 手与真人脱敏 HUD；Console/Network 监控无非预期异常。warning 为既有 Starlette/httpx deprecation |
| Holdem 数据库影响 | **无 schema/迁移/业务写路径变更** | 仅前端归约、画布、布局、测试与文档；浏览器写操作全部指向独立 inode 的 worktree 副本库，最终 `integrity_check=ok`、`foreign_key_check=0` |
| 观赛/视觉复审定向浏览器回归 | **9/9 passed（42.8s）** | 隔离 QA backend `50386` + worktree Vite `5178`；4 条新增/编辑态风险用例与 5 条既有终态/顺播/重连/零手协议故障回归分两组实测，覆盖 320px MyBots、50ms 连续 SSE、4101→4351 无损 snapshot、零手 admin/platform 中止、终局不跳及 Console/Network |
| 前端游戏契约定向浏览器回归 | **3 passed** | 独立无数据库 fake API + worktree Vite：未知 `game_id` 显示 unsupported 且不创建 Holdem canvas；Gomoku canvas 点击只发送 `{"response":{"x":int,"y":int}}`；点格棋 HUD 移入游戏包后的首回合棋钟/超时回归仍通过。Console/普通 HTTP Network 监控无非预期异常 |
| 权威终态定向浏览器回归 | **1 passed** | 隔离 QA backend + worktree Vite；mock SSE/WS 只发送 canonical `match_end {winner,reason,deltas}`，MatchViewer 与 HumanPlay 均正确显示胜者和 Holdem 累计净筹码，Console/Network 无非预期异常 |
| 权威终态后端定向回归 | **70 passed / 1 warning（29.94s）** | `test_authoritative_terminal_events` + `test_audit_coverage` + `test_human_match` + `test_engine`：真实 70 手 Holdem、duplicate、协议技术负、启动崩溃、平台错误、SSE 队列与真实 TestClient WebSocket；replay/live 各一条相同 canonical 终态，广播时 Store/GET 已完成。warning 为既有 Starlette/httpx deprecation |
| Pencil 重基最终定向回归 | **浏览器 7/7 passed（15.4s）** | 基于 `main@5a2662f` 的最终整合提交；覆盖几何、生产事故夹具、强制让行、键盘合法边、等长 snapshot/父级重渲染动画、回放布局与真实裁判多步。后端规则与协议回归同时包含在本表的最终完整 pytest 中 |
| Pencil 宽屏短视口最终门禁 | **pytest 1093 passed / 1 warning（222.70s）；Playwright 50/50 passed（3.5m）；build 2562 modules** | 基于 `main@b210067` 的独立副本栈 `50384/5176`；附件对局 `20260810143624-4149d6a3` 绑定的回归覆盖 2048×1024/1152 观赛与真人首屏、640px 完整方形棋盘、206 条长时序内部滚动、外圈边 1px DPR 取整容差及 320/390/1312 既有布局。完整浏览器套件含四角色；warning 为既有 Starlette/httpx deprecation |
| PR154 中性结果 pytest 基线 | **1091 passed / 1 warning（220.72s）** | `main@5a2662f` 合并前的中性结果契约目标提交完整实测；公共结果、迁移与正式榜契约均保留，warning 为既有 Starlette/httpx deprecation |
| PR154 中性结果 Playwright 基线 | **43/43 passed（3.2m）** | 隔离 QA 栈 `50383/5175`、Chromium 单 worker。该历史版本曾暴露排行榜逐行配置请求风暴并完成同游戏 singleflight 修复；现行数值排行榜已删除该整套标签请求。该行不冒充点格棋重基结果 |
| 生产副本中性迁移 | **通过** | 2026-08-10 15:03 +08:00 从主库只读 `cp` 到独立 inode（12748789 → 4197548），用 PR154 代码迁移并二次打开：integrity=`ok`、FK=0；三对局表 523/441/453、completed 519/441/453，ratings 76、pair 209、replay 1895、stage/official 33、settlement 1194 行均不变。正式排名投影 `contest_id,entry_id,stage_idx,rank` hash 前后均为 `d1d5a83c2ed4d1dc240556668cc739b4bd3f61f81f280bee4f7921587509f620`，settlement 按物理列全行 hash 前后均为 `43f8de1554cdecaa8b673361ebdd65fc4785a97a510d1e013eaf9ec1f1918931`。完成结果缺字段/旧键/归一值错误、非完成态伪公共字段、正式榜旧键均为 0；新中性列存在，pair/replay 与旧筹码列均删除 |
| 自动排位评分重建生产副本演练 | **dry-run → apply → verify → no-op 通过** | 2026-08-10 20:40 +08:00；只在主库 `cp` 的独立 worktree 副本完成正常 schema 迁移与维护，主库/50380 未写。单快照 dry-run 识别 1194 个权威 `settled_order`（1163 rated、31 neutral）、88 个 Bot，完整传播影响 28 个 Bot 与 4 个正式名次；source/plan/rebuilt digest 分别为 `0332ea50596bb0e10b299f0f2997a5266c4d6dae4e42eb8772ccae74063d29fa` / `19f8f5484de070fbf4174375b0c5ed4d9f8b81ddc93c5c8649574f04edc103f4` / `22e22434b1e1c224e32fb5f169180aff67d4666cfe30b90a031090285fd44994`。逐字节冷备与目标通过 integrity=`ok`、FK=0、完整业务 digest `37d7b22958197cdeef7b19b0581c65e1d4404c8792c76606551b477dc91ea19b` 和文件 digest `631223e2e56f49236855f426b96d6feddd252f64ccf730e39999bea7786debb0` 后首次 apply 成功，独立 `--verify` 得到投影/水位一致、sequence next=`1195`。再从提交后副本制作新冷备并执行同三摘要 apply，返回 `applied=false/no_op=true/rows_written=0`；目标文件 size、mtime 与 SHA-256 `dab6fc9649cf7a8a068a0ed4b68cac363874c6146c7b5291eb15d2b0f0a87b3c` 前后完全不变。该演练只证明离线命令和真实数据形态，不能替代最终完整 pytest/build/Playwright |
| 重基最终完整 pytest | **1093 passed / 1 warning（226.20s）** | Pencil 提交重基到 `main@5a2662f` 后，在同一最终代码 HEAD 完整执行；warning 为既有 Starlette/httpx deprecation |
| 重基最终完整 Playwright | **50/50 passed（3.4m）** | 隔离 QA backend `50382` + worktree Vite `5174`、Chromium 单 worker；覆盖 PR154 中性结果契约，以及 Pencil 事故链路、无效命中零请求、强制让行、键盘、多视口和 Console/Network |
| Pencil 响应式仪表盘最终门禁 | **pytest 1093 passed / 1 warning（231.60s）；Playwright 50/50 passed（3.9m）；build 2562 modules** | 基于 `main@fffdd9c` 的独立 QA backend `50384` + Vite `5176`，主库复制为独立 inode；真实事故对局 `20260810143624-4149d6a3` 的 54 步/206 事件 fixture 与技术终局 fixture 覆盖 2560/2048/1920/1600/1536/1366/1312/1024/844/390/320 多比例、实际格子归属、连边构成、三栏顶部对齐且概览不越出棋盘、短屏双栏、折叠回收空轨、手机堆叠、页面滚动与时序内部滚动；完整 50 条含四角色与三游戏，warning 为既有 Starlette/httpx deprecation |
| 排行榜重构最终完整 pytest / build | **1096 passed / 1 warning（228.65s）；build 2562 modules** | 排行榜提交重基到 `main@22c4c645` 后，在同一最终代码 HEAD 完整执行；包含上述 Pencil 响应式仪表盘、中性结果基线及新增的单游戏排行榜、分页全局名次、相邻评分变化、recent-match game/物理表/completed/实际参赛方四重校验回归；warning 为既有 Starlette/httpx deprecation |
| 排行榜重构最终完整 Playwright | **53/53 passed（3.8m）** | 隔离 QA backend `50382` + worktree Vite `5174`、Chromium 单 worker。保留全部 Pencil/中性结果/响应式仪表盘基线并新增 Desktop `1440×900`、Laptop `1024×768`、Mobile `390×844` 三条密度回归；每条逐一覆盖访客、普通用户、组织者、管理员，并精确断言 `/api/auth/me`：访客为 401 且无本地会话，另外三类 username/role 与账号一致、各真实登录一次后复用独立 storageState。页面/请求/滚动中切 tabs/慢网清旧概览/sticky/长文本/无横溢出/singleflight/Console/Network 断言不降级 |
| Admin 浏览器定向回归 | **9 passed（24.7s）** | `admin-audit.spec.ts` 全量；含状态边界、Dialog 内错误、真实隔离 DB 的手动开赛 `NULL` 重载与成功 audit 证据 |
| Admin 时间定向后端回归 | **26 passed / 1 warning（8.04s）** | `test_admin_contest_status.py`；覆盖状态边界、发布态轮次错峰重排/清空、已有 match 拒绝、强制 SQLite 写失败整事务回滚；warning 为既有 Starlette/httpx deprecation |
| QA 能力门 + Admin 联合回归 | **旧实现历史基线；当前待重跑** | 现行目标测试落点为 `test_execution_queue.py` + `test_runtime_settings.py`；须重新覆盖 QA 零自动生产、复制开关不可绕过、开启返回 409、`execution_control.auto_enabled` strict boolean/RBAC/audit，并验证 manual/human/contest 不受开关影响 |
| 历史前端构建 | **旧 HEAD 已通过；当前待重跑** | 旧证据为 `npm run build`（`tsc -b && vite build`），2562 modules transformed；不代表当前执行队列前端已构建通过 |
| 浏览器 QA 写隔离 | **历史通过；当前仍须复核** | 历史栈 50382/5174 的 `/api/health` 返回 `qa_instance=true`，worktree DB 与主库 inode 不同；该证据早于全局执行队列。当前重跑还须使用唯一 `BZ_INSTANCE_KEY`，确认生产 50380/主库/其他 namespace 未被测试触碰，并在结束后核对进程、端口和容器 label |
| 赛事演示快照 390px 四身份定向验收 | **通过（定向实测）** | 独立副本栈 `50386/5178`、Chromium `390×844` 依次以访客、普通用户、赛事 owner、admin 打开公开快照并滚动/切换阶段；只读标识与禁用原因可见、无写控件、根元素无横向溢出。draft 仅 owner/admin 成功打开，访客/普通用户为 404；本行不冒充完整 Playwright 套件。 |
| QA 后端日志 | **历史通过；当前待重查** | 旧重基栈曾逐项检查 `app/access/audit` 与服务输出；当前执行队列目标还必须检查 dispatcher 状态转换、Docker cleanup/重试、取消/补偿与自动 producer，无 5xx、非预期 409、ERROR 或 Traceback |
| 私有 debug 生产副本迁移 | **通过** | 最终 `main@fffdd9c` 主库只读真相源 inode `12748789` 经独立复制得 inode `4197763`；在临时副本上用目标代码迁移并二次打开，迁移前 31 张既有表逐表行数与全行 SHA-256 均不变，新增 `match_debug_sessions/entries` 两表均 0 行，迁移后共 33 表、39 个具名索引，integrity=`ok`、FK=0；临时目录退出即回收，主库未打开写事务 |
| 私有 debug 重基最终完整 pytest | **1113 passed / 1 warning（233.21s）** | 目标分支重基到 `main@fffdd9c` 后在最终代码完整执行；含 PR156 全部 Pencil 回归与 debug Traditional/LR、真实 StreamReader 64 KiB 边界、超深 JSON/超长整数归责、复合 Cookie/Set-Cookie 脱敏、达到条数或字节上限后不再调用 sanitizer、赛事 FK 缺失 fail-closed、双方 owner/organizer/admin/赛事延迟权限、公开 list/search/replay/SSE 边界、迁移/删除和写失败不影响对局回归；warning 为既有 Starlette/httpx deprecation。独立只读安全复核结论为 Ready，P0/P1/P2 均为 0 |
| 私有 debug 定向 Playwright | **1/1 passed（2.6s，完整套件内）** | 默认折叠、座位切换、4 KiB 连续长文本、390px 滚动后零根溢出、HTML/URL 不执行不可点、无权零 debug 请求；新路由权限详情被故意阻塞时旧面板同步消失，上一局迟到的私有响应也不渲染 |
| 私有 debug 重基最终完整 Playwright | **51/51 passed（3.5m）** | 最终 `main@fffdd9c` 快照的隔离 QA backend `50383` + worktree Vite `5175`、Chromium 单 worker；PR156 所含 50 条全部通过，第 51 条覆盖私有 debug；同轮再次通过 Holdem 顺播/终局、Pencil 短视口/事故夹具/强制让行/键盘/真人合法连边、四身份与三视口 Console/Network 守卫 |

## 5. 可靠性与恢复专项

- **执行请求与重启恢复**：`test_execution_queue` 须守护 queued 不创建 Match；claim 在同一事务建 Match/index/replay/policy、绑定 pairing 并写 attempt；`starting/running/settling` 都占双资源容量。`test_claim_version_loss_has_truthful_retry_and_auto_lifecycle` 精确守护 manual/human=`interrupted+retryable`、auto=`cancelled+non-retryable` 及 auto decision 同步取消；`test_claim_version_loss_backs_off_contest_pairing` 守护 contest=`cancelled+non-retryable`、pairing 回 pending 且至少退避 30 秒；`test_contest_and_auto_requests_are_admin_mutations_only` 还须守护管理员取消 queued contest 后 pairing 同样至少退避 30 秒。`test_terminal_runtime_retryability_is_owned_by_request_source` 守护运行期 interrupted 仅 manual/human 可通用 retry、auto/contest 不可复活。`test_finalize_never_releases_capacity_for_non_terminal_match` 证明 settling 对应非终态 Match 必须 fail closed。Docker 专项还要守护 create intent 先写 journal、同 boot 双零不放行、迟到 token 容器精确清理、host boot 改变后的双零收敛、preflight/execution/cleanup 共用跨进程 flock、dispatcher 周期检查等待同一 flock 后才判断 journal（正常 live launch 不得误暂停）、所有 worker 的预检单槽 admission（取消/异常释放）、start CLI 取消后排空、Traditional 每回合按精确 slot/name/launch-token 立即清理且 label 不一致时 fail closed、journal 未闭合时 resume/claim fail closed、连续三次运行失败产生 1/2/4 秒而非每秒 attempt；普通无错误重启保持即时。启动只有在当前 instance label/name/token 连续确认清零且 journal 闭合后才补偿：无公开事件的 starting 原位回队，有事件的 attempt 保留 aborted 审计并标 interrupted，终态未清理者回 settling；Docker 结果不确定则 pause 且不释放容量。
- **持续公平自动排位**：自动排位仅是 `source=auto` producer，`test_execution_queue`/`test_runtime_settings` 须覆盖唯一开关关闭后不生成/claim auto、在途自然结束、预告保留，manual/human/contest 不受影响；游戏与 bootstrap/established lane、owner/Bot/pair/座位服务计数只读 auto 专属状态，永久 decision 审计映射到通用 job，不形成第二套 dispatcher 或 admission。
- **评分资格、恰好一次与顺序屏障**：job 创建事务冻结 `rated/rating_reason`；同 Bot/同所有者挑战明确中性且 settlement marker 存在，但 rating/WLD/history/pair 不动，不同 owner challenge 保持 rated。DB 触发器阻止在途/完成未结算的同 Bot 计分重叠。完成事务冻结连续 `settled_order`，settlement marker 与双方 rating/history/pair_stats 同事务；失败整体回滚，恢复只按序号且任一早场失败阻断后续，通知/XP 不重复。
- **离线排行榜重建与在线 mutation guard**：`test_rating_rebuild` 覆盖显式只读事务、三摘要、Bot universe 漂移、rated/result 严格契约、绝对路径/停服/冷备/二次 DB 门禁与故障全回滚。apply 的 No-Go 是 `execution_control=stopped + accepting=0`、没有 `starting/running/settling` execution job 且无 running Match；存量 `legacy-unverified` 必须先离线 rebuild。另有两条新库边界回归：创建前无业务表的全新库自动认证规范空投影并在新 Bot 交易后保持 current；任何既有库即使被手工置回 `legacy-unverified` 也不会在重开时被静默认证。只有 `rating-rebuild` 语义已一致的二次 apply 承诺字节/mtime/rebuilt_at zero-write；普通 Store 二次打开不作该承诺。
- **trigger schema-idempotency**：`test_store_schema_idempotency` 须精确断言 39 个 trigger 同定义二次打开零 trigger DDL、`schema_version` 不变；过期定义只修复一次，identifier/对象类型冲突、创建后定义漂移均抛错并事务回滚。该维度与业务数据/hash/完整性副本审计分别记录。
- **排名资格/对手统计一致性**：公开资格由 `RANKING_MIN_RATED_MATCHES` 独立驱动 leaderboard 与 Bot profile，不读取 `AUTO_MATCH_BOOTSTRAP_TARGET_MATCHES`；无资格样本 `rank=null`，公开名次按 Rating、计分场次、Bot 键稳定排序。每次 rating settlement 同事务递增 `pair_stats.samples`，并断言其恒等于胜+负+平；迁移回归修复历史零值。
- **通知偏好布尔边界**：Store 单测保留 SQLite 0/1 断言，HTTP 测试断言 GET/PUT 四字段只返回 boolean、字符串布尔被拒绝；浏览器抓取 Switch 的单字段 boolean PUT，并覆盖迟到初始 GET、同字段快速反复点击、旧账号请求迟到三类竞态，刷新后服务端与 UI 都以最后一次操作为准。
- **通信投递恢复**：注册/重置在 SMTP 未配置时仍须持久化用户、code row 与 queued delivery；码不得出现在 payload/message/Admin API/日志。worker 要覆盖普通/活动父广播的 `sending→queued` 恢复、非活动父广播的 `sending→cancelled`、指数退避、达上限 failed、过期 cancelled、确定性 Message-ID；`claim→cancel→resolve` 必须零 SMTP，只有 write-locked resolve 准入提交后的供应商调用才进入 at-least-once 窗口。
- **广播与 Bug 隐私**：预览 token/hash 同时锁定受众 ID 快照、主题、正文和渠道，重复用户只计一次；approve 不重解析受众。受众投影需证明 running CAS、消息/站内投递/邮件与 recipient 结算在同一写事务，取消前后都无检查后发送窗口；broadcast 完成须等待 recipient 与全部渠道终态，delivery-only 重试须清空完成时间并恢复调度。Bug 请求的未知字段必须 422，路由去 query/fragment，原始 UA/cookie/token/email/实名/路径/stderr/debug/底牌不入库；访客创建、认证详情和私有 communications thread 响应禁缓存，附件的未知编号、缺失/错误 token 以及 guest token + 登录身份混用均统一 404。附件要求独立 multipart、magic/MIME/尺寸/hash/路径隔离与 owner/token 权限；前端提交/回复/多文件上传须冻结发起身份并在每个 await 后复核 identity epoch，身份变化中止剩余请求且迟到操作不得回填选择/列表/loading。EXIF 剥离未纳入本轮契约，不得把 magic/hash 校验描述成元数据清洗。
- **终局原因展示**：浏览器以正常 `five/score/majority`、异常 `illegal/protocol_error/platform_error` 和未知 completed 历史码作对照，断言 MatchViewer、HumanPlay、admin 对局表的 `{label,tone}` 一致，内部英文码不泄漏，时间线保留游戏 `describeEvent`；点格棋 `illegal/time_used/technical_incident` 均转成含展示座位与决策/用时的中文；同一 SPA 内从点格棋人机终局切到五子棋人机终局，验证旧 Scene 不会跨 renderer 复用导致画布崩溃。
- **私有 Bot debug sidecar**：动作历史、结果和公共事件必须与无 debug 时逐字等价；清洗/限额/写失败均不可影响判决。授权在同一 DB 快照判定并读取，赛事 owner 延迟到整赛终态；响应 no-store，审计仅记元数据。浏览器对长连续文本、恶意 HTML/Markdown/URL、座位切换、页面滚动和无权限零请求做真实 DOM/Network 断言。
- **关注/收藏竞态**：Store 直接覆盖 actor/target 任一缺失；API 故障注入在预检查后删除目标，follow/favorite 必须稳定返回 404 且不留下关系，证明最终存在性检查与写入位于同一事务。
- **赛事队列与阶段推进**：可执行 pairing 先写 `source=contest` job，claim 才原子创建/绑定 Match；未取得容量的 pairing 保持 `pending + match_id=NULL`。赛事共享份额、取消/中断解除绑定、单场完成回写与补下一条都须与 manual/human/auto 混排回归。后续 stage 的全部 pairing（含版本快照、bye、排期）与 `current_stage_idx/status` 仍同事务提交；Swiss/KO 懒生成批次继续以故障注入守护零 partial。
- **演示快照**：六个 key 必须各唯一且状态、标题、唯一 marker、游戏、模板、专用名册、pairing/match 数量符合清单；published 保持 `starts_at=NULL + 24 pending + 0 match`，running 固定为 `4 completed + 20 pending + 0 active`，rest 固化 24 场与 12 条阶段榜，finished 固化 24+7 个互不复用的真实 Match/回放、连续 1..12 正式名次。三档 checksum 锁定 Bot 在并发 1/2 下必须强双座位胜中、中双座位胜弱且轨迹一致；rest/finished 每组积分精确 8/4/0，同一有序 Bot 对跨 running/rest/finished 的归一轨迹一致，Top 8 七场均决胜。59 场逐一要求无技术负/故障事件且数据库终局与唯一末尾 canonical `match_end` 一致；12 Bot 全部 inactive，管理统计排除 6 赛事/59 局/13 用户/12 Bot。实际副本执行还须连续通过 `seed → seed（全跳过）→ verify`，再在重写二进制路径的 DB/上传目录复制品上验证 rollback 在坏质量、缺回放/预期文件、partial key 或 active Bot 位下仍可清理，同时对 active Match、未知对象、symlink、foreign 引用 fail-closed；最后检查外键/完整性，不得拿主库做回滚演练。
- **正式榜破同分**：从 pairing 的实际 A/B 与 completed match 的 `technical_loss/winner` 识别技术负 entry；其余破同分项相同时，技术负次数较少者必须排前。
- **正式榜发布恢复**：`contest_official_results` 的清旧、全量插入与 `official_results_ready=1` 同事务；中途注入失败不得留下 partial。若进程在赛事先写 `finished` 后、榜事务提交前退出，启动对账须对 `finished+ready=0` 幂等补算，恢复后公开接口不再返回 409，重复启动不重写已就绪结果。
- **管理操作**：admin abort 必须先取消并等待 runner 收敛；赛事局保留 aborted 历史同时将 pairing 复位/重派，2 人 KO 不得固定晋级座位 0。直接改 running/completed 被拒绝。赛事 `finished/cancelled` 终态不可回退或互转；`finished` 与已有正式榜的历史记录不可删除，`published` 删除明确走“取消未开打排期后删除”。runtime 与赛制模板没有管理写入口；其 GET 只验证代码来源。其余成功/失败管理写（含移除赛事报名）须进入 audit log。日志 API 按结构化记录过滤，ERROR/关键字命中时保留完整 traceback，上送响应不得含服务端绝对路径。
- **QA 写隔离**：后端 CLI 在 Store、日志和静态目录创建前校验 DB/端口/运行时目标；前端代理与 Playwright health guard 构成第二、第三道保护。

## 6. 发布门槛

最终验收至少需要：完整 `pytest`、`npm run build`、目标提交静态收集出的全部 Playwright、隔离 `e2e_smoke.sh` 全部通过；同时完成四身份全页面的首屏/中段/页尾截图图片识别，检查浏览器 Console/Network、QA 后端日志、QA 写目标与 50380 服务未被测试触碰。首页最新/热门、History、Bot 详情、搜索与 Cmd+K、MatchViewer、赛事赛程/树、Admin 对局都必须在桌面与移动端截图中逐行识别双方的用户、Bot/真人身份和对局性质；验证长用户名/Bot 名不溢出、移动端序号从 1 开始、性质标签不挤压操作入口，并确认任何缺失身份都不会显示数据库 ID。线上后台任务会合法改变主 DB hash/mtime，此时必须用主日志、实体哨兵与隔离实例标记归因，不能谎称 hash 未变，也不能把正常线上写误判成 QA 污染。若任一项未执行或失败，结论只能是“待验证”，不能写成“已验收”。

`mobile-public-data-cards.spec.ts` 专门守护 390px 下 Bot 历史与赛事赛程不依赖横向滑动的紧凑卡片：双方身份、owner、性质、轮次、状态与赛果必须默认可见，主查看入口至少 44px，桌面端仍保留高密度表格；同一组回归还要求反馈附件为中文选择按钮和文件摘要，不再混用浏览器英文原生控件。

> 返回 [doc/INDEX.md](./INDEX.md)
