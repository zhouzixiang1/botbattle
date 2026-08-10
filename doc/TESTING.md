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
| **API 关键链路脚本** | `scripts/api_full_test.py` | 注册回滚、播种、上传、挑战、SSE 终态 snapshot、全局 admission/429/补槽、循环赛 | 验证所列 HTTP 业务链路可重复运行；不声称覆盖实时 SSE 增量或全部端点 |
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
| **架构解耦** | `test_result_contract`、`test_game_registry`、`test_import_cycles`、`test_tongyong_layer_no_game_branches`、`test_db_layer_extensibility`；守护所有完成写路径只经 `matches/result_contract.py`，公共结果固定为 `rounds_played`、`deltas`、`normalized_delta`，各 GameSpec 自行提供进度与归一函数 |
| **持久化与迁移** | `test_db_migration` 覆盖三张对局表结果 JSON、正式榜破同分键、ratings/阶段结果中性列、pair/replay 精简；验证 JSON 新键优先于同行旧物理列、非法 JSON 先规范化后回填、非 completed 不伪造完成结果、复式轮数累计、二次打开幂等、行数/PK/FK/UNIQUE/索引/正式排名不变，并用故障注入断言整事务回滚 |
| **固定规则与协议** | `test_engine`、`test_board_engines`、`test_protocol`、`test_canonical_protocol_docs`、`test_pinned_game_config`、`test_canonical_bot_protocol`、`test_judge_public`；holdem=70+20000+50/100（预检首信封同样 `max_hand=70`）、gomoku=15×15、pencil=N=6+900s；direct session 未知规则参数、赛事阶段未知/错拼字段均拒绝；棋类每个 protocol 只导出自身 API且共享实现随源码公开；schema 与 Wiki 守护唯一严格信封 |
| **可发布样例 Bot** | `test_canonical_protocol_docs` 逐字绑定 Wiki 内嵌的三游戏 C/Python 完整示例与回归源码；`test_sample_bots_runtime` 实际构建三款 C ELF 与三款 PyInstaller 单文件 ELF，校验 Linux x86_64，并让两类产物在 Traditional/LongRunning 下分别跑完整 70 手 Holdem 与两款棋类合法终局；另守护完整历史重放、精确握手和六种 Holdem 策略仅依赖标准 history 字段 |
| **认证/邮件/安全/审计** | `test_auth`、`test_mail`、`test_store`、`test_security_logging`、`test_logging`、`test_audit_coverage`、`test_real_name`；邮件守护默认发件人、三条模板的 Botbattle 多游戏品牌与真实渲染结果；密码重置覆盖邮箱码/管理员 token、双 Store 并发单赢家、session 删除故障整事务回滚及过期凭据不消费；限流按 IP+方法+路径分桶，版本历史 GET 不得误耗上传 POST 额度 |
| **编排/实时通信** | `test_authoritative_terminal_events`、`test_human_match`、`test_frozen_version_failclosed`、`test_chess_clock`、`test_auto_matcher`、`test_match_seat_names`、`test_qa_script_artifacts`；GameSpec 棋钟覆盖 Bot-vs-Bot 与人类双方的累计/超时事件；Gomoku 人机回归让固定座位 2 的人类连续提交真实合法落子直至 `five/draw`，断言不少于 9 步、无 `illegal`、结果/replay 终局原因一致且 Glicko 不变；Pencil 真浏览器人机回归从权威 WS 事件维护已占边，连续点击三条合法边，断言每次只发一个 canonical `response`、裁判均产生人类 `move` 且无 `illegal_move`；mock WS 成格回合还断言 `pass=1` 时棋盘禁用、点边零发送且“确认让行”只发 `{"response":{"x":-1,"y":-1}}`。Python 守护把浏览器回归绑定到线上事故 ID、四类无效点击零发送、真实多步与让行用例；终态回归覆盖真实 70 手 Holdem、复式每 leg、协议技术负、启动崩溃、平台故障、通用异常与 human WS，断言 engine `match_end/error` 不进入公开写入，所有 completed 结果具备 `rounds_played/deltas/normalized_delta` 三个公共字段，Holdem 复式两 leg 的 `rounds_played=140`；completed 的 replay/live 各只有一条相同 `match_end {winner,reason,deltas}`，aborted 各只有一条相同 `error {reason}`；未知事件整条丢弃，已知事件附加的 message/debug/私有路径全部移除；活跃真人 Holdem 的公开 REST/SSE 不含底牌或决策请求，本人 WS 只含自己座位底牌；订阅快照成功后才注册队列，故障不留孤儿队列，可见性元数据缺失时默认隐藏底牌/请求。广播时 Store/GET 已提交终态，复式细节只由 `result.legs` 表达；管理员中止必须先原子提交，DB 失败时不能取消在途任务或丢订阅，提交后 replay 读取故障不得覆盖既有历史且仍完成 SSE/赛事 handoff；冻结版本缺失、跨 Bot、空路径、快照异常或 SHA-256/文件大小漂移在普通/人机/赛事三路径均须 fail-closed：创建前发现则零 match/task，已排队后发现则 `aborted + version_unavailable`，两者 runner 调用数与评分副作用均为零，任务/人类占用/赛事 pairing 统一收敛；同尺寸覆盖并恢复 mtime 仍须因 ctime 变化使缓存失效并重新哈希，checksum/size 为空的真正历史版本保持可运行；另含 SSE/WS 终态关闭与 shutdown 收敛 |
| **崩溃语义** | 中途崩溃（含 human）=`completed + reason=crash`；Bot-vs-Bot 启动失败=`technical_loss`；human 启动失败=`aborted` |
| **Bot 技术故障** | `test_bot_technical_faults` 覆盖拒绝 `{a:...}`、顶层整数/裸坐标、缺 response、非法 JSON/类型，同时断言带 `debug` 等额外顶层字段的响应只提交 `response`；另覆盖 LongRunning 缺失/错误握手、超时、三游戏、duplicate、人机隔离、评分政策、新写 `technical_incident` 事件、bounded result/replay 样本与结构化日志；预检必须走同一首回合信封。`test_matches_pagination` 覆盖只读归一化两种历史事件、敏感旧错误脱敏、REST 回放/SSE snapshot 只输出 `technical_incident`、现行 `technical_incident_*` 字段、`has_technical_incidents` 跨游戏/状态过滤、两个退役查询名显式 400 与 malformed replay；平台故障继续由 `test_audit_coverage` 断言 aborted 且不评分 |
| **二进制目标闸门** | `test_runtime`、`test_binary_visibility`、`test_frozen_version_failclosed` 覆盖仅 ELF64/小端/Linux/x86-64 可写与可执行；真实 x64 PE 必须在镜像检查及 Docker 启动前拒绝且不建立 session；Linux 镜像缺失只允许在 Bot 计时前单飞拉取并复核 `linux/amd64`，registry/拉取超时归平台故障，`docker run` 固定 `--pull=never --entrypoint /app/bot`；历史 PE 及主库同形态 `elf/空/空 + version unknown/空/空` 不迁移但 owner/admin 标记不可运行，并从公开候选、搜索、排行榜、自动匹配及赛事候选过滤；owner/admin 激活与版本回滚均 409 且 DB 不被改写；无 checksum/size 的旧版本缺文件也在建局前 `version_unavailable`；Playwright 还用真实 PE 上传验证服务端 400 与 UI 错误态 |
| **代码唯一配置** | `test_runtime_settings`、`test_auto_matcher`、`test_contest_templates`、`test_contest_template_seed`：旧 runtime KV 不能覆盖启动值，fresh app 不再 seed 同名键，runtime PATCH/admin template CRUD 为 404，只读诊断和公开模板均标记 `source=code/mutable=false`；历史模板表不 seed/对账且无法覆盖注册表；auto-match 使用冻结配置并把已接纳等待任务计入全局 admission；每日 cap 覆盖 scheduler 重启、双 Store 并发抢位/首次迁移、Asia/Shanghai 日期边界、等待写锁跨日、跨日新额度与普通/unclaimed ladder 不误计 |
| **测试产物隔离** | `test_logging` 断言从 repo CWD + tmp DB 运行时日志落临时目录，`create_app` 默认 upload root 落 DB 同目录，主 checkout 的 `bot_uploads/logs` 不接收测试标记；测试不得手写相对 `bot_uploads` |
| **赛事一致性** | `test_contest_*`、`test_scheduler_*`、`test_swiss_scale`：并发报名/派发、时间线 `opens<=closes<=starts`（含等时刻/部分 PATCH/旧脏数据/零部分写）、`starts_at=NULL` 的手动开赛闸门、全局 admission 下整轮只创建可用槽数量且完成一场补一场、Match 完成后 Pairing 逐场回写、历史空 starts_at/假 running 状态幂等修复、match_id 单 pairing 唯一绑定、发布/开赛 Bot 可用性闸门、版本冻结、两阶段 prepare→bind→start 补偿、admin abort 复位 pairing 且不晋级、单侧缺 Bot 技术判负/双侧缺 Bot 阻塞、published 残缺批次恢复、后续 stage 与 Swiss/KO 后续整轮批次原子提交、Swiss 实际座位轮换、正式榜技术负破同分/完整替换与 `finished+ready=0` 重启补算、安全 finish/delete；`test_contest_showcase` 另覆盖四身份可见性、所有展示写入口 409、scheduler/reconcile/活跃统计跳过、阶段实际参赛者交集、nullable unique key、seed 中断恢复不触碰无关 running match 与回滚身份碰撞拒绝 |
| **管理端安全操作** | 活跃 match 仅可经 orchestrator abort；用户/Bot/赛事存在活跃引用时拒绝硬删；批量指派做字段与归属校验 |
| **QA 隔离** | `test_qa_*`、`test_seed_test_accounts`、`test_qa_script_artifacts`、`test_load_test_seed`、`test_runtime_settings`：拒绝 50380、主库同路径/同 inode、主 checkout 运行时写目标与错误 Vite 代理；固定凭据账号须精确匹配 namespace/用户名/邮箱/角色/密码，压测不得复用任意管理员；隔离 QA 由代码选择 `enabled=False` 的 auto-match profile，生产 profile 与并发契约不变，只读诊断必须显示实际生效值；同名 QA Bot 只有在当前 `upload_root/<bot>/vN/bot.bin` 的规范路径、执行位、ELF 元数据、checksum/size/磁盘内容与 bots 镜像全部一致时才复用，否则在 per-Bot 锁内发布并激活当前运行时的新版本，绝不执行复制库指向主 checkout 的文件 |
| **社交/通知/成长/站点** | `test_notifications`、`test_comments_likes`、`test_social`、`test_xp_level`、`test_tiers`、`test_load_test_seed`、`test_wiki_pages`；覆盖 actor/target 不存在或在 API 预检后竞态删除时，关注/收藏/评论/点赞/取消点赞都由 `BEGIN IMMEDIATE` 写事务稳定返回 404，XP 与通知无副作用；删实体/用户清孤儿且即时同步对局点赞缓存；自评论不通知自己；通知使用展示层 1-based 座位；偏好 REST 严格 boolean，浏览器忽略迟到初始 GET、按字段串行合并快速点击并让最后一次操作成为服务端/UI 真值；以及统一 10 场定级阈值与正式榜优先排序 |
| **单游戏排行榜契约** | `test_leaderboard_density`、`test_tiers`、`test_pagination`、`test_response_field_whitelist`；Store/API 缺失或未知 `game_id` 必须拒绝，正式 rank 与定级分段/概览由后端权威生成，分页名次保持全局连续，rating 变化只读同游戏前一历史快照；最近对局必须同时存在于历史 reason、同 game 索引和物理 completed 行，且该 Bot 确为任一座位参赛方。公开行不返回内部累计分差、重复 game_id 或恒定平台三元组 |

