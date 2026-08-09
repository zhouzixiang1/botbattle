# Wiki 首页

这里是 Bot 作者和平台用户的对外文档。通信协议与游戏规则只以本站当前实现为准，
不提供旧协议或外部规则的兼容用法。

## 文档目录

| 页面 | 说明 |
|------|------|
| [通信协议](#/wiki?slug=protocol) | 唯一 JSON 信封、Traditional / LongRunning、严格响应与故障语义 |
| [Bot 开发指南](#/wiki?slug=bot-dev) | 编译、上传、预检、调试和三游戏样例 |
| [德州扑克](#/wiki?slug=texas) | 固定 70 手、动作码、11 字段请求与牌编码 |
| [五子棋](#/wiki?slug=gomoku) | 固定 15×15、黑先、无禁手、连五胜 |
| [点格棋](#/wiki?slug=pencil) | 固定 N=6、成格连走、每方 900 秒累计棋钟 |
| [平台功能指南](#/wiki?slug=guide) | 对局、裁判、排行、赛事、社交、通知与设置 |

## 规则速查

| 游戏 | game_id | 固定规则 |
|------|---------|----------|
| 德州扑克 | `holdem` | 70 手；每手起始 20000、盲注 50/100；按累计净筹码判胜 |
| 五子棋 | `gomoku` | 15×15；黑先；连续不少于五子即胜；无禁手 |
| 点格棋 | `pencil` | N=6；25 格；成格连走；每方累计 900 秒 |

三款游戏共用同一个外层协议：响应必须是只含 `response` 的 JSON 对象。上传时选择的
Traditional / LongRunning 只决定进程生命周期；LongRunning 握手缺失不会回退。

平台唯一接受 Linux x86_64 ELF。Windows 与 macOS 玩家也应按
[Bot 开发指南](#/wiki?slug=bot-dev)在 Linux amd64 容器中构建，不要直接上传 `.exe`、
Mach-O 或 `.py`。
