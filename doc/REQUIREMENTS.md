# 需求分析

> 本文档定义 botbattle 平台的功能与非功能需求，并以追溯矩阵确认每项需求的实现覆盖。

## 1. 项目背景与目标

### 1.1 背景
Bot 竞赛平台（对标 Botzone）允许用户提交自动化程序（Bot），由平台托管运行对局并排名。传统方案（如 Botzone 默认）每回合启停进程、聚合请求响应，开销大且不适合长驻对局。

### 1.2 目标
构建一个**整场对局长驻、沙箱隔离、多游戏可扩展**的 Bot 对战平台，提供：
1. 用户上传 Bot → 平台沙箱安全运行 → 自动判胜与评分。
2. 实时观赛、完整回放、Glicko-2 排行榜。
3. 组织者赛事（多赛制）、人类亲自上场、社交互动。
4. 三款游戏（德州扑克/五子棋/点格棋），且**新增游戏对赛制/编排层零改动**。

## 2. 用户角色

| 角色 | 标识 | 核心诉求 |
|------|------|---------|
| **玩家** | `user` | 上传 Bot、发起挑战、观赛、查看排行与战绩、社交互动 |
| **组织者** | `organizer` | 创建与管理赛事（赛制模板、报名、推进阶段） |
| **管理员** | `admin` | 全局管理（用户/Bot/对局/赛事/运行时配置/裁判参数/日志） |
| **访客** | 未登录 | 浏览排行榜、对局回放、Bot/用户主页、Wiki |

## 3. 功能需求

### 3.1 账号与认证
| 需求 | 验收标准 |
|------|---------|
| 注册 / 登录 / 登出 | 用户名 `^[a-zA-Z][a-zA-Z0-9_]{2,31}$`，密码 ≥8，session 7 天，图形验证码防爆破 |
| 邮箱验证 | 注册后发验证码（TTL 30 分钟），验证后方可登录 |
| 密码重置 | 邮件重置链接，防枚举（不存在也返回成功语义） |
| 个人资料 | 改显示名 / 简介 / 头像（png/jpeg/webp/gif ≤2MB，存本地 `avatars/`）/ 改密码 |

### 3.2 Bot 管理
| 需求 | 验收标准 |
|------|---------|
| 上传 Bot | 支持 ELF/PE 二进制（Mach-O 拒绝），魔数自动识别 os/arch/format，按游戏分类 |
| 版本管理 | 同一 Bot 可上传多版本，可切换激活版本，可删/改名/改简介 |
| Bot 详情页 | 信息卡（评级/胜率/战绩/段位）+ 对局历史 + 对手战绩 + 评分曲线（recharts）|

### 3.3 对局与观赛
| 需求 | 验收标准 |
|------|---------|
| Bot vs Bot 挑战 | 选对手（全部/我的/按用户搜索）+ 选游戏 +选手数，沙箱运行，完成后计 Glicko |
| 实时观赛 | SSE 推送事件流，前端棋盘/牌桌逐步渲染 |
| 对局回放 | 完整事件录制，播放/暂停/步进/倍速（0.5x-4x）/逐手跳转/进度拖动 |
| 人类 vs Bot | WebSocket 落子回传，独立并发槽（默认 4），per-user ≤1，不计 Glicko |
| 自博弈 | 同一 owner 的两个不同 Bot 可对战，走普通挑战 |

### 3.4 评分与排行
| 需求 | 验收标准 |
|------|---------|
| Glicko-2 评分 | 自实现，按游戏分别排名；对局完成自动更新（contest/human 除外） |
| 段位称号 | 6 档：新手(<1600)/进阶/熟练/高手/专家/大师(≥2200)，彩色徽章 |
| 排名趋势 | rating_history 记录评分变化，排行榜显示升降箭头 |

### 3.5 赛事系统
| 需求 | 验收标准 |
|------|---------|
| 赛制模板 | 6 种阶段（单/双循环、分组单/双循环、瑞士、单败淘汰）+ 2 种计分 + 7 内置模板 |
| 赛事生命周期 | draft→open→running→(rest)→finished，组织者可 open/register/dispatch/start/resume/advance |
| 积分榜与对阵图 | 实时积分榜 + 单败淘汰 bracket 树 + 瑞士/循环轮次分组，显示 Bot 名（非裸 ID） |
| 休息期换 Bot | 阶段间休息期允许选手更换派遣 Bot |

### 3.6 社交与互动
| 需求 | 验收标准 |
|------|---------|
| 关注 / 收藏 | 关注用户、收藏 Bot，触发通知（被关注） |
| 评论 / 点赞 | 对局与 Bot 可评论（删自己的/admin 可删任何）、点赞、浏览计数、点赞榜 |
| 通知 | 站内通知（对局完成/被关注/赛事/评论）+ 可选邮件（按偏好开关）+ 铃铛未读提醒 |
| 全局搜索 | Cmd+K 命令面板，聚合搜 Bot/用户/对局 |

