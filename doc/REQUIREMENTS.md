# 需求分析

> 本文档定义 botbattle 平台的功能与非功能需求，并以追溯矩阵确认每项需求的实现覆盖。

## 1. 项目背景与目标

### 1.1 背景
Bot 竞赛平台允许用户提交自动化程序（Bot），由平台托管运行对局并排名。平台同时提供逐决策重启与整场长驻两种明确的进程生命周期。

### 1.2 目标
构建一个**严格支持 Traditional/LongRunning、沙箱隔离、多游戏可扩展**的 Bot 对战平台，提供：
1. 用户上传 Bot → 平台沙箱安全运行 → 自动判胜与评分。
2. 实时观赛、完整回放、Glicko-2 排行榜。
3. 组织者赛事（多赛制）、人类亲自上场、社交互动。
4. 三款游戏（德州扑克/五子棋/点格棋），且赛制/编排主流程通过统一契约接入游戏，不新增游戏名分支。
5. 可追踪的平台↔用户站内通信、可恢复邮件投递、安全广播与小白式 Bug 反馈。

## 2. 用户角色

| 角色 | 标识 | 核心诉求 |
|------|------|---------|
| **玩家** | `user` | 上传 Bot、发起挑战、观赛、查看排行与战绩、社交互动 |
| **组织者** | `organizer` | 创建与管理赛事（赛制模板、报名、推进阶段） |
| **管理员** | `admin` | 全局管理（用户/Bot/对局/赛事/日志/通信/广播/Bug）；运行参数、赛制模板与官方事务邮件模板由代码持有，不提供管理写入口 |
| **访客** | 未登录 | 浏览排行榜、对局回放、导出终态单场公开日志、浏览 Bot/用户主页与 Wiki |

## 3. 功能需求

### 3.1 账号与认证
| 需求 | 验收标准 |
|------|---------|
| 注册 / 登录 / 登出 | 用户名 `^[a-zA-Z][a-zA-Z0-9_]{2,31}$`，密码 ≥8，session 7 天，图形验证码防爆破 |
| 邮箱验证 | 注册后创建 TTL 30 分钟验证码与高优先级邮件 delivery，API 返回 `queued`；SMTP 不参与注册事务，失败不回滚用户/验证码，验证后方可登录 |
| 密码重置 | 邮件重置验证码，防枚举（不存在也返回成功语义）；发信异步排队，不因 SMTP 失败回滚业务 |
| 个人资料 | 改显示名 / 简介 / 头像（png/jpeg/webp/gif ≤2MB，存本地 `avatars/`）/ 改密码 |

### 3.2 Bot 管理
| 需求 | 验收标准 |
|------|---------|
| 上传 Bot | 唯一接受 Linux x86_64 ELF；PE/`.exe`、Mach-O、ARM64 ELF、原始 `.py` 与脚本全部拒绝；预检按所选 runtime_mode 使用正式首回合同一信封，LongRunning 必须握手 |
| 历史二进制 | 旧库中非 Linux x86_64 ELF 记录只可供 owner/admin 审计，必须标记为不可运行；不得出现在公开选择器、搜索、排行榜、自动排位或报名候选中，也不得由 owner/admin 重新激活；即使旧版本没有 checksum/size，缺失文件也须在 job 创建/claim 的完整性门禁拒绝 |
| 版本管理 | 同一 Bot 可上传多版本，可切换激活版本，可删/改名/改简介 |
| 排行榜派遣 | 同一账号在同一游戏最多派遣一个 Bot 参加平台排行榜和自动排位；首个通过预检的 Bot 在该游戏尚无派遣项时自动成为排行榜 Bot，之后可由 owner 原子替换或退出。切换不搬运、不清空历史 Rating/RD/对局，未派遣 Bot 仍可练习和参加锦标赛 |
| Bot 详情页 | 数值评分卡（Rating/RD/95% 区间/公开名次/样本/可靠性）+ 服务端分页的对局历史 + 当前评分池对手战绩（完整总数与服务端分页）+ 评分曲线（recharts）|

