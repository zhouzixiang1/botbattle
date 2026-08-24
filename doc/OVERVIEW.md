# 项目总览

> 本文档为甲方与项目干系人提供 botbattle 平台的全貌：定位、能力、技术栈、目录结构与交付物。

## 1. 项目定位

**botbattle** 是一个**多游戏 Bot 线上对战平台**：参赛者平时可在平台节能沙箱或自己的电脑上测试 Bot，组织者开赛后由平台在统一的高性能赛事沙箱中裁判并给出名次；平台同时提供实时观赛、对局回放、Glicko-2 评分排行榜与人类亲自上场。

支持三款游戏（这是有意的产品边界，非技术限制）：

| 游戏 | game_id | 规则摘要 |
|------|---------|---------|
| 德州扑克（HU NLHE） | `holdem` | 固定 70 手、每手 20000 筹码、50/100 盲注；raise=额外下注量 |
| 五子棋 | `gomoku` | 15×15、26 种指定开局、三手交换、五手二打、黑方禁手、每方 900 秒 |
| 点格棋（Dots and Boxes） | `pencil` | 固定 N=6 点（交错网格 11×11、25 格）、成格连走计分 |

## 2. 核心能力一览

| 能力域 | 说明 |
|--------|------|
| **Bot 上传与沙箱对战** | 唯一接受 Linux x86_64 ELF；拒绝 PE、Mach-O、ARM64 ELF 与脚本；Docker 硬隔离（CPU/内存/网络/文件系统全限制） |
| **三种执行环境** | 日常挑战与自动排位使用每 Bot 1 核/512 MiB 节能沙箱；正式赛事自动使用每 Bot 2 核/2 GiB 赛事沙箱；本地 Bot 从用户电脑主动建立 WSS 连接，平台只裁判且练习局不计平台排行榜 |
| **三游戏裁判引擎** | 平台内置裁判模块，Bot 通过 stdin/stdout 行协议交互；赛制/编排主流程经统一 GameSpec 与结果契约调用，不写游戏名分支 |
| **实时观赛** | SSE 推送对局事件流，前端棋盘/牌桌逐步可视化 |
| **对局回放与记录** | 完整事件录制，支持播放/暂停/步进/倍速/逐手跳转；德州、五子棋、点格棋终态均可导出脱敏、确定性的单场 canonical JSON v1 对局日志，五子棋另有专项棋谱 JSON v1。两类导出都不臆造未落稳事件，专项棋谱不宣称为组委会官方电子格式 |
| **对局身份与检索** | 首页、历史、Bot 详情、搜索、赛事、回放与管理端共用座位身份契约：显示 Bot 与所属用户或实际真人，并明确自动排位、用户挑战、自博弈、真人对战、锦标赛等性质；owner 可把 Bot 不可逆地移出管理库存，但平台以墓碑保留版本、评分、赛事和历史身份，不用硬删除抹掉既有记录 |
| **私有 Bot 调试** | 可选响应 `debug` 经限额、清洗、脱敏后独立保存；终局按 Bot owner/赛事组织者/admin 授权查看，不进入公共回放或任何公开导出 |
| **自主挑战位置** | Bot-vs-Bot 发起者可选择自己的 Bot 位于任一游戏位置；Bot、版本、执行环境与本地连接整体换位，普通用户的本人 Bot 授权不因换位放宽；真人仍固定在第二方 |
| **人类 vs Bot** | WebSocket 实时交互，人类可亲自上场；与其他来源共享全局 match slots，只占 1 个沙箱单元，不计平台排行榜 |
| **全来源执行队列** | 人工、人机、赛事与自动排位统一写持久 job；按 8 vCPU / 16 GiB 基准代码硬顶为 2 场并发/4 个平台 Bot 运行位，实际资源不足会继续排队；用户挑战、人机和真实赛事属于前台，自动排位只在前台清空并连续空闲 5 分钟后使用至多 1 个槽；挑战/人机返回 HTTP 202 请求，支持刷新恢复、查询、取消与中断后重试，Match 仅在原子 claim 时创建 |
| **Glicko-2 数值排行榜** | 按游戏分别排名；每个账号、每款游戏只展示一个当前派遣的排行榜 Bot，其他 Bot 保留历史评分并可用于练习/赛事；展示 1-based 名次/百分位、Rating、RD、95% 置信区间、不同对手数、变化量、最近对局与无名次计分样本 |
| **赛事系统** | 6 种赛制阶段（单/双循环、分组、瑞士、单败淘汰）+ 10 个内置模板（含预赛/决赛），完整生命周期（草稿→报名→发布当前阶段排期→进行→休息→结束），积分榜 + 对阵图 + 正式名次；公开成绩 CSV 永不含实名，组织者名单用报名/用户/Bot 稳定 ID 关联账号与显示名；实名赛事仅允许选手本人报名并保留报名时资料快照，管理员纠错代报名受审计；可部署六个明确标注、不可写的客户演示快照，回放仍由真实裁判链生成 |
| **社交与互动** | 关注用户、收藏 Bot、对局/Bot 评论、点赞、浏览计数、点赞榜 |
| **平台通信** | conversation/message 作为站内真相，邮件为异步 delivery；旧通知 API 兼容投影、用户/admin 线程、固定快照广播、Bug 反馈与诊断附件 |
| **用户体系** | 注册/登录/邮箱验证、个人主页、资料编辑、头像、经验与等级 |
| **全局搜索** | Cmd+K 命令面板，聚合搜索 Bot / 用户 / 对局 |
| **管理后台** | 统一侧栏信息架构与紧凑邮箱工作台覆盖仪表盘、用户/Bot/对局/赛事、日志、收发会话、受众快照群发、失败重试与 Bug 诊断/回复；运行参数、赛制模板和事务邮件模板均由代码唯一配置，不提供网页编辑器 |
| **闲时公平自动排位** | 仅作为全局执行队列的 `source=auto` 后台 producer；候选先由“每账号每游戏唯一排行榜 Bot”收敛，再按游戏/bootstrap/established 通道与所有者轮转，平衡配对与先后手。启用后仍等待前台清空及 5 分钟空闲/冷却，每次至多保留 1 个候选、运行 1 场；自动请求不参与跨来源 aging，不计入前台请求的前方任务与 ETA。任一前台请求或真实赛事到达时自动局安全让路，精确清理 sandbox 后才释放容量 |
| **站点可配置** | 站名 / Logo / 公告 / 关于（admin 可配） |