### 3.7 经验与等级
| 需求 | 验收标准 |
|------|---------|
| XP 奖励 | 对局参与 +10/胜利 +15、赛事参与 +50、评论 +2、被关注 +3 |
| 等级 | `xp_for_level(N)=100×N×(N+1)/2`，主页显示等级徽章 + 经验进度条 |
| 等级 gating | 数据集下载需等级 ≥1 |

### 3.8 管理后台
| 需求 | 验收标准 |
|------|---------|
| 10 Tab 后台 | 仪表盘、用户/Bot/对局/赛事管理、邮件模板与发件箱、运行时热配置、裁判参数热调、赛制模板设计器、日志查看 |
| 运行时热配置 | 并发上限/超时/auto-match 参数可热改（资源硬顶不可抬高） |
| 裁判参数热调 | 德州筹码/盲注/手数、五子棋盘大小可热改 |

### 3.9 数据与站点
| 需求 | 验收标准 |
|------|---------|
| 对局数据集下载 | 按游戏×月份打包 gzip JSON 行，等级 ≥1 gating |
| 站点配置 | 站名/Logo/公告/About 可配（admin） |
| 闲时自动对局 | 后台自动调度 ladder 对局维护天梯（陈旧度 + 定级赛优先） |

## 4. 非功能需求

| 类别 | 需求 | 指标 / 实现 |
|------|------|------------|
| **性能** | 单场对局低延迟 | Bot 决策默认超时 60s（可配 1-300）；沙箱启动 ~1s；半负载并发 ceiling=`max(1,cpu//4)` |
| **性能** | 前端首屏快 | React.lazy 代码分割，主包 gzip ~115KB；recharts 等重依赖隔离到 BotDetail chunk |
| **安全** | Bot 沙箱隔离 | Docker: `--network=none --memory=512m --cpus=1 --read-only --cap-drop=ALL --user 65534` |
| **安全** | 接口限流 | 分级 IP 限流（auth 20/60s、challenge 8/60s、upload 6/60s 等），可 `BZ_RATE_LIMIT` 开关 |
| **安全** | 认证 | 密码 hash 存储，session token，cookie `bz_session`，验证码防爆破 |
| **可靠** | 数据持久 | SQLite 单文件，自带 `_migrate` 自愈（补列/重建表），向后兼容旧库 |
| **可靠** | 日志可查 | 统一日志 `logs/app.log`（5MB×5 轮转），Bot stderr 尾部 4KB 捕获，admin 网页查看 |
| **可维护** | 新增游戏低成本 | 赛制/编排层零改动：实现 `games/<game>/`（engine/protocol/result/tiers/templates/spec）+ schema 注册 + 前端 GameViewSpec，**禁止**通用层 `if game_id` 分支 |
| **可维护** | 常量集中 | 所有状态码/类型/`REGISTERED_ENGINES`/平台 settings 键名集中在 `schema.py` |
| **可用性** | 响应式 | 桌面/平板/手机三档适配，移动端汉堡菜单 + 表格列隐藏 |
| **可用性** | 暗色模式 | OKLCH 双主题，浅色默认 + 暗色对等，顶栏一键切换 |
| **可用性** | 无障碍 | aria-label / focus-visible / 键盘导航 / WCAG AA 对比度 |

## 5. 需求覆盖追溯矩阵

| 需求模块 | 实现位置 | 测试覆盖 | 文档 |
|----------|---------|---------|------|
| 账号认证 | `auth/` | test_auth | wiki（功能说明散见） |
| Bot 管理 | `bots/` + api_routes | test_settings_mybots | wiki/BOT_DEV、BOT_DETAIL |
| 对局编排 | `matches/orchestrator+runner` | test_engine、test_protocol | wiki/MATCH |
| 人类对战 | orchestrator + WS /play | test_human_match | wiki/MATCH |
| 评分排行 | `rating/glicko2` + `games/*/tiers.py` | test_tiers、test_engine | wiki/TIER |
| 赛事 | `contests/`（模板聚合自 games） | test_contest_*、test_game_templates | wiki/CONTEST_FORMAT、CONTEST_BRACKET |
| 社交 | api_routes + store | test_social、test_comments_likes | wiki/SOCIAL、COMMENTS_LIKES |
| 通知 | `notifications/` | test_notifications | wiki/NOTIFICATIONS |
| 经验等级 | store + api_routes | test_xp_level | wiki/XP_LEVEL |
| 沙箱运行时 | `runtime/` | test_runtime、test_runtime_settings | wiki/RUNTIME |
| 三游戏引擎 | `games/<game>/`（engine 为 shim） | test_engine、test_board_engines、test_result_contract、test_game_registry、test_import_cycles | wiki/PROTOCOL、GOMOKU、PENCIL、TEXAS、JUDGE_CODE |
| 架构契约 | games 注册表 + 通用层无游戏分支 | test_tongyong_layer_no_game_branches、test_despecialization、test_physical_reorg | doc/DESIGN、AGENTS.md |
| 前端 | `frontend/`（`src/games/` 注册表 + canvas） | browser_verify / screenshot_verify | doc/DESIGN §5 |

> 所有功能需求均有对应的代码实现 + 自动化测试 + wiki 文档，覆盖率完整。

> 返回 [doc/INDEX.md](./INDEX.md)