### 3.3 对局与观赛
| 需求 | 验收标准 |
|------|---------|
| Bot vs Bot 挑战 | 选对手（全部/我的/按用户搜索）+ 选游戏（规则参数已钉死固定值）+ 选择“我的 Bot 位置”；`my_bot_id` 始终表示本人 Bot，`my_seat=0/1` 决定其物理座位，Bot/版本/执行环境/本地连接整体映射。普通用户两个方向都必须通过本人 owner 校验。POST 返回 HTTP 202 持久 request，不在排队阶段创建 Match；支持查询、刷新恢复、取消及 interrupted 重试，取得容量后才沙箱运行。不同 owner 的双方都必须是各自该游戏当前排行榜 Bot 才计 Glicko；其他组合仍可练习但明确标记不计分 |
| 实时观赛 | SSE 推送事件流，前端棋盘/牌桌逐步渲染 |
| 对局回放 | 完整事件录制，播放/暂停/步进/倍速（0.5x-4x）/逐手跳转/进度拖动 |
| 对局日志导出 | 德州、五子棋、点格棋的 `completed/aborted` 单场均可公开下载 canonical JSON v1；只有同一数据库快照中的终态 Match 与已持久化 `match_end/error` 尾项一致时才导出，活动局、未落稳回放、未知游戏或损坏契约返回 409，缺失对局返回 404。日志不包含私有 Bot `debug`、原始 stdout/stderr、二进制/版本路径、执行配置或令牌；五子棋专项棋谱是并列能力。功能严格限于单场，不恢复已下线的按月/批量对局数据集 |
| Pencil 累计棋钟 | 每方固定 900 秒；Bot-vs-Bot 与人类对战均累计实际决策时间，成功/耗尽分别落 `time_used`/`time_out` 事件，并在点格棋观赛/回放显示剩余时间与超时状态 |
| 人类 vs Bot | WebSocket 落子回传；公开挑战契约继续固定真人 `human_seat=1`（第二方），不继承 Bot-vs-Bot 的 `my_seat`；与 manual/contest/auto 共用全局队列和 match slots，claim 后占 `1 slot + 1 sandbox unit`，人工/人机 per-user 活跃 ≤1，不计 Glicko；通用人类回合等待默认 120 秒，Pencil 同时受每方 900 秒累计棋钟约束 |
| 自博弈 | 同一 owner 的两个不同 Bot 可对战，走普通挑战 |
| 崩溃收敛 | 对局中途 Bot 崩溃（含人类局）按游戏结果结算为 `completed` + `reason=crash`；Bot-vs-Bot 启动失败为 `completed` + `technical_loss`，人类局启动失败为 `aborted` |
| 协议故障收敛 | 唯一响应对象必须包含 `response`，平台忽略其他顶层字段；顶层整数/裸坐标/缺少 `response` 的旧 `{a}` 仍拒绝，LongRunning 缺失精确握手不回退。首次协议故障即 `completed + protocol_error + technical_loss`；超时为 `completed + timeout + technical_loss`。Bot-vs-Bot 计分、人机局不计 Glicko；平台 sandbox 故障始终 aborted 且不评分；格式正确的非法游戏动作仍交裁判。新写回放和 SSE 只使用 `technical_incident`；结果只公开 `technical_incident_count` / `technical_incidents_by_seat` / `technical_incident_samples`，列表查询唯一使用 `has_technical_incidents`；历史旧事件仅在服务端读取边界归一化，不形成新写或第二套对外合约 |
| 中止终局契约 | REST replay、SSE 与人类 WS 共用唯一 `error {reason}` 两字段终局；reason 只允许平台稳定码，未知值归一 `platform_error`，不公开 message/异常/路径。先持久化 aborted 再写回放并广播；管理员只可请求中止，原因固定 `admin_aborted` |