## 3. Playwright 真浏览器回归

### 3.1 套件结构

`bzplat/frontend/e2e/` 当前有 5 个 spec，Playwright 静态收集为 56 条浏览器测试：

| Spec | 重点 |
|------|------|
| `public-audit.spec.ts` | 公开深链、刷新/前进/后退、404 fallback、登录错误、Network 失败后的错误/空状态 |
| `qa-regression.spec.ts` | 三 viewport 导航与单层页面 gutter、受保护页面访客门禁（不得先发无意义 401）、表单与超长文本/横向溢出边界（含 MyBots 320px 编辑态）、Windows PE 真实上传拒绝与历史不可运行 UI、赛事模板切换竞态与跨游戏提交闸门、挑战防重复提交、搜索、版本上传/回滚、受控 SSE 直播从事件 1 顺播/持续高频推流不饿死游标/超过 4000 条无损重连/暂停与终局不跳/显式跳转、德州有首手才显示 X/70、canonical `match_end.deltas` 驱动 MatchViewer 与 HumanPlay、Holdem 盲注/底池/all-in raise-to reducer 契约、生产回放 `20260809205002-ede64ea8` 的 70 手真实结算与末手完整事件 HUD、多比例三列/横排/堆叠/折叠/sticky/零横溢出、复式 140 手/换座/leg 边界与真人 HUD 无底牌文本，Pencil 非法终局 2:0 归一、点格棋首次计时/首回合超时 UI 契约、Pencil 横纵边端点与 `2×cell` 格几何、线上 `(5,5)` 格心事故 fixture 零发送、`pass=1` 禁棋盘并只发 `(-1,-1)`、`move/pass/turn` 逐事件行动方与强制让行真值、方向键选择/坐标播报/Enter canonical 提交、同长度 snapshot scene 替换与倒计时重渲染不中断动画、320/390/844/1024/1312/1600/1920/2048/2560 多比例方形棋盘与滚动/时序协作、局面概览数值、Safari 截图同尺寸的 2048×1024/1152 Chromium 布局回归（观赛与真人完整棋盘、长时序独立滚动）、真实 Pencil 人机连续三条合法边（自动处理 Bot 成格让行）、未知游戏 fail-closed、棋类人类动作 canonical `response` 信封、人类 Holdem WebSocket、admin abort 回归 |
| `contest-workflow.spec.ts` | 组织者创建→开放→两名浏览器用户报名→发布→开赛→完成→admin 清理 |
| `admin-audit.spec.ts` | admin 7 个业务 Tab、查询参数/返回数据一致性、关键保存操作与布局；赛事时间按状态收口、空值/显式 `NULL`、保存失败原位反馈、真实隔离库重载与 audit、500+ 字连续长文本、Dialog 滚动与三视口；断言不存在运行时/赛制模板 Tab 与对应写 API |
| `leaderboard-density.spec.ts` | 访客/普通用户/组织者/admin × Desktop/Laptop/Mobile；每个 context 精确断言 `/api/auth/me` 的匿名状态或 username/role，非访客三类各只登录一次并复用独立 storageState；长 Bot/用户名折行、桌面六列表头、移动列表卡、正式/定级分段、滚动中可操作的 Radix tabs/表头 sticky、慢响应切游戏立即清旧概览、根元素零横溢出、三游戏显式请求、tiers 同 game singleflight 与 Console/Network clean |

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
- 视口：Desktop `1440×900`、Laptop `1280×720`、Mobile `390×844`；访客导航与 admin 七 Tab 均覆盖三档，核心赛事流程另以 laptop 执行。德州生产回放 HUD 另覆盖 `2560×1080`、`1920×1080`、`1760×900`、`1600×900`、`1536×900`、`1366×768`、`1280×800`、`1024×768`、`390×844`、`320×568`；断言自定义 `3xl=1760px` 及以上三列、1280–1759px HUD 横排在牌桌上方且时序在右、窄屏堆叠、16:9、时序折叠/页面滚动 sticky、真人 HUD 无底牌文本和根元素零横向溢出；复式 fixture 另断言 2×70=140 手总进度、第二局物理座位映射、leg 边界不沿用旧动作和无伪整场胜者。Pencil 观赛/人机另用 Chromium 覆盖 `2560×1080`、`1920×1080`、`2048×1024/1152`、`1600×900`、`1536×1080`、`1366×768`、`1312×700`、`1024×768`、`844×390`、`390×700`、`320×568`；断言 21:9、16:9、4:3、短横屏和手机竖屏按三栏/双栏/堆叠重排，方形 canvas 同时受可用宽高约束，局面概览数值与当前事件一致且不高于棋盘、长时序独立滚动、折叠后不保留空右轨、根元素零横向溢出且页面上下滚动后仍可用。真实 Safari/WebKit 仍列为人工视觉抽查，不把同尺寸 Chromium 结果冒充 Safari 引擎证据。
- UI：主要导航、按钮、Tab、Dialog、筛选、表单合法/非法/超长输入、重复提交、空状态、错误状态、直接子路由、刷新、返回/前进与根元素横向溢出。
- Console：持续收集 `pageerror` 与 error 级 console；未在精确白名单中的异常直接使测试失败。
- Network：跟踪 request failed 与 4xx/5xx；负向用例只豁免精确预期的请求/状态，关键写操作断言方法、路径、状态和返回结构。深链 reload/back/forward 在继续导航前须等待目标实体 ID 对应的 detail 200 与普通 HTTP quiet window，避免仅凭通用标题把仍在收尾的 fetch 留给下一段导航。
- SSE：除终态 snapshot 转回放且不重连外，以可控 EventSource 批次断言初始停在事件 1、新事件先增加分母再逐条播放；另以 50ms 连续推流覆盖事件增长频率持续高于 700ms 播放速度，游标仍须在推流结束前前进，防 timer 被 `total` 变化反复取消。初始 4101 条、重连增长到 4351 条的回归同时断言事件 1、暂停游标和新增尾部全部保留，不允许后缀裁剪。暂停与普通重连 snapshot 不改变游标、终局不跳尾且可继续顺播、显式跳结局/重播从事件 1 生效；德州只有看到 `hand_start` 后才显示 X/70，零手 `admin_aborted/platform_error` 不显示假进度。后端重连测试须证明运行 snapshot 来自完整内存前缀而非节流落库点，经过与 REST 相同的字段投影/真人座位脱敏；终态状态必须忽略尚未释放的旧前缀并从 Store 合成唯一终局。另断言公开 error 只按稳定 reason 显示中文且恶意/缺失 reason 的 message 不参与语义、运行态空 reason 不预称 completed、异常 completed 原因可见、服务端终态后流关闭；纯 mock 点格棋流还断言首次 `time_used` 用 `budget` 初始化未行动方，首次事件即 `time_out` 时显示 `0:00 + 超时`。
- WebSocket：真实人类 Holdem 流程断言单页只建一个连接、发送合法协议并进入终态；真实 Pencil 人机连续三回合从 canvas 合法边中心点击，逐帧核验 canonical response 与裁判 `move`，并以线上事故棋盘 fixture 证明点/格心/已占边/棋盘外为零发送；成格后的 `pass=1` fixture 断言 canvas 为 inactive、点边零帧、确认让行精确一帧 `(-1,-1)`；admin abort 在取消 runner 后仍须向既有连接送达权威终态，且 runner 不得覆盖 aborted。
- Admin 赛事时间：`draft` 可改开放/截止/开赛，`open` 只可改未来的截止/开赛，`published` 只可改开赛，其余状态只读；无值时控件保持空白并展示手动语义，已有自动开赛时间可通过开关提交显式 `starts_at:null`，与省略字段保留旧值区分。`published` 尚无 `match_id` 时按发布轮次错峰规则在同一事务重排当前阶段 pending pairing；显式 `null` 同步清空，已有绑定或任一写入失败时赛事与逐场排期均不部分更新。Dialog 直接展示保存错误，真实隔离库用例还验证重载后的 `NULL` 与成功 audit 记录。

