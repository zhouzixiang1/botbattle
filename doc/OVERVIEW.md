# 项目总览

> 本文档为甲方与项目干系人提供 botbattle 平台的全貌：定位、能力、技术栈、目录结构与交付物。

## 1. 项目定位

**botbattle** 是一个**多游戏 Bot 线上对战平台**：用户上传自行编写的 Linux x86_64 ELF Bot，平台在 Docker 安全沙箱中自动运行对局，提供实时观赛、对局回放、Glicko-2 评分排行榜、组织者赛事、人类亲自上场等功能。

支持三款游戏（这是有意的产品边界，非技术限制）：

| 游戏 | game_id | 规则摘要 |
|------|---------|---------|
| 德州扑克（HU NLHE） | `holdem` | 固定 70 手、每手 20000 筹码、50/100 盲注；raise=额外下注量 |
| 五子棋 | `gomoku` | 15×15、黑先、五连即胜（含长连）、无禁手 |
| 点格棋（Dots and Boxes） | `pencil` | 固定 N=6 点（交错网格 11×11、25 格）、成格连走计分 |

## 2. 核心能力一览

| 能力域 | 说明 |
|--------|------|
| **Bot 上传与沙箱对战** | 唯一接受 Linux x86_64 ELF；拒绝 PE、Mach-O、ARM64 ELF 与脚本；Docker 硬隔离（CPU/内存/网络/文件系统全限制） |
| **三游戏裁判引擎** | 平台内置裁判模块，Bot 通过 stdin/stdout 行协议交互；赛制/编排主流程经统一 GameSpec 与结果契约调用，不写游戏名分支 |
| **实时观赛** | SSE 推送对局事件流，前端棋盘/牌桌逐步可视化 |
| **对局回放** | 完整事件录制，支持播放/暂停/步进/倍速/逐手跳转 |
| **人类 vs Bot** | WebSocket 实时交互，人类可亲自上场（独立并发，不计评分） |
| **Glicko-2 排行榜** | 自实现评分系统，按游戏分别排名；含段位称号（6 档）与排名变化趋势 |
| **赛事系统** | 6 种赛制阶段（单/双循环、分组、瑞士、单败淘汰）+ 10 个内置模板（含预赛/决赛），完整生命周期（草稿→报名→发布当前阶段排期→进行→休息→结束），积分榜 + 对阵图 + 正式名次 |
| **社交与互动** | 关注用户、收藏 Bot、对局/Bot 评论、点赞、浏览计数、点赞榜 |
| **通知系统** | 站内通知 + 可选邮件提醒（对局完成/被关注/赛事/评论） |
| **用户体系** | 注册/登录/邮箱验证、个人主页、资料编辑、头像、经验与等级 |
| **全局搜索** | Cmd+K 命令面板，聚合搜索 Bot / 用户 / 对局 |
| **管理后台** | 7 个 Tab：仪表盘、用户/Bot/对局/赛事管理、日志、邮件；运行参数与赛制模板均由代码唯一配置，不提供网页编辑器 |
| **闲时自动对局** | 后台自动调度 ladder 对局维护天梯（陈旧度优先 + 定级赛优先） |
| **站点可配置** | 站名 / Logo / 公告 / 关于（admin 可配） |

## 3. 技术栈

| 层 | 技术 | 版本 |
|----|------|------|
| **后端** | Python + FastAPI + uvicorn + SQLite | Python ≥ 3.12，FastAPI ≥ 0.115 |
| **CLI** | typer | 入口 `botzone`（serve / create-admin） |
| **评分** | Glicko-2（自实现，无外部依赖） | — |
| **沙箱** | Docker（Linux x86_64 ELF: debian:bookworm-slim） | 必须，`BZ_BOT_LOCAL=1` 可在兼容 Linux 主机退回本机（仅测试） |
| **邮件** | SMTP（注册验证 / 密码重置 / 通知） | python 标准库 smtplib |
| **前端** | React + Vite + Tailwind CSS v4 + shadcn/ui | React 19 / Vite 8 / Tailwind v4（CSS-first） |
| **UI 组件** | shadcn/ui（new-york）+ Radix UI + lucide-react + recharts | 26 个共享原语 |
| **暗色模式** | next-themes（class 策略）+ OKLCH 双主题 token | 浅色默认 + 暗色对等 |
| **路由** | react-router-dom（HashRouter） | v7 |
| **实时通信** | SSE（观赛）+ WebSocket（人类对战） | — |

