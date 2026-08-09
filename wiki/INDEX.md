# Wiki 首页

本站文档从 [Botzone Wiki](https://wiki.botzone.org.cn/) 相关页面迁移并适配本平台（长驻 stdin/stdout 行协议）。**核心是三款游戏**，其余功能集中在[平台功能指南](#/wiki?slug=guide)。

## 文档目录

| 页面 | 说明 |
|------|------|
| [协议规范](#/wiki?slug=protocol) | **Botzone 标准协议**（信封、两模式、德州裸整数 response） |
| [Bot 开发指南](#/wiki?slug=bot-dev) | 上传、调试、样例 |
| [德州扑克](#/wiki?slug=texas) | 对齐 Botzone TexasHoldem2p 全文结构 + 本平台行协议 |
| [五子棋 Gomoku](#/wiki?slug=gomoku) | 规则 + 协议 + 样例 + 一手交换变体 |
| [点格棋 Pencil](#/wiki?slug=pencil) | 规则 + 交错网格 + pass + 每方 15 分钟累计棋钟 |
| [平台功能指南](#/wiki?slug=guide) | 对局 / 裁判 / 段位 / 等级 / 锦标赛 / Bot详情 / 用户主页 / 社交 / 通知 / 设置——一页看全 |

> 前端设计系统与组件库、开发/测试/架构等工程文档在 [`doc/`](../doc/INDEX.md)（面向开发者）。

## 本平台 vs Botzone（摘要）

| 项 | Botzone | 本平台 |
|----|---------|--------|
| 进程模型 | 默认每回合启停；可选长时运行 | **整场对局长驻**（不每回合重启），Botzone 信封一行一条 JSON |
| CPU / 内存 | 1 核 / 默认 256MB | Docker `--cpus=1` / `--memory=512m` |
| 决策时限 | 默认 1s/回合（首回合×2） | holdem / gomoku 默认 60s/决策（可配）；Pencil 固定 900s/方累计（含人类局） |
| 游戏 | 站内多游戏 | `holdem` / `gomoku` / `pencil` |
| 德州协议 | 信封 + 裸整数 response + raise=增量 | **完全照 Botzone**（信封 + 裸整数 `-1/-2/0/>0` + raise=额外量；固定 70 手） |
| 棋类协议 | 聚合 `requests`/`responses` | 每步推送对方上一手，Botzone 信封 + `{x,y}` 落子（完全照 Botzone） |

上传 Bot 时请选择正确的 **游戏类型**；挑战与排行榜按 `game_id` 过滤。

## 参考资源

- **参考裁判**（`samples/judges/`）：可在本地自测合法着 / 胜负 / 手牌评估的独立脚本，与服务端引擎逻辑一致。见[功能指南 · 裁判](#/wiki?slug=guide)。
- **闲时自动对局**：系统空闲时自动安排 bot 对战维护天梯（`match_type=ladder`）。
- Bot 开发入门见 [Bot 开发指南](#/wiki?slug=bot-dev)。