### 3.3 文档边界审计

公开 `wiki/` 只保留 `INDEX`、统一协议、Bot 上手/编译、平台使用与三游戏规则/示例；未出现 worktree、PR、仓库构建脚本、部署、pytest/npm 等平台工程说明。`BOT_DEV.md` 对 Windows/Linux/macOS 的 C/Python 指南最终都产出 Linux x86_64 ELF，未承诺运行 PE、Mach-O 或源码。协议中的 `BOTZONE_REQUEST_KEEP_RUNNING` 是必须逐字输出的 LongRunning 握手常量，不是新旧平台对比或兼容入口。

工程架构、测试、部署、运行配置、裁判代码位置和本轮清理盘点均在 `doc/`。机器可读 `contracts/` 当前只为 Holdem 提供完整 payload schema；Gomoku/Pencil 仅在 Wiki 与运行时测试中约束 payload，属于 P2 待补的契约覆盖差距，但不代表存在第二套协议。

旧的 `browser_verify.py`、`screenshot_verify.py` 仍可做补充，但不能替代上述真实交互、Console 与 Network 断言。

## 4. 本轮 QA 执行证据

以下只记录实际已执行的证据，不把“已收集”写成“已通过”：

| 检查 | 本轮状态 | 证据/说明 |
|------|----------|-----------|
| 隔离端到端冒烟 | **ALL PASSED** | 本分支 `bash scripts/e2e_smoke.sh` 在 `/tmp` 临时 DB 与运行时目录完成，退出后回收自己的服务和目录；写目标不在主仓库 |
| API 关键链路脚本 | **50 passed / 0 failed** | 全新临时库隔离运行 `scripts/api_full_test.py`，包含无 SMTP 注册回滚、全局并发上限精确接纳、超额 429 与释放后补槽等核心 API 链路；SSE 证据为终态 snapshot，不含实时增量 |
| Playwright 收集 | **56 条 / 5 spec** | 目标整合 HEAD `npx playwright test --list` 实测；保留排行榜合入后 53 条 / 5 spec 基线，新增 3 条 Holdem 回归：真实 0809 单局回放的多比例 HUD、复式 140 手/换座/leg 边界、真人 HUD 公开信息不泄露底牌 |
| Holdem 响应式最终整合门禁 | **pytest 1096 passed / 1 warning（234.38s）；Playwright 56/56 passed（4.5m）；build 2563 modules** | 基于 `main@7c63fb843` 的单一整合树；完整浏览器套件含四角色排行榜、Pencil 响应式仪表盘、真实 `20260809205002-ede64ea8` Holdem 回放、复式 140 手与真人脱敏 HUD；Console/Network 监控无非预期异常。warning 为既有 Starlette/httpx deprecation |
| Holdem 数据库影响 | **无 schema/迁移/业务写路径变更** | 仅前端归约、画布、布局、测试与文档；浏览器写操作全部指向独立 inode 的 worktree 副本库，最终 `integrity_check=ok`、`foreign_key_check=0` |
| 观赛/视觉复审定向浏览器回归 | **9/9 passed（42.8s）** | 隔离 QA backend `50386` + worktree Vite `5178`；4 条新增/编辑态风险用例与 5 条既有终态/顺播/重连/零手协议故障回归分两组实测，覆盖 320px MyBots、50ms 连续 SSE、4101→4351 无损 snapshot、零手 admin/platform 中止、终局不跳及 Console/Network |
| 前端游戏契约定向浏览器回归 | **3 passed** | 独立无数据库 fake API + worktree Vite：未知 `game_id` 显示 unsupported 且不创建 Holdem canvas；Gomoku canvas 点击只发送 `{"response":{"x":int,"y":int}}`；点格棋 HUD 移入游戏包后的首回合棋钟/超时回归仍通过。Console/普通 HTTP Network 监控无非预期异常 |
| 权威终态定向浏览器回归 | **1 passed** | 隔离 QA backend + worktree Vite；mock SSE/WS 只发送 canonical `match_end {winner,reason,deltas}`，MatchViewer 与 HumanPlay 均正确显示胜者和 Holdem 累计净筹码，Console/Network 无非预期异常 |
| 权威终态后端定向回归 | **70 passed / 1 warning（29.94s）** | `test_authoritative_terminal_events` + `test_audit_coverage` + `test_human_match` + `test_engine`：真实 70 手 Holdem、duplicate、协议技术负、启动崩溃、平台错误、SSE 队列与真实 TestClient WebSocket；replay/live 各一条相同 canonical 终态，广播时 Store/GET 已完成。warning 为既有 Starlette/httpx deprecation |
| Pencil 重基最终定向回归 | **浏览器 7/7 passed（15.4s）** | 基于 `main@5a2662f` 的最终整合提交；覆盖几何、生产事故夹具、强制让行、键盘合法边、等长 snapshot/父级重渲染动画、回放布局与真实裁判多步。后端规则与协议回归同时包含在本表的最终完整 pytest 中 |
| Pencil 宽屏短视口最终门禁 | **pytest 1093 passed / 1 warning（222.70s）；Playwright 50/50 passed（3.5m）；build 2562 modules** | 基于 `main@b210067` 的独立副本栈 `50384/5176`；附件对局 `20260810143624-4149d6a3` 绑定的回归覆盖 2048×1024/1152 观赛与真人首屏、640px 完整方形棋盘、206 条长时序内部滚动、外圈边 1px DPR 取整容差及 320/390/1312 既有布局。完整浏览器套件含四角色；warning 为既有 Starlette/httpx deprecation |
| PR154 中性结果 pytest 基线 | **1091 passed / 1 warning（220.72s）** | `main@5a2662f` 合并前的中性结果契约目标提交完整实测；公共结果、迁移与正式榜契约均保留，warning 为既有 Starlette/httpx deprecation |
| PR154 中性结果 Playwright 基线 | **43/43 passed（3.2m）** | 隔离 QA 栈 `50383/5175`、Chromium 单 worker。曾由 37/39 精确暴露排行榜每行 `TierBadge` 的同键 tiers 请求风暴；PR154 已按 game_id 合并 in-flight Promise，失败后可重试，三视口定向 3/3 证明每游戏并发首请求只 1 次。该行不冒充点格棋重基结果 |
| 生产副本中性迁移 | **通过** | 2026-08-10 15:03 +08:00 从主库只读 `cp` 到独立 inode（12748789 → 4197548），用 PR154 代码迁移并二次打开：integrity=`ok`、FK=0；三对局表 523/441/453、completed 519/441/453，ratings 76、pair 209、replay 1895、stage/official 33、settlement 1194 行均不变。正式排名投影 `contest_id,entry_id,stage_idx,rank` hash 前后均为 `d1d5a83c2ed4d1dc240556668cc739b4bd3f61f81f280bee4f7921587509f620`，settlement 按物理列全行 hash 前后均为 `43f8de1554cdecaa8b673361ebdd65fc4785a97a510d1e013eaf9ec1f1918931`。完成结果缺字段/旧键/归一值错误、非完成态伪公共字段、正式榜旧键均为 0；新中性列存在，pair/replay 与旧筹码列均删除 |
| 重基最终完整 pytest | **1093 passed / 1 warning（226.20s）** | Pencil 提交重基到 `main@5a2662f` 后，在同一最终代码 HEAD 完整执行；warning 为既有 Starlette/httpx deprecation |
| 重基最终完整 Playwright | **50/50 passed（3.4m）** | 隔离 QA backend `50382` + worktree Vite `5174`、Chromium 单 worker；覆盖 PR154 的 tiers 同键 singleflight/失败重试、中性结果契约，以及 Pencil 事故链路、无效命中零请求、强制让行、键盘、多视口和 Console/Network |
| Pencil 响应式仪表盘最终门禁 | **pytest 1093 passed / 1 warning（231.60s）；Playwright 50/50 passed（3.9m）；build 2562 modules** | 基于 `main@fffdd9c` 的独立 QA backend `50384` + Vite `5176`，主库复制为独立 inode；真实事故对局 `20260810143624-4149d6a3` 的 54 步/206 事件 fixture 与技术终局 fixture 覆盖 2560/2048/1920/1600/1536/1366/1312/1024/844/390/320 多比例、实际格子归属、连边构成、三栏顶部对齐且概览不越出棋盘、短屏双栏、折叠回收空轨、手机堆叠、页面滚动与时序内部滚动；完整 50 条含四角色与三游戏，warning 为既有 Starlette/httpx deprecation |
| 排行榜重构最终完整 pytest / build | **1096 passed / 1 warning（228.65s）；build 2562 modules** | 排行榜提交重基到 `main@22c4c645` 后，在同一最终代码 HEAD 完整执行；包含上述 Pencil 响应式仪表盘、中性结果基线及新增的单游戏排行榜、分页全局名次、相邻评分变化、recent-match game/物理表/completed/实际参赛方四重校验回归；warning 为既有 Starlette/httpx deprecation |
| 排行榜重构最终完整 Playwright | **53/53 passed（3.8m）** | 隔离 QA backend `50382` + worktree Vite `5174`、Chromium 单 worker。保留全部 Pencil/中性结果/响应式仪表盘基线并新增 Desktop `1440×900`、Laptop `1024×768`、Mobile `390×844` 三条密度回归；每条逐一覆盖访客、普通用户、组织者、管理员，并精确断言 `/api/auth/me`：访客为 401 且无本地会话，另外三类 username/role 与账号一致、各真实登录一次后复用独立 storageState。页面/请求/滚动中切 tabs/慢网清旧概览/sticky/长文本/无横溢出/singleflight/Console/Network 断言不降级 |
| Admin 浏览器定向回归 | **9 passed（24.7s）** | `admin-audit.spec.ts` 全量；含状态边界、Dialog 内错误、真实隔离 DB 的手动开赛 `NULL` 重载与成功 audit 证据 |
| Admin 时间定向后端回归 | **26 passed / 1 warning（8.04s）** | `test_admin_contest_status.py`；覆盖状态边界、发布态轮次错峰重排/清空、已有 match 拒绝、强制 SQLite 写失败整事务回滚；warning 为既有 Starlette/httpx deprecation |
| QA profile + Admin 联合回归 | **53 passed / 1 warning（12.22s）** | `test_auto_matcher.py` + `test_runtime_settings.py` + `test_admin_contest_status.py`；覆盖 disabled loop 零 challenge、main QA wiring、实际生效只读诊断、生产 profile 不变及赛事时间全部边界 |
| 前端构建 | **已通过** | `npm run build`（`tsc -b && vite build`），2562 modules transformed |
| 浏览器 QA 写隔离 | **通过** | 50382 与 5174 的 `/api/health` 均返回 `qa_instance=true`；worktree DB 与主库 inode 不同，PR154 迁移后 integrity=`ok`、FK=0，标准 tester1/tester2 及六个种子 ELF Bot 只写入 worktree 副本和其 `bot_uploads/`。事故副本 `20260810140318-a8752705` 的 API 结果为新契约 `rounds_played/deltas/normalized_delta`。QA profile 固定禁用 auto-match，浏览器全量执行另显式关闭 QA 限流；生产 50380/主库未作为测试目标，验证完成后仅停止本轮两个独立服务 |
| QA 后端日志 | **通过（无非预期异常）** | 从最终重基栈 15:27:22 启动行起逐项检查 `app/access/audit` 与服务输出：无 5xx、429、ERROR、Traceback、`version_unavailable`、auto-match 调度或 Bot cleanup 409；其余仅有精确预期的 SMTP 未配置提示、`/usr/bin/true` 预检 EOF，以及负向用例主动制造的失败 |

