# botbattle

多游戏 Bot 线上对战平台：用户上传二进制 Bot，平台在沙箱中运行对局，提供实时观赛、回放、排行榜与组织者比赛。支持 **五子棋、点格棋（Dots and Boxes）、德州扑克**。

## 能力

- 用户注册 / 登录 / 邮箱验证 / 重置密码（图形验证码 + SMTP）
- 上传 **Linux ELF / Windows PE** 二进制 Bot（macOS Mach-O 拒绝），Docker / Wine 或本地沙箱执行
- 多游戏对战：每款游戏独立的规则引擎与紧凑 JSON 行协议
- SSE 实时观赛、图形化扑克桌 / 棋盘、逐手回放（含进度控制）
- 全局 Glicko-2 排行榜
- 组织者比赛：报名、派遣 Bot、多阶段（瑞士 / 分组循环 / 单败淘汰）、休息期换 Bot、积分榜
- **闲时自动对局**：系统空闲时自动安排 bot 对战维护天梯榜（`match_type=ladder`，陈旧度优先 + rating 就近配对）
- 站内 Wiki（协议规范、Bot 开发指南）；**参考裁判**脚本可本地自测（`samples/judges/`）
- 管理端：用户 / Bot / 对局 / 比赛 / 邮件模板 / 发件箱 / 仪表盘 / 运行时热配置

## 支持的游戏

| 游戏 | 状态 | 说明 |
|------|------|------|
| 德州扑克（HU NLHE） | ✅ 已实现 | 70 手 / 盲注 50-100 / 起始 20000，紧凑 JSON 行协议 |
| 五子棋 | ✅ 已实现 | 15×15，五连胜（含长连），无禁手 |
| 点格棋（Dots and Boxes） | ✅ 已实现 | N=11 点交错网格，成格连走计分 |

> 平台架构按游戏解耦：每款游戏在 `engine/` 下独立模块，对战通过统一的 `game_id` 路由，新增游戏只需实现规则引擎与协议适配。

## 快速开始

```bash
# 安装后端依赖
source .venv/bin/activate       # Python ≥ 3.12
pip install -e '.[dev]'

# 构建前端
(cd bzplat/frontend && npm install && npm run build)

# 配置环境（可选填写 SMTP_* 启用邮件）
cp .env.example .env

# 本地无 Docker 时用本机跑同架构 ELF：
export BZ_BOT_LOCAL=1
scripts/platform-ctl.sh start
# 浏览器打开 http://127.0.0.1:50380/
```

创建管理员（跳过邮箱验证）：

```bash
botzone create-admin alice alice@example.com 'password123'
```

编译样例 Bot：

```bash
samples/build_sample.sh
```

端到端冒烟：

```bash
bash scripts/e2e_smoke.sh
```

## 文档

- [`wiki/PROTOCOL.md`](wiki/PROTOCOL.md) — 紧凑 JSON 对局协议规范（字段、卡牌编码、规则）
- [`wiki/BOT_DEV.md`](wiki/BOT_DEV.md) — Bot 开发指南（样例、编译、上传、调试）

## 技术栈

- **后端**：Python ≥ 3.12、FastAPI、uvicorn、SQLite、SMTP、captcha + Pillow
- **前端**：React 19 + Vite + Tailwind CSS v4（浅色主题）
- **运行时**：Docker（必需）、Wine 镜像（Windows Bot）
- **评分**：Glicko-2（自实现，无外部依赖）

## 目录结构

```
├── bzplat/
│   ├── backend/          # FastAPI 后端：engine / protocol / store / auth / bots / runtime / matches / contests / rating
│   └── frontend/         # React + Vite + Tailwind 前端
├── wiki/                 # 协议规范与 Bot 开发指南
├── contracts/            # 协议 JSON Schema
├── samples/              # 样例 Bot（C / Python 源码 + 编译脚本）
├── scripts/              # 启停、e2e 冒烟、API 测试
└── deploy/               # systemd unit
```

> Python 包名为 `bzplat`（避免遮蔽标准库 `platform`）。

## License

MIT
