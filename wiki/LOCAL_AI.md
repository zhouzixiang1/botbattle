# 本地 Bot 接入

本地 Bot 适合日常调试：程序留在自己的电脑上，电脑主动建立加密连接，平台只发送局面、接收动作并执行裁判。无需上传这次运行的程序，也无需开放路由器端口。

本地 Bot 对局是**练习对局，不计平台排行榜，也不能参加锦标赛**。需要稳定成绩时上传 Linux x86_64 ELF；正式赛事统一使用赛事沙箱。

## 先分清两件事

| 选择 | 决定什么 | 可选项 |
|------|----------|--------|
| 运行环境 | 程序在哪里、使用多少平台资源 | 节能沙箱 / 赛事沙箱 / 本地 Bot |
| 交互模式 | 一局内进程怎样收发消息 | Traditional / LongRunning |

当前本地接入只支持 **Traditional**：每个决策启动一次本机命令，stdin 收到一行完整历史，stdout 首行作为响应。它仍使用平台唯一的[通信协议](#/wiki?slug=protocol)和相应游戏 payload，不是另一套游戏协议。

## 三种运行环境

| 环境 | 程序位置 | 单个 Bot 资源 | 用途 | 平台排行榜 |
|------|----------|---------------|------|------------|
| 节能沙箱 | 平台 Docker | 1 核、512 MiB | 日常挑战、自动排位、人机测试 | 符合计分条件时计入 |
| 赛事沙箱 | 平台 Docker | 2 核、2 GiB | 正式锦标赛，由平台自动使用 | 只计赛事成绩，不计平台排行榜 |
| 本地 Bot | 用户自己的电脑 | 不占平台沙箱 | 快速调试、两份本地程序互测、与已上传 Bot 比较 | 不计平台排行榜 |

全站仍只有一个对局执行队列。本地对局不占 Docker 沙箱，但仍占一个裁判对局槽，不会绕过其他用户排队。

## 接入步骤

1. 先在“我的 Bot”上传并启用一个 Bot，再创建本地接入。所选 Bot 只作为对局中显示的身份；本机程序可以运行尚未上传的新代码，平台不会下载或保存它。
2. 复制页面**仅显示一次**的令牌。不要把令牌放进 URL、命令行、源码、截图或仓库。
3. 下载页面提供的 `local_ai_client.py`，复制同一位置显示的 WSS 接入地址，并准备一个遵守 Traditional 协议的本机命令。
4. 启动客户端，页面显示“在线”后再去“挑战”页选择“本地 Bot（使用我的电脑）”。

Linux / macOS：

```bash
python -m pip install "websockets>=10.4"

# read 的输入不会写入 shell 历史；粘贴页面给出的令牌后回车
read -rsp "接入令牌: " BZ_LOCAL_AI_TOKEN && echo
export BZ_LOCAL_AI_TOKEN
read -rp "WSS 接入地址: " BOTBATTLE_LOCAL_AI_URL

python local_ai_client.py \
  --url "$BOTBATTLE_LOCAL_AI_URL" \
  --command ./my_bot
```

Windows PowerShell：

```powershell
python -m pip install "websockets>=10.4"
$env:BZ_LOCAL_AI_TOKEN = Read-Host "接入令牌"
$env:BOTBATTLE_LOCAL_AI_URL = Read-Host "WSS 接入地址"
python local_ai_client.py --url $env:BOTBATTLE_LOCAL_AI_URL --command python my_bot.py
```

`--command` 必须放在客户端参数最后；它后面的内容全部属于 Bot 命令。客户端不经 shell 拼接命令。令牌只从 `BZ_LOCAL_AI_TOKEN` 读取，并通过 `Authorization: Bearer ...` 发送；接入地址必须是没有查询参数、用户名或密码的 `wss://` URL，握手阶段的 30x 重定向会被拒绝，令牌不会被带到另一个地址。

客户端启动 Bot 时会移除子进程环境中的 `BZ_LOCAL_AI_TOKEN`，不会通过环境继承把连接令牌交给 Bot。

## 本机 Bot 收到什么

平台发给客户端的控制消息如下。`input_line` 是要原样写入 Bot stdin 的一行 Traditional 信封：

```json
{
  "type": "turn",
  "request_id": "req_...",
  "match_id": "20260813...",
  "turn": 3,
  "input_line": "{\"requests\":[...],\"responses\":[...]}",
  "timeout_ms": 8000
}
```

客户端每回合执行一次指定命令，向 stdin 写入 `input_line` 加换行，只读取 stdout 首行，然后回送：

```json
{
  "type": "response",
  "request_id": "req_...",
  "match_id": "20260813...",
  "turn": 3,
  "output": "{\"response\":0}"
}
```

`output` 仍须符合[通信协议](#/wiki?slug=protocol)：顶层对象必须包含 `response`；可选 `debug` 的规则不变。各游戏动作见[德州扑克](#/wiki?slug=texas)、[五子棋](#/wiki?slug=gomoku)和[点格棋](#/wiki?slug=pencil)。

如果本机命令没有启动、没有输出、输出过大或格式无效，客户端不会伪造动作，也不会让整场一直等到最长棋钟。它会回送与当前决策严格绑定的故障类别：

```json
{
  "type": "failure",
  "request_id": "req_...",
  "match_id": "20260813...",
  "turn": 3,
  "reason": "bot_start_failed"
}
```

`reason` 只可能是 `bot_start_failed`、`bot_no_response`、`bot_output_too_large`、`bot_output_invalid`、`bot_io_failed` 或 `bot_decision_timeout`。平台确认 `request_id + match_id + turn` 与当前待答回合完全一致后，立即按本地 Bot 技术故障结束该局并释放执行槽；错局、错回合和晚到消息都会拒绝。本机路径、命令参数、stderr 和原始异常不会上传。

客户端把单次输入限制为 1 MiB、stdout 首行限制为 64 KiB，并按平台给出的 `timeout_ms` 结束超时进程。stderr 不上传。连接若在 Bot 思考时中断，客户端会立即结束该回合的 Bot 进程并进入重连，不会空等到决策超时。断线后按 1、2、4、8、16、30 秒退避重连；平台只会在原截止时间尚未到达时重发同一决策，重连不会延长时间。

## 两个本地 Bot 对战

为两个自己的 Bot 分别创建接入并各启动一个客户端。发起挑战时，两个位置都选择“本地 Bot”，再选择各自在线的接入。也可以一边选本地 Bot、一边选已上传 Bot。

- 每个接入同一时间只处理一个决策，不能被多场对局复用。
- 离线接入不会取得新对局；正在等待的决策断线后仍按原截止时间处理。
- 本地 Bot 只能由创建它的用户选择，不能把自己的电脑借给其他账号远程执行。
- 每个账号最多保留 8 个有效接入、同时在线 4 个；全站最多 64 个在线接入。撤销不用的接入后，可以继续使用原来的显示名称创建新接入。
- 正在对局的接入不能更换令牌；须等本场结束、平台释放占用后再操作。被拒绝的更换不会断开当前连接。
- 账号或对应 Bot 被停用时，接入令牌与正在占用的租约会同时失效，已连接的客户端会被关闭。
- 本地程序在用户电脑上运行，不受平台 Docker 保护。请使用普通系统账号或自己的容器运行不可信代码。

## 令牌与故障处理

- **令牌泄露**：立即在“我的 Bot”中“更换令牌”；旧令牌随即失效。停止使用时撤销接入。
- **一直离线**：重新复制页面给出的 `wss://` 地址，确认系统时间正确、网络允许出站 HTTPS/WSS，并检查令牌没有首尾空格。
- **Bot 未响应或本局立即技术结束**：先看客户端终端中的本机诊断，再直接给程序输入一行完整信封，确认命令能启动、只向 stdout 输出一行 JSON 且立即 flush；这些本机诊断不会上传平台。
- **频繁超时**：本机进程启动时间也计入本回合时限。减少启动开销，或先用更简单的局面定位协议问题。
- **需要 LongRunning**：当前本地接入不支持；请上传 ELF 并选择 LongRunning，或先把程序改为 Traditional 调试。

## 三类用户各自关注什么

- **普通参赛者**：平时用本地 Bot 快速迭代，再用节能沙箱验证上传产物；赛前确认正式版本已上传、能通过预检并完成平台对局。
- **赛事组织者**：只选择赛制、报名和时间，不手动分配机器。开赛后平台统一用每 Bot 2 核、2 GiB 的赛事沙箱，避免参赛者电脑和网络影响正式成绩。
- **超级管理员**：关注本地接入在线数、执行队列与赛事主机余量；可撤销异常接入，但不能查看令牌或把本地练习局改成计分局。

> 本地接入使用 Botbattle 自己的 Bearer 令牌与 WSS 控制信封；其他平台的本地接入客户端和令牌不能混用。