## 5. 可靠性与恢复专项

- **对局重启恢复**：上一进程遗留的 running 与非赛事 pending 对局统一中止；活跃赛事 pending 由 pairing 对账精确恢复，避免把可重派赛事粗暴 abort。
- **auto-match 每日上限**：`auto_match_daily_claims` 是 DB 权威计数；match/index/claim 同事务创建，后续初始化失败的精确补偿同时删除 claim。并发回归以 6 个独立 Store 连接同时争抢 cap=2，必须恰好 2 个成功；重建 scheduler 仍读取既有 claim。日界回归让调用前日期停在 23:59:59、Store 拿写锁后切到次日，provider 断言调用时连接已在事务中，并验证旧日满额不影响新日首场、新日第二场被原子 cap 拒绝。
- **评分恰好一次 + 顺序屏障**：`match_rating_settlements` 的 claim 与双方 rating/history/pair_stats 同事务；失败整体回滚。覆盖 M1 评分事务失败后 M2 触发补算，必须严格按 `(created_at,id)` 得到与 M1→M2 正常路径完全一致的 rating/history；并发后处理不越序且通知/XP 不重复。启动恢复共用同一顺序机制，只补评分，任一早场失败即停止，重复调用无副作用。
- **定级/对手统计一致性**：定级场数由代码配置同时驱动 auto-match、tiers、leaderboard 与 Bot profile；正式 Bot 始终排在定级 Bot 之前。每次 rating settlement 同事务递增 `pair_stats.samples`，并断言其恒等于胜+负+平；迁移回归修复历史零值。
- **通知偏好布尔边界**：Store 单测保留 SQLite 0/1 断言，HTTP 测试断言 GET/PUT 四字段只返回 boolean、字符串布尔被拒绝；浏览器抓取 Switch 的单字段 boolean PUT，并覆盖迟到初始 GET、同字段快速反复点击、旧账号请求迟到三类竞态，刷新后服务端与 UI 都以最后一次操作为准。
- **终局原因展示**：浏览器以正常 `five/score/majority`、异常 `illegal/protocol_error/platform_error` 和未知 completed 历史码作对照，断言 MatchViewer、HumanPlay、admin 对局表的 `{label,tone}` 一致，内部英文码不泄漏，时间线保留游戏 `describeEvent`；点格棋 `illegal/time_used/technical_incident` 均转成含展示座位与决策/用时的中文；同一 SPA 内从点格棋人机终局切到五子棋人机终局，验证旧 Scene 不会跨 renderer 复用导致画布崩溃。
- **关注/收藏竞态**：Store 直接覆盖 actor/target 任一缺失；API 故障注入在预检查后删除目标，follow/favorite 必须稳定返回 404 且不留下关系，证明最终存在性检查与写入位于同一事务。
- **赛事两阶段派发与阶段推进**：prepare match → 原子绑定 pairing → start task；任一步失败须删除/解绑本次精确对象。服务重启可清理未绑定幽灵、复位死 pairing，并只在整个 published 批次尚未启动时原子重建残缺排期。后续 stage 的全部 pairing（含版本快照、bye、排期）与 `current_stage_idx/status` 同事务提交；Swiss/KO 懒生成的后续轮先构造完整 rows，再在 `BEGIN IMMEDIATE` 内复核 contest/stage/上一轮/目标轮并一次追加。两类批次均须以第二行故障注入验证零 partial，重试只生成一个完整批次。Swiss 还须验证按 entry 累计实际 seat0、`color_first=1` 落库前交换 A/B、challenge 收到同一实际顺序。
- **演示快照**：六个 key 必须各唯一且状态、名册、pairing/match 数量符合清单；published 保持 `starts_at=NULL + 24 pending + 0 match`，running 固定为 `4 completed + 20 pending + 0 active`，rest 固化 24 场与 12 条阶段榜，finished 固化 24+7 个互不复用的真实 Match/回放、非全平阶段积分与连续 1..12 正式名次。实际副本执行还须连续通过 `seed → seed（全跳过）→ verify`，再在重写二进制路径的 DB/上传目录复制品上验证白名单 rollback 和外键/完整性；不得拿主库做回滚演练。
- **正式榜破同分**：从 pairing 的实际 A/B 与 completed match 的 `technical_loss/winner` 识别技术负 entry；其余破同分项相同时，技术负次数较少者必须排前。
- **正式榜发布恢复**：`contest_official_results` 的清旧、全量插入与 `official_results_ready=1` 同事务；中途注入失败不得留下 partial。若进程在赛事先写 `finished` 后、榜事务提交前退出，启动对账须对 `finished+ready=0` 幂等补算，恢复后公开接口不再返回 409，重复启动不重写已就绪结果。
- **管理操作**：admin abort 必须先取消并等待 runner 收敛；赛事局保留 aborted 历史同时将 pairing 复位/重派，2 人 KO 不得固定晋级座位 0。直接改 running/completed 被拒绝。赛事 `finished/cancelled` 终态不可回退或互转；`finished` 与已有正式榜的历史记录不可删除，`published` 删除明确走“取消未开打排期后删除”。runtime 与赛制模板没有管理写入口；其 GET 只验证代码来源。其余成功/失败管理写（含移除赛事报名）须进入 audit log。日志 API 按结构化记录过滤，ERROR/关键字命中时保留完整 traceback，上送响应不得含服务端绝对路径。
- **QA 写隔离**：后端 CLI 在 Store、日志和静态目录创建前校验 DB/端口/运行时目标；前端代理与 Playwright health guard 构成第二、第三道保护。

## 6. 发布门槛

最终验收至少需要：完整 `pytest`、`npm run build`、目标提交静态收集出的全部 Playwright、隔离 `e2e_smoke.sh` 全部通过；同时检查浏览器 Console/Network、QA 后端日志、QA 写目标与 50380 服务未被测试触碰。线上后台任务会合法改变主 DB hash/mtime，此时必须用主日志、实体哨兵与隔离实例标记归因，不能谎称 hash 未变，也不能把正常线上写误判成 QA 污染。若任一项未执行或失败，结论只能是“待验证”，不能写成“已验收”。

> 返回 [doc/INDEX.md](./INDEX.md)