### 3.4 评分与排行
| 需求 | 验收标准 |
|------|---------|
| Glicko-2 评分 | 自实现，按游戏分别排名；只有不同 owner 的两个当前排行榜 Bot 之间的平台对局自动更新，contest/human/本地 Bot/未派遣 Bot 练习局除外；资格在 execution job 创建时冻结，派遣变化不得重释历史 policy |
| 数值排名 | 至少 `RANKING_MIN_RATED_MATCHES` 场才有公开名次；展示 1-based 名次/总数、百分位、Rating/RD、95% 区间和样本进度，不输出定性标签 |
| 排名趋势 | `rating_history` 记录评分变化；排行榜显示相邻变化和 30 日基线变化 |

### 3.5 赛事系统
| 需求 | 验收标准 |
|------|---------|
| 赛制模板 | 6 种阶段 + 2 种计分 + **19 个代码注册模板 / 18 个可新建**。Holdem：`holdem_dup_rr`、`holdem_rr`、`holdem_swiss_ranked`、`holdem_swiss_top8_ranked`、`holdem_swiss_ko`、`holdem_top8_ranked`、`holdem_prelim_swiss`，以及只读历史 `holdem_final_ranked`；Gomoku：`board_rr`、`gomoku_rr`、`gomoku_swiss_ranked`、`gomoku_swiss_top8_ranked`、`gomoku_group_drr_ko`、`gomoku_swiss_ko`；Pencil：`pencil_drr`、`pencil_group_drr_ko`、`pencil_swiss_ranked`、`pencil_swiss_ko`、`pencil_ko`。公开 `recommended_min/max`、`purpose`、`time_class` 仅供推荐，不阻断自由选择；创建/发布确认须显示基础对局、基础计分场、基础 ETA 与风险 |
| 赛事生命周期 | draft→open→published→running⇄rest→finished；`finished/cancelled` 为不可回退终态；`published` 只发布当前阶段/当前轮可确定的排期，不承诺一次生成完整赛事对阵；已填写时间必须满足 registration_opens_at≤registration_closes_at≤starts_at（等时刻合法），`starts_at` 留空表示等待组织者手动开始；ContestScheduler 只推进已到开赛时间的赛事 |
| 赛事并发与状态 | Bot 对局与其他来源共享全站 6 match slots / 12 sandbox units 代码硬顶，每场占 1 slot；job 按冻结 CPU/内存/sandbox 向量和主机预算准入，实际并发为 1–6，显式值只能收紧。赛事共享份额 1 不是额外槽；同一非 human Bot 全局至多一个 `starting/running/settling` job。contest 只在不跨 manual/human 排序边界的连续队列段内按持久 claim 历史轮转；一轮其余 Pairing 保持待开始，每场完成后立即回写并补派 |
| 循环赛规模 | 全员与分组单/双循环均不设参赛人数硬上限，完整 O(n²) pairing/job 进入持久队列，不放大物理并发。`allow_large_round_robin` 仅为历史快照的严格布尔兼容 no-op；页面必须以精确基础场数/计分场/ETA 和超过 8/24 小时风险提示帮助选择 Swiss 或更短模板，不得把建议范围变成发布门禁 |
| Swiss 轮数 | `swiss_round_bands` 是通用阶段字段；Gomoku 三个 Swiss 模板按 13–15 人 7 轮、16–20 人 9 轮、21 人以上 11 轮解析，publish 将结果冻结为 `effective_rounds`。人数低于建议范围仍可自由选择并沿通用轮数规则；运行中/历史快照不得被新 band 改写 |
| 淘汰决胜 | 新 Holdem/Gomoku 单败须显式冻结 `tiebreak=paired_swap_until_decided`：原局平后追加两场换座组，按原 stage scoring 汇总组分；仍平继续下一组，无次数上限，不使用 margin/delta/seed/报名序兜底。Holdem 同组使用相同实际 seed 保证同牌；Gomoku 只交换开局提案方/交换决策方，开局由 Bot 决定，不承诺相同。历史无 marker 阶段仍阻断，只有 draft/open 且零进度赛事可经 CAS 更新 |
| 管理员名册纠错 | draft/open 赛事中，管理员以精确“活跃用户 → 该用户当前可运行、同游戏 Bot”映射为主路径，可核对、换 Bot、移除暂存项后一次提交；后端在写事务内重验用户、归属、游戏、版本/协议/二进制与实名资格，部分失败逐项反馈。“全员指派”只保留为再次确认的次要快捷操作；普通组织者的实名赛代报名权限不因此放宽 |
| 积分榜与对阵图 | 实时积分榜 + 单败淘汰 bracket 树 + 瑞士/循环轮次分组，显示 Bot 名（非裸 ID）；德州每个 70 手计分场完成后立即按 3/1/0 入榜，胜+平+负恒等实际计分场数，阶段进度另列对手系列、对局记录和计分场；复式顶层空 winner 不得显示为平局；阶段结束可落**正式名次**（破同分，`contests/ranking.py`） |
| 版本冻结与换 Bot | 已发布 pairing 冻结 Bot 与版本；published/rest 换 Bot 只影响尚未发布的后续轮次/阶段，不回写已有排期 |