## 4. 目录结构

```
botbattle/
├── bzplat/                    # 应用代码（Python 包名 bzplat，刻意规避标准库 platform）
│   ├── backend/
│   │   ├── main.py            # FastAPI 应用工厂 + 装配 + lifespan
│   │   ├── api_routes.py      # 主 REST + SSE + WebSocket
│   │   ├── auth/              # 认证（13 路由 + 验证码 + 依赖）
│   │   ├── games/             # 【游戏单一真相】GameSpec + registry + holdem/gomoku/pencil 自包含子包
│   │   ├── store/             # SQLite（Store + schema.py 常量唯一来源；matches 按游戏分表）
│   │   ├── runtime/           # Linux ELF 沙箱 + config 不可变运行配置 + limits 资源硬顶
│   │   ├── matches/           # 编排 orchestrator + runner + auto_matcher
│   │   ├── contests/          # 赛制 templates/stages/manager/ranking（模板由 games 聚合）
│   │   ├── notifications/     # 站内通知 + 邮件偏好
│   │   ├── bots/ rating/ mail/
│   │   ├── security.py / logging_config.py / crypto.py / cli.py
│   │   └── tests/             # 后端 pytest（含架构契约、并发与恢复守护）
│   └── frontend/              # React 19 + Vite 8 + Tailwind v4 + shadcn/ui
│       └── src/
│           ├── components/ui/     # 26 个 shadcn 共享原语
│           ├── components/shell/  # 全局 Shell + 导航 + Cmd+K（lg+ 侧栏，含访客）
│           ├── games/             # 前端 GameViewSpec 注册表 + 每游戏 canvas/reducer
│           ├── pages/             # lazy 页面模块（含 admin）
│           └── lib/               # tiers / utils / markdown 等
├── doc/                       # 本目录：6 份核心交付文档 + 现行专项文档 + INDEX
├── wiki/                      # 面向 Bot 玩家的规则/协议/开发指南文档
├── contracts/                 # 协议 JSON Schema
├── samples/                   # 与现行协议逐字绑定的三游戏 C / Python 样例 Bot
├── scripts/                   # 运维脚本（启停/重建/冒烟/压测/浏览器验收/种子）
├── deploy/                    # systemd unit
├── pyproject.toml             # Python 包定义 + 依赖 + pytest 配置
├── AGENTS.md                  # 开发规范（架构分层 + 约束 + 文档规范）
└── README.md                  # 项目门面
```

## 5. 交付物清单

| 交付物 | 说明 | 位置 |
|--------|------|------|
| **源代码** | 后端 + 前端完整源码 | `bzplat/` |
| **交付文档**（本文档集） | 6 份核心交付文档及专项/历史说明 | `doc/` |
| **规则与协议文档** | 三游戏规则、对局协议、Bot 开发指南 | `wiki/` |
| **测试套件** | 后端 pytest + 隔离 API/E2E 脚本 + Playwright 真浏览器回归 | `bzplat/backend/tests/`、`bzplat/frontend/e2e/`、`scripts/` |
| **部署配置** | systemd unit + 启停脚本 | `deploy/`、`scripts/platform-ctl.sh` |
| **样例 Bot** | 三游戏样例源码与可上传 Linux x86_64 ELF | `samples/` |

## 6. 如何使用本文档

| 如果你想了解… | 请阅读 |
|----------------|--------|
| 项目是什么、能做什么 | 本文档（OVERVIEW） |
| 需求是什么、是否都满足了 | [REQUIREMENTS.md](./REQUIREMENTS.md) |
| 系统怎么设计的 | [DESIGN.md](./DESIGN.md) |
| 怎么开发、部署、运维 | [DEVELOPMENT.md](./DEVELOPMENT.md) |
| 测得怎么样、能否验收 | [TESTING.md](./TESTING.md) |
| 整体交付情况、成果与遗留 | [SUMMARY.md](./SUMMARY.md) |
| 怎么写 Bot、游戏规则 | [../wiki/](../wiki/)（面向 Bot 玩家） |

> 返回 [doc/INDEX.md](./INDEX.md)
