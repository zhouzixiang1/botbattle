# Wiki 首页

这里是 Bot 玩家、访客和赛事组织者的使用文档。本站只有一套通信协议和一套固定游戏规则。

## 第一次上传 Bot

1. 从[通信协议](#/wiki?slug=protocol)了解请求信封与响应格式。
2. 阅读所选游戏的固定规则和 payload：
   [德州扑克](#/wiki?slug=texas)、[五子棋](#/wiki?slug=gomoku)或
   [点格棋](#/wiki?slug=pencil)。
3. 从对应页面复制完整 C / Python 示例，再按
   [Bot 开发指南](#/wiki?slug=bot-dev)构建为 Linux amd64 可执行文件。
4. 上传前确认文件类型包含 `ELF 64-bit` 和 `x86-64`，再选择与程序一致的运行模式。

平台只接受 **Linux x86_64 ELF**。Windows、Linux 和 macOS 都可以使用开发指南中的
Docker 命令构建；`.exe`、Mach-O、ARM64 ELF 和 `.py` 源文件不能上传运行。

## 协议速查

- Bot 每次输出包含 `{"response":...}` 的对象；可选 `debug` 只作终局后有界私有调试，
  其他顶层字段忽略，动作始终只由 `response` 决定。
- Traditional 每回合收到完整 `{"requests":[...],"responses":[...]}` 信封。
- LongRunning 首回合同样收到完整信封，首个响应后必须输出精确握手；握手失败不会降级。
- stdin 与 stdout 均为一行一条消息，Bot 输出后必须立即 flush。

## 固定规则速查

| 游戏 | `game_id` | 固定规则 |
|------|-----------|----------|
| 德州扑克 | `holdem` | 70 手；每手起始筹码 20000；小盲 50、大盲 100；累计净筹码高者胜 |
| 五子棋 | `gomoku` | 15×15；黑先；无禁手；连续不少于五子即胜 |
| 点格棋 | `pencil` | N=6；25 格；成格连走；每方 900 秒累计棋钟 |

## 文档目录

| 页面 | 内容 |
|------|------|
| [通信协议](#/wiki?slug=protocol) | 信封、运行模式、三个游戏 payload 与故障处理 |
| [Bot 开发指南](#/wiki?slug=bot-dev) | 完整最小示例、跨平台构建、验证、预检与排错 |
| [德州扑克](#/wiki?slug=texas) | 固定规则、动作码、请求字段与牌编码 |
| [五子棋](#/wiki?slug=gomoku) | 固定规则、坐标、通信要点与完整示例 |
| [点格棋](#/wiki?slug=pencil) | 固定规则、连走、累计棋钟、通信要点与完整示例 |
| [平台功能指南](#/wiki?slug=guide) | 持久排队/取消/重试、对局、排行、赛事、社交、通知与设置 |
