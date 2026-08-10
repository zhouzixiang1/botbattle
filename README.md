# botbattle

**多游戏 Bot 线上对战平台**：用户上传自行编写的 Linux x86_64 ELF Bot，平台在 Docker 安全沙箱中自动运行对局，提供实时观赛、对局回放、Glicko-2 排行榜、组织者赛事、人类亲自上场、社交互动。支持 **德州扑克、五子棋、点格棋** 三款游戏。

## 能力一览

**对战核心**
- 唯一上传格式为 **Linux x86_64 ELF**；拒绝 PE/`.exe`、Mach-O、ARM64 ELF 和原始 `.py`，Docker 硬隔离执行
- 唯一 JSON 信封：Traditional / LongRunning 只区分进程生命周期；响应必须包含 `response`，其他顶层字段忽略；LongRunning 必须握手且不回退
- 各游戏独立裁判引擎 + 统一 GameSpec/结果契约；赛制与编排主流程无需游戏名分支
- SSE 实时观赛 + 完整对局回放（播放/暂停/步进/倍速/逐手跳转）
- 人类 vs Bot（WebSocket 实时交互，独立并发，不计评分）

**赛事与排行**
- Glicko-2 排行榜（按游戏分别排名）+ 6 档段位称号 + 相邻评分变化/RD/胜率与最近对局
- 组织者赛事：6 种赛制阶段（单/双循环、分组、瑞士、单败淘汰）+ 内置预赛/决赛等模板、积分榜、对阵图、休息期换 Bot
- 闲时自动对局维护天梯（陈旧度 + 定级赛优先）

**平台功能**
- 账号体系：注册/登录/邮箱验证/重置密码、个人主页、资料/头像编辑
- 社交：关注用户、收藏 Bot、评论、点赞、浏览计数、点赞榜
- 通知：站内通知 + 可选邮件提醒（对局完成/被关注/赛事/评论）
- 经验与等级系统（等级 gating 部分功能）、全局搜索（Cmd+K 命令面板）
- 站点可配置（站名/Logo/公告）
- 管理后台（7 Tab：仪表盘/用户/Bot/对局记录/锦标赛/日志/邮件）；运行参数与赛制模板随代码评审发布，不提供网页写入口

**前端**
- React 19 + shadcn/ui 设计系统，**浅/暗双主题**（OKLCH token，一键切换）
- 响应式（桌面/平板/手机）、代码分割、无障碍（WCAG AA）

## 支持的游戏

| 游戏 | game_id | 规则摘要 |
|------|---------|---------|
| 德州扑克（HU NLHE） | `holdem` | 固定 70 手 / 固定盲注 50-100、每手起始 20000 筹码 / raise delta 语义 |
| 五子棋 | `gomoku` | 15×15 / 黑先 / 五连即胜（含长连）/ 无禁手 |
| 点格棋（Dots and Boxes） | `pencil` | 固定 N=6 点交错网格 / 成格连走计分 / 每方累计 15 分钟棋钟（Bot 与人类对局同契约） |

> 平台在赛制/编排契约层按 `game_id` 解耦。新增游戏仍需注册 GameSpec、前端视图与元数据；数据库会按注册 ID 自动建立同构 `matches_<game>` 表。

## 快速开始

```bash
# 后端（Python ≥ 3.12）
source .venv/bin/activate
pip install -e '.[dev]'

# 前端
(cd bzplat/frontend && npm install && npm run build)

# 配置环境（SMTP_* 未配时现有账号仍可使用，但注册/重置会返回 503）
cp .env.example .env

# 本地无 Docker 时用本机跑 ELF（仅测试；必须在启动服务前设置）
export BZ_BOT_LOCAL=1

# 起服务（默认 127.0.0.1:50380）
scripts/platform-ctl.sh start

# 建管理员（跳过邮箱验证）
botzone create-admin alice alice@example.com 'password123'

```

浏览器打开 <http://127.0.0.1:50380/>。样例构建、隔离冒烟和真浏览器回归命令统一见 [`doc/DEVELOPMENT.md`](doc/DEVELOPMENT.md)。

> 改完代码必须 `bash scripts/rebuild.sh`（build + restart）才生效。

## 文档

| 文档集 | 受众 | 入口 |
|--------|------|------|
| **交付文档**（需求/设计/开发/测试/总结） | 甲方、干系人、平台开发者 | [`doc/`](doc/) |
| **规则与协议**（游戏规则、对局协议、Bot 开发指南） | Bot 玩家、访客 | [`wiki/`](wiki/) |

快速入口：[对局协议规范](wiki/PROTOCOL.md) · [Bot 开发指南](wiki/BOT_DEV.md) · [项目总览](doc/OVERVIEW.md)

## 技术栈

- **后端**：Python ≥ 3.12、FastAPI、uvicorn、SQLite、SMTP（captcha + Pillow）
- **前端**：React 19 + Vite 8 + Tailwind CSS v4（CSS-first）+ shadcn/ui + Radix UI + lucide-react + recharts
- **暗色模式**：next-themes + OKLCH 双主题 token（浅色默认 + 暗色对等）
- **运行时**：Docker（必需；Linux x86_64 ELF 使用 debian:bookworm-slim）
- **评分**：Glicko-2（自实现，无外部依赖）

## 目录结构

```
├── bzplat/
│   ├── backend/          # FastAPI：games(注册表) / matches / contests / store / runtime /
│   │                     # auth / bots / notifications / rating / mail
│   └── frontend/         # React 19 + Vite 8 + Tailwind v4 + shadcn/ui（src/games 注册表 + canvas）
├── doc/                  # 工程交付文档（6 份核心文档 + 现行专项文档 + INDEX）
├── wiki/                 # Bot 玩家文档（规则/协议/Bot 开发指南）
├── contracts/            # 协议 JSON Schema
├── samples/              # 与现行协议逐字绑定的三游戏 C / Python 样例 Bot
├── scripts/              # 启停、重建、冒烟、压测、浏览器验收、种子
└── deploy/               # systemd unit
```

> Python 包名为 `bzplat`（避免遮蔽标准库 `platform`）。

## License

MIT