### 3.6 社交与互动
| 需求 | 验收标准 |
|------|---------|
| 关注 / 收藏 | 关注用户、收藏 Bot，触发通知（被关注） |
| 评论 / 点赞 | 对局与 Bot 可评论（删自己的/admin 可删任何）、点赞、浏览计数、点赞榜 |
| 平台通信 | conversation/message 是站内真相，支持收件箱/已发/线程/已读/回复；用户只能访问自己的 participant thread，首期只开放平台/admin↔user，不开放任意私信 |
| 通知兼容 | 对局完成/被关注/赛事/评论的新写入经 communications 生成站内消息，再原子生成旧 `notifications` 读投影；旧行不回填成新会话 |
| 邮件投递 | 邮件仅是 delivery；业务请求不直连 SMTP，单进程 worker 可停止/恢复、优先验证/重置、指数退避并限定尝试。唯一 key+确定性 Message-ID 支持去重，语义明确为有界 at-least-once |
| 管理员广播 | 受众可选 active users / role / game Bot owners / contest entrants / selected public users；预览返回去重 dry-run count 与短期批准 token，token 绑定受众快照 hash+subject/body/channel；二次批准、固定批次、取消、重试与投递统计均可审计 |
| 全局搜索 | Cmd+K 命令面板，聚合搜 Bot/用户/对局 |

### 3.6.1 Bug 反馈

| 需求 | 验收标准 |
|------|---------|
| 小白式表单 | category/impact/title/body/current route 字段明确；访客经 captcha+独立限流可提交，登录用户可列出/读取自己的反馈 |
| 通信复用 | 每个 bug_report 绑定同一 conversation，管理员与报告者在同一 thread 追问/回复；状态为 new/acknowledged/needs_info/in_progress/resolved/duplicate/wont_fix，状态变化与回复事件追加保存 |
| 诊断隐私 | 只保存 build/route/服务端 role/粗粒度 browser+OS/viewport/locale/timezone、失败 API 模板/status/trace ID 与公开 match/contest/queue 摘要；拒绝 raw UA、cookie/token/email/实名/路径/raw stderr/private debug/底牌 |
| 可选附件 | 不接受 JSON base64；独立 multipart 仅允许图片，同时校验 magic+MIME+尺寸/像素/帧数，单个 ≤5 MiB、每报告 ≤5，SHA-256 与隔离路径元数据入库，路径不对外 |

### 3.7 经验与等级
| 需求 | 验收标准 |
|------|---------|
| XP 奖励 | 对局参与 +10/胜利 +15、赛事参与 +50、评论 +2、被关注 +3 |
| 等级 | `xp_for_level(N)=100×N×(N+1)/2`，主页显示等级徽章 + 经验进度条 |
| 等级展示 | 主页显示等级徽章与经验进度，升级阈值由统一公式计算 |

### 3.8 管理后台
| 需求 | 验收标准 |
|------|---------|
| 统一后台 | 仪表盘、用户/Bot/对局/赛事管理、日志、邮箱/广播与 Bug 处理使用一致信息架构；广播预览后持令牌二次批准，不展示运行时/赛制/事务邮件模板编辑器 |
| 代码唯一配置 | 双资源容量、前台 aging、用户上限、超时、自动公平与闲时门禁、scheduler、循环赛护栏与内置赛制模板只随代码评审发布；旧数据库值和前端请求不能覆盖；唯一可变自动开关为 `execution_control.auto_enabled`，开启只表示允许闲时运行，不绕过空闲条件 |
| 安全中止与删除 | 活跃对局只允许经 orchestrator 取消并收敛为 `aborted`，不得手工伪造 running/completed；用户/Bot/赛事存在活跃引用时拒绝硬删 |

