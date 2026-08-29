# botbattle

**多游戏 Bot 线上对战平台**：用户上传自行编写的 Linux x86_64 ELF Bot，平台在 Docker 安全沙箱中自动运行对局，提供实时观赛、对局回放、Glicko-2 排行榜、组织者赛事、人类亲自上场、社交互动。支持 **德州扑克、五子棋、点格棋** 三款游戏。

## 能力一览

**对战核心**
- 唯一上传格式为 **Linux x86_64 ELF**；拒绝 PE/`.exe`、Mach-O、ARM64 ELF 和原始 `.py`，Docker 硬隔离执行
- 三种清晰的执行环境：日常节能沙箱（每 Bot 1 核 / 512 MiB）、正式赛事沙箱（每 Bot 2 核 / 2 GiB，仅锦标赛）和用户电脑主动连接的本地 Bot（平台只裁判，练习局不计平台排行榜）
- 唯一 JSON 信封：Traditional / LongRunning 只区分进程生命周期；响应必须包含 `response`，可选 `debug` 走独立私有 sidecar，其余顶层字段忽略；LongRunning 必须握手且不回退
- 各游戏独立裁判引擎 + 统一 GameSpec/结果契约；赛制与编排主流程无需游戏名分支
- SSE 实时观赛 + 完整对局回放（播放/暂停/步进/倍速/逐手跳转）+ 三游戏终态单场公开 canonical JSON v1 对局日志导出；五子棋另提供专项棋谱 JSON v1
- 全站对局身份统一：逐座位显示 Bot 与所属用户或实际真人，并明确标注自动排位、用户挑战、自博弈、真人对战、锦标赛等性质；Bot 可停用或从所有者管理列表删除，但不硬删已参赛实体的历史身份
- 终局私有 Bot debug sidecar：双方作者对称调试，赛事延迟授权，不进入公开回放、对局日志或五子棋棋谱
- Bot-vs-Bot 挑战可把自己的 Bot 放在任一游戏位置；版本、运行环境与本地连接会随 Bot 一起换位，真人对战仍固定由第二方亲自上场
- 人类 vs Bot（WebSocket 实时交互；与其他来源共享全局容量，占 1 个对局槽 + 1 个沙箱单元，不计平台排行榜）
- 人工、人机、赛事与自动排位统一进入持久执行队列；全局代码硬顶为 6 个对局槽 / 12 个沙箱单元，实际并发由每个 job 入队时冻结的 CPU、内存、沙箱向量与主机预算逐维准入，按组合动态为 1–6 场；显式参数只能收紧。同一非真人 Bot 全局最多参与一个进行中 job，赛事只在不跨 manual/human 排序边界的连续队列段内按持久 claim 历史轮转。自动排位仍只在前台清空并连续空闲 5 分钟后使用至多 1 个槽；挑战/人机返回 HTTP 202 请求，可刷新恢复、取消，基础设施中断后可安全重试

**赛事与排行**
- Glicko-2 数值排行榜（按游戏分别排名）；每个账号在每款游戏只派遣一个排行榜 Bot，其他 Bot 仍可练习和参赛，版本升级不更换评分身份
- 组织者赛事：6 种赛制阶段（单/双循环、分组、瑞士、单败淘汰）+ **19 个注册模板（18 个可新建）**、积分榜、对阵图、休息期换 Bot；进行中赛事提供自动更新的公开转播台。全员与分组单/双循环均不设参赛人数硬上限，完整 O(n²) 排期进入持久队列并继续受全站资源硬顶约束；`allow_large_round_robin` 仅是历史兼容字段。模板人数范围、用途和时长等级只用于推荐，组织者始终可以自由选择；页面同时给出基础对局、基础计分场、基础 ETA 与不封顶决胜风险。Gomoku Swiss 在发布时按报名人数冻结为 13–15 人 7 轮、16–20 人 9 轮、21 人以上 11 轮。新 Holdem/Gomoku 淘汰局打平后追加一组两场换座局，按原阶段积分比较，仍平继续下一组且不设上限；Holdem 同组同牌，Gomoku 只换先后角色、不承诺相同开局，历史无决胜 marker 的单败仍保持阻断。德州每个 70 手计分场独立按 3/1/0 计分；草稿/报名阶段可调整的设置在发布排期后随阶段快照冻结。正式名次提供永不含实名的公开成绩 CSV，组织者另可导出按报名/用户/Bot 稳定 ID 关联的报名名单；实名赛事仅允许选手本人报名并使用报名时资料快照，管理员可精确选择具体用户及其同游戏可运行 Bot 进行受审计纠错代报名，“全员指派”仅为需确认的次要操作
- 客户演示：六个明确标注“合成演示”的只读生命周期快照，保留真实裁判回放、逐阶段排名/晋级与独立正式总榜
- 闲时公平自动排位：只从每个账号、每款游戏当前派遣的唯一排行榜 Bot 中选手，按游戏/bootstrap 通道/所有者轮转；启用不等于立即运行，用户挑战、人机和真实赛事始终优先，系统连续空闲 5 分钟后至多生成 1 个候选并运行 1 场，结束后重新冷却 5 分钟；新前台任务或真实赛事到达时，自动局安全让路并在精确清理后释放容量