## 3. 技术栈

| 层 | 技术 | 版本 |
|----|------|------|
| **后端** | Python + FastAPI + uvicorn + SQLite | Python ≥ 3.12，FastAPI ≥ 0.115 |
| **CLI** | typer | 入口 `botzone`（serve / create-admin） |
| **评分** | Glicko-2（自实现，无外部依赖） | — |
| **执行环境** | Docker（Linux x86_64 ELF: debian:bookworm-slim）+ 用户端 WSS 本地 Bot | 节能 1 核/512 MiB；赛事 2 核/2 GiB；`BZ_BOT_LOCAL=1` 仅是平台开发测试回退，不等同于用户本地 Bot |
| **邮件** | lifespan delivery worker 异步 SMTP（注册验证 / 密码重置高优先级，普通通知/广播可选） | python 标准库 smtplib；有界指数退避、确定性 Message-ID |
| **前端** | React + Vite + Tailwind CSS v4 + shadcn/ui | React 19 / Vite 8 / Tailwind v4（CSS-first） |
| **UI 组件** | shadcn/ui（new-york）+ Radix UI + lucide-react + recharts | 26 个共享原语 |
| **暗色模式** | next-themes（class 策略）+ OKLCH 双主题 token | 浅色默认 + 暗色对等 |
| **路由** | react-router-dom（HashRouter） | v7 |
| **实时通信** | SSE（观赛）+ WebSocket（人类对战）+ Bearer 鉴权 WSS（本地 Bot） | — |

## 4. 目录结构

```
botbattle/
├── bzplat/                    # 应用代码（Python 包名 bzplat，刻意规避标准库 platform）
│   ├── backend/
│   │   ├── main.py            # FastAPI 应用工厂 + 装配 + lifespan
│   │   ├── api_routes.py      # 主 REST + SSE + WebSocket
│   │   ├── auth/              # 认证（13 路由 + 验证码 + 依赖）
│   │   ├── games/             # 【游戏单一真相】GameSpec + registry + holdem/gomoku/pencil 自包含子包
│   │   ├── store/             # SQLite（Store + schema.py + execution job/attempt/control；matches 按游戏分表）
│   │   ├── runtime/           # Linux ELF 资源档位、本地 Docker supervisor、本地 Bot 连接协调
│   │   ├── matches/           # execution_queue dispatcher + orchestrator + runner
│   │   ├── contests/          # 赛制 templates/stages/manager/ranking（模板由 games 聚合）
│   │   ├── communications/    # 会话/消息/delivery、广播、Bug 反馈、诊断与 worker
│   │   ├── notifications/     # 旧业务 facade + 通知偏好（写入委托 communications）
│   │   ├── bots/ rating/ mail/
│   │   ├── security.py / logging_config.py / crypto.py / cli.py
│   │   └── tests/             # 后端 pytest（含架构契约、并发与恢复守护）
│   └── frontend/              # React 19 + Vite 8 + Tailwind v4 + shadcn/ui
│       └── src/
│           ├── components/ui/     # 26 个 shadcn 共享原语
│           ├── components/shell/  # 全局 Shell + 导航 + Cmd+K（lg+ 侧栏，含访客）
│           ├── games/             # 前端 GameViewSpec 注册表 + 每游戏 canvas/reducer
│           ├── pages/             # lazy 页面模块（含 admin）
│           └── lib/               # games / utils / markdown 等
├── doc/                       # 本目录：6 份核心交付文档 + 现行专项文档 + INDEX
├── wiki/                      # 面向 Bot 玩家的规则/协议/开发指南文档
├── contracts/                 # 协议 JSON Schema
├── samples/                   # 与现行协议逐字绑定的三游戏 C / Python 样例 Bot
├── scripts/                   # 运维/QA 脚本 + 用户端 local_ai_client.py
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
| **规则与协议文档** | 三游戏规则、对局协议、Bot 开发指南、本地 Bot 接入 | `wiki/` |
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