### 3.9 站点与后台调度
| 需求 | 验收标准 |
|------|---------|
| 站点配置 | 站名/Logo/公告/About 可配（admin） |
| 全来源执行队列 | manual/human/contest/auto 全部先写持久 job，四类共享代码硬顶 `6 match slots + 12 sandbox units`。节能/赛事 Docker 座位各占 1 unit，本地 Bot/真人座位占 0；每个 job 仍占 1 slot。job 冻结环境、档位版本及 sandbox/CPU/内存向量，claim 取 affinity、逻辑 CPU、各级 cgroup 与物理内存的有效预算，因而低配、人机、本地与赛事组合的实际并发为 1–6，赛事不得降档，显式配置只能收紧。`starting/running/settling` 均占容量；同一非 human Bot 不得跨 active job 重叠；Match/index/replay/policy 只在原子 claim 时创建；Docker 不确定时安全暂停且不释放容量 |
| 闲时公平自动排位 | 默认允许，但启用不等于立即运行；只作为全局队列的 `source=auto` 后台 producer。manual/human/contest 的 queued/active、真实赛事 `running/rest` 全程 guard、已有待开 pairing 且 `starts_at` 进入未来 5 分钟的 `published` 保护窗，或未满 5 分钟的空闲/冷却均阻止 auto 生成与 claim；showcase 明确排除，远期或手动开赛的 published 不会无期占用 guard。全部 active 槽清空后每次至多生成 1 个候选并运行 1 场，结束后重新冷却；auto claim 还必须预留最高档一场赛事的 1 match slot + 2 sandbox units + 4000 毫核 + 4096 MiB。任一前台成功入队/重试或真实赛事 guard 取消 queued auto，在途 auto 以 `auto_yield_foreground` 安全收口且精确清理后才释放容量。每个 owner/game 只消费当前唯一排行榜 Bot，按游戏/lane/所有者/pair 轮转并平衡对手与座位，永久 decision 审计映射到通用 job；管理员单纯关闭开关不取消在途局，关闭后在途局自然结束 |

## 4. 非功能需求

| 类别 | 需求 | 指标 / 实现 |
|------|------|------------|
| **性能** | 单场对局低延迟 | holdem Bot 单步决策固定超时 60s；Gomoku/Pencil 每方累计 900s（固定，含人类局），Pencil 赛事 ETA 每局按双方合计 1800s 保守估算；沙箱镜像准备不计入 Bot 决策时间；全来源代码上限 6 槽，主机资源向量门禁可把实际并发收紧到 1–6 |
| **性能** | 前端首屏快 | React.lazy 代码分割，主包 gzip ~115KB；recharts 等重依赖隔离到 BotDetail chunk |
| **安全** | Bot 沙箱隔离 | Docker 共用 `--network=none --read-only --cap-drop=ALL --user 65534` 等硬化；日常节能与上传预检每 Bot 1 CPU/512 MiB，锦标赛每 Bot 2 CPU/2 GiB；本地 Bot 由用户电脑主动连接且不占平台沙箱，所有持久任务按冻结版本解析，不允许任意资源组合或降档 |
| **安全** | 接口限流 | 分级 IP 限流（auth 20/60s、challenge 8/60s、upload 6/60s 等），可 `BZ_RATE_LIMIT` 开关 |
| **安全** | 认证 | 密码 hash 存储，session token，cookie `bz_session`，验证码防爆破 |
| **可靠** | 数据持久 | SQLite 单文件，自带 `_migrate` 自愈（补列/重建表），向后兼容旧库 |
| **可靠** | 本地 supervisor | 同 DB 由邻接 flock 保证单 dispatcher；Docker 固定 canonical 本机 socket，以稳定 `BZ_INSTANCE_KEY` 和 job/attempt/slot label 精确清理；旧 active 状态与控制结果不确定均 fail closed |
| **可靠** | 通信副作用可恢复 | 站内消息先持久化，邮件后投递；worker 启动恢复中断 claim，广播受众固定，不让 SMTP 阻塞/回滚 API 事务 |
| **可靠** | 隔离 QA | `BZ_QA_INSTANCE=1` 时拒绝 50380、主库同路径/同 inode 与主 checkout 运行时写目标；Vite 同样拒绝代理到 50380 |
| **可靠** | 日志可查 | 统一日志 `logs/app.log`（5MB×5 轮转），Bot stderr 尾部 4KB 捕获，admin 网页查看 |
| **可维护** | 新增游戏低成本 | 实现 `games/<game>/`（engine/protocol/result/templates/spec）+ 常量/注册 + 前端 GameViewSpec；同构对局表由迁移模板创建，赛制/编排主流程禁止增加 `if game_id` 分支 |
| **可维护** | 常量集中 | 所有状态码/类型/`REGISTERED_ENGINES`/平台 settings 键名集中在 `schema.py` |
| **可用性** | 响应式 | 桌面/平板/手机三档适配，移动端汉堡菜单 + 表格列隐藏 |
| **可用性** | 暗色模式 | OKLCH 双主题，浅色默认 + 暗色对等，顶栏一键切换 |
| **可用性** | 无障碍 | aria-label / focus-visible / 键盘导航 / WCAG AA 对比度 |