**平台功能**
- 账号体系：注册/登录/邮箱验证/重置密码、个人主页、资料/头像编辑
- 社交：关注用户、收藏 Bot、评论、点赞、浏览计数、点赞榜
- 通信：站内消息为真相、邮件异步投递；保留旧通知铃铛，并支持用户/admin 线程、固定受众广播与可追踪 Bug 反馈
- 经验与等级系统（等级 gating 部分功能）、全局搜索（Cmd+K 命令面板）
- 站点可配置（站名/Logo/公告）
- 管理后台提供紧凑通信中心：收发信、广播快照预览/二次批准、失败重试与 Bug 诊断/回复；事务邮件模板随代码版本发布，历史数据库自定义只读保留

**前端**
- React 19 + shadcn/ui 设计系统，**浅/暗双主题**（OKLCH token，一键切换）
- 响应式（桌面/平板/手机）、代码分割、无障碍（WCAG AA）

## 支持的游戏

| 游戏 | game_id | 规则摘要 |
|------|---------|---------|
| 德州扑克（HU NLHE） | `holdem` | 每个计分场固定 70 手 / 固定盲注 50-100、每手起始 20000 筹码 / raise delta 语义 |
| 五子棋 | `gomoku` | 15×15 / 26 种指定开局 / 三手交换 / 五手二打 / 黑方禁手 / 每方 15 分钟 |
| 点格棋（Dots and Boxes） | `pencil` | 固定 N=6 点交错网格 / 成格连走计分 / 每方累计 15 分钟棋钟（Bot 与人类对局同契约） |

> 平台在赛制/编排契约层按 `game_id` 解耦。新增游戏仍需注册 GameSpec、前端视图与元数据；数据库会按注册 ID 自动建立同构 `matches_<game>` 表。

## 快速开始

```bash
# 后端（Python ≥ 3.12）
source .venv/bin/activate
pip install -e '.[dev]'

# 前端
(cd bzplat/frontend && npm install && npm run build)

# 配置环境（SMTP_* 未配时邮件仍会排队并重试；注册/重置业务请求不阻塞）
cp .env.example .env

# 本地无 Docker 时用本机跑 ELF（仅测试；必须在启动服务前设置）
export BZ_BOT_LOCAL=1

# 起服务（默认 127.0.0.1:50380）
scripts/platform-ctl.sh start

# 建管理员（跳过邮箱验证）
botzone create-admin alice alice@example.com 'password123'

```

浏览器打开 <http://127.0.0.1:50380/>。样例构建、隔离冒烟和真浏览器回归命令统一见 [`doc/DEVELOPMENT.md`](doc/DEVELOPMENT.md)。

> `platform-ctl.sh` 会优先管理工作目录匹配的已安装 user-systemd unit，否则使用 PID 文件模式；两种模式都检查健康状态并拒绝在已有监听端口上启动第二个进程。运行时代码发布须完整执行 [`AGENTS.md` §1.8](AGENTS.md#18-合并后发布) 与[开发文档的维护排空流程](doc/DEVELOPMENT.md#计划部署先排空再停服)：ready 后还要停服、冷备、迁移预演、精确推进已审 SHA 和依赖核对，不能直接运行 `rebuild.sh`。只有冻结并审阅完整 fast-forward 区间、确认其均为纯文档/规则后才无需 restart；区间夹带运行时变更就必须在推进工作树前转完整发布流程。
> 需要 `192.168.1.0/24` 直连时，先按[安全文档](doc/SECURITY.md#受控-lan-直连)限制主机防火墙，再显式开启 LAN bind；直连客户端网段不能加入 trusted-proxy CIDR。

## 文档

| 文档集 | 受众 | 入口 |
|--------|------|------|
| **交付文档**（需求/设计/开发/测试/总结） | 甲方、干系人、平台开发者 | [`doc/`](doc/) |
| **规则与协议**（游戏规则、对局协议、Bot 开发指南） | Bot 玩家、访客 | [`wiki/`](wiki/) |

快速入口：[对局协议规范](wiki/PROTOCOL.md) · [Bot 开发指南](wiki/BOT_DEV.md) · [本地 Bot 接入](wiki/LOCAL_AI.md) · [项目总览](doc/OVERVIEW.md)

## 技术栈

- **后端**：Python ≥ 3.12、FastAPI、uvicorn、SQLite、异步 SMTP delivery worker（captcha + Pillow）
- **前端**：React 19 + Vite 8 + Tailwind CSS v4（CSS-first）+ shadcn/ui + Radix UI + lucide-react + recharts
- **暗色模式**：next-themes + OKLCH 双主题 token（浅色默认 + 暗色对等）
- **运行时**：Docker 节能/赛事资源档位（Linux x86_64 ELF 使用 debian:bookworm-slim）+ 用户端主动建立的 TLS WebSocket 本地 Bot；平台 Docker 只连接 canonical 本机 socket
- **评分**：Glicko-2（自实现，无外部依赖）

## 目录结构

```
├── bzplat/
│   ├── backend/          # FastAPI：games(注册表) / matches / contests / store / runtime /
│   │                     # auth / bots / communications / notifications(兼容投影) / rating / mail
│   └── frontend/         # React 19 + Vite 8 + Tailwind v4 + shadcn/ui（src/games 注册表 + canvas）
├── doc/                  # 工程交付文档（6 份核心文档 + 现行专项文档 + INDEX）
├── wiki/                 # Bot 玩家文档（规则/协议/Bot 开发指南）
├── contracts/            # 协议 JSON Schema
├── samples/              # 与现行协议逐字绑定的三游戏 C / Python 样例 Bot
├── scripts/              # 启停、重建、冒烟、压测、浏览器验收、种子、本地 Bot 客户端
└── deploy/               # systemd unit
```

> Python 包名为 `bzplat`（避免遮蔽标准库 `platform`）。

## License

MIT
