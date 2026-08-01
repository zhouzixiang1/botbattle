# Wiki 首页

本站文档从 [Botzone Wiki](https://wiki.botzone.org.cn/) 相关页面迁移并适配本平台（长驻 stdin/stdout 行协议）。

## 文档目录

| 页面 | 说明 |
|------|------|
| [协议规范](#/wiki?slug=protocol) | 德州紧凑 JSON 协议 |
| [Bot 开发指南](#/wiki?slug=bot-dev) | 上传、调试、样例 |
| [运行时与资源限制](#/wiki?slug=runtime) | Docker / 超时 / 并发 |
| [五子棋 Gomoku](#/wiki?slug=gomoku) | 规则 + 协议 + 样例 |
| [一手交换五子棋](#/wiki?slug=gomoku-swap1) | Swap1 简介 |
| [点格棋 Pencil](#/wiki?slug=pencil) | 规则 + 交错网格 + pass |
| [德州扑克](#/wiki?slug=texas) | Botzone TexasHoldem2p 对照 |
| [裁判](#/wiki?slug=judge) | 裁判概念 |
| [对局](#/wiki?slug=match) | 对局生命周期与错误码 |
| [赛制模板](#/wiki?slug=contest-format) | 模板结构、阶段类型、match_config 与管理员配置 |
| [赛事对阵图](#/wiki?slug=contest-bracket) | 对阵/报名显示 Bot 名 + bracket 数据端点 |
| [Bot 详情页](#/wiki?slug=bot-detail) | Bot 档案/对局历史/对手战绩/评分曲线 |
| [用户主页与搜索](#/wiki?slug=user-profile) | 用户档案/战绩/Bot 列表 + 全局搜索 + 资料编辑 |
| [通知系统](#/wiki?slug=notifications) | 站内通知 + 邮件提醒（铃铛/列表/偏好） |
| [社交](#/wiki?slug=social) | 关注用户 + 收藏 Bot |
| [段位称号](#/wiki?slug=tier) | Rating→段位映射 + 排名变化趋势 |
| [压测](#/wiki?slug=loadtest) | 大规模系统压测脚本（批量用户 + 全端点覆盖） |

## 本平台 vs Botzone（摘要）

| 项 | Botzone | 本平台 |
|----|---------|--------|
| 进程模型 | 默认每回合启停；可选长时运行 | **整场对局长驻**，一行一条 JSON |
| CPU / 内存 | 1 核 / 默认 256MB | Docker `--cpus=1` / `--memory=512m` |
| 决策时限 | 默认 1s/回合（首回合×2） | 管理员可配（默认 60s） |
| 游戏 | 站内多游戏 | `holdem` / `gomoku` / `pencil` |
| 德州 response | 整型；raise=增量 | `{"a","x"}`；raise=`x` 为 **raise-to-total** |
| 棋类协议 | 聚合 `requests`/`responses` | 每步推送对方上一手，语义对齐 |

上传 Bot 时请选择正确的 **游戏类型**；挑战与排行榜按 `game_id` 过滤。

## 参考资源

- **参考裁判**（`samples/judges/`）：可在本地自测合法着 / 胜负 / 手牌评估的独立脚本，与服务端引擎逻辑一致。见 [裁判](#/wiki?slug=judge)。
- **闲时自动对局**：系统空闲时自动安排 bot 对战维护天梯（`match_type=ladder`）。见 [运行时](#/wiki?slug=runtime)。
- Bot 开发入门见 [Bot 开发指南](#/wiki?slug=bot-dev)。