## 5. 需求覆盖追溯矩阵

| 需求模块 | 实现位置 | 测试覆盖 | 文档 |
|----------|---------|---------|------|
| 账号认证 | `auth/` | test_auth | wiki（功能说明散见） |
| Bot 管理 | `bots/` + api_routes | test_settings_mybots | wiki/BOT_DEV、BOT_DETAIL |
| 对局编排 | `matches/execution_queue+orchestrator+runner`、`store/execution` | test_execution_queue、test_engine、test_protocol | wiki/GUIDE、doc/RUNTIME |
| 人类对战 / 棋钟 | orchestrator + runner + WS /play | test_human_match、test_chess_clock | wiki/GUIDE、PENCIL |
| 评分排行 | `rating/glicko2` + `store/db.py` 数值投影 | test_numeric_ranking、test_leaderboard_density、test_rating_rebuild | wiki/GUIDE |
| 赛事 | `contests/`（模板聚合自 games） | test_contest_*、test_game_templates | wiki/GUIDE、CONTEST_BRACKET |
| 社交 | api_routes + store | test_social、test_comments_likes | wiki/GUIDE |
| 通信/通知 | `communications/` + `notifications/` 兼容门面 | test_communications_feedback、test_notifications、test_auth | wiki/GUIDE、doc/DESIGN/SECURITY |
| Bug 反馈 | `communications/feedback.py` + `communications/api.py` | test_communications_feedback | wiki/GUIDE、doc/SECURITY |
| 经验等级 | store + api_routes | test_xp_level | wiki/GUIDE |
| 沙箱运行时 | `runtime/` | test_runtime、test_runtime_settings | doc/RUNTIME |
| 各游戏引擎 | `games/<game>/`（自包含子包，真实现全在 games/） | test_engine、test_board_engines、test_result_contract、test_game_registry、test_import_cycles | wiki/PROTOCOL、GOMOKU、PENCIL、TEXAS、doc/JUDGE_CODE |
| 架构契约 | games 注册表 + 通用层无游戏分支 | test_tongyong_layer_no_game_branches、test_despecialization、test_physical_reorg | doc/DESIGN、AGENTS.md |
| 前端 | `frontend/`（`src/games/` 注册表 + canvas） | Playwright `frontend/e2e/` + browser/screenshot verify | doc/DESIGN §5、doc/TESTING |

> 上表给出需求到实现、测试入口与文档的追溯关系；是否通过本次验收，以目标提交上的完整执行结果为准，见 [TESTING.md](./TESTING.md)。

> 返回 [doc/INDEX.md](./INDEX.md)
