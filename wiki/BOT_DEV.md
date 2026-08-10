# Bot 开发指南

本页面向准备上传 Bot 的玩家。平台唯一接受的上传产物是 **Linux x86_64 ELF**：

- 必须是 64 位、`x86-64` / `amd64` 架构的 Linux ELF 可执行文件；
- 不接受 Windows PE / `.exe`、macOS Mach-O、ARM64 / `aarch64` ELF；
- 不接受 `.py` 源文件、Shell 脚本、压缩包或源码目录；
- 文件叫什么名字并不重要，平台按文件内容校验格式与架构。

因此，即使你在 Windows 或 macOS 上开发，最终也必须在 **Linux amd64 环境**中构建。
最稳妥的方式是使用 Docker，并在命令中固定 `--platform linux/amd64`。

开始前请先阅读[通信协议](#/wiki?slug=protocol)和对应游戏规则。先复制一份完整示例跑通，
再替换其中的决策函数，通常是最快的上手方式。

## 1. 选择运行模式

- **Traditional（默认）**：每个决策点重启进程；每次收到
  `{"requests":[...],"responses":[...]}` 完整历史。
- **LongRunning**：首回合收到同一完整历史；输出首个响应后必须立即输出精确握手
  `>>>BOTZONE_REQUEST_KEEP_RUNNING<<<`；后续收到 `{"request":...}`，进程不得退出。

两种模式共用同一游戏 payload 和 `{"response":...}` 响应信封。LongRunning 未完成精确
握手会直接协议判负，不会回退成 Traditional。

Traditional Bot 可以只读取一行完整信封、输出一行响应后退出；它也可以像下方示例一样
保持读取循环，由平台在取得该回合响应后结束进程。LongRunning Bot 必须保持进程运行并
持续读取增量信封。

## 2. 完整可复制的 C 最小 Bot

下面是一个可用于 Holdem 的完整 `bot.c`。策略永远 call/check。它在首个 JSON 响应后
输出 LongRunning 握手，因此同一个 ELF 可选择 Traditional 或 LongRunning；Traditional
只读取本次 JSON 响应后便结束进程。

<!-- SAMPLE:holdem:c -->
```c
#include <stdio.h>
#include <stdlib.h>

#define KEEP_RUNNING ">>>BOTZONE_REQUEST_KEEP_RUNNING<<<"
#define MAX_LINE (4 * 1024 * 1024)

int main(void) {
    char *line = malloc(MAX_LINE);
    int first_response = 1;
    if (line == NULL) return 1;

    while (fgets(line, MAX_LINE, stdin) != NULL) {
        /* Holdem: response=0 表示 call/check。 */
        fputs("{\"response\":0}\n", stdout);

        if (first_response) {
            fputs(KEEP_RUNNING "\n", stdout);
            first_response = 0;
        }
        fflush(stdout);
    }

    free(line);
    return 0;
}
```

把代码完整保存为当前目录下的 `bot.c`。不要向 stdout 打印独立日志行；想让 Bot 作者在终局后
查看策略诊断，应把有界信息放进同一 JSON 的顶层 `debug`。stderr 只用于平台运维排查崩溃，
不会作为作者调试面板的数据源。

## 3. 完整可复制的 Python 最小 Bot

下面是等价的完整 `bot.py`。源文件不能直接上传，必须按后文使用 Linux amd64 容器中的
PyInstaller 打包成 ELF。

<!-- SAMPLE:holdem:python -->
```python
#!/usr/bin/env python3
import json
import sys

KEEP_RUNNING = ">>>BOTZONE_REQUEST_KEEP_RUNNING<<<"


def current_request(envelope):
    if "request" in envelope:
        request = envelope["request"]
    else:
        requests = envelope.get("requests")
        if not isinstance(requests, list) or not requests:
            raise ValueError("missing requests")
        request = requests[-1]
    if not isinstance(request, dict):
        raise ValueError("request payload must be an object")
    return request


def main():
    first_response = True
    for line in sys.stdin:
        if not line.strip():
            continue

        envelope = json.loads(line)
        current_request(envelope)

        # Holdem: response=0 表示 call/check。
        print(json.dumps({"response": 0}, separators=(",", ":")), flush=True)

        if first_response:
            print(KEEP_RUNNING, flush=True)
            first_response = False


if __name__ == "__main__":
    main()
```

把代码完整保存为当前目录下的 `bot.py`。PyInstaller 会把解释器和依赖一起打进单文件
ELF；这不代表平台会执行原始 `.py` 文件。

## 4. Linux：构建 C 与 Python

先安装 Docker Engine，并确认 `docker version` 正常。以下命令在 Bash 中执行，当前目录
应包含上面的 `bot.c` 和 `bot.py`。即使开发机是 ARM Linux，也要保留
`--platform linux/amd64`。

### C：Alpine 静态编译

```bash
docker run --rm --platform linux/amd64 \
  --mount "type=bind,source=$PWD,target=/work" \
  -w /work alpine:3.20 sh -lc '
    apk add --no-cache build-base file &&
    cc -O2 -pipe -static -s -o /work/bot_c_linux_amd64 /work/bot.c &&
    chmod a+rx /work/bot_c_linux_amd64 &&
    file /work/bot_c_linux_amd64
  '
```

### Python：Linux PyInstaller 打包

```bash
docker run --rm --platform linux/amd64 \
  --mount "type=bind,source=$PWD,target=/work" \
  -w /work python:3.12-bookworm bash -lc '
    python -m pip install --no-cache-dir pyinstaller &&
    pyinstaller --noconfirm --clean --onefile \
      --name bot_py_linux_amd64 \
      --distpath /work \
      --workpath /tmp/pyinstaller \
      --specpath /tmp \
      /work/bot.py &&
    chmod a+rx /work/bot_py_linux_amd64
  '
```

生成的 `bot_c_linux_amd64` 或 `bot_py_linux_amd64` 才是上传文件。

## 5. Windows：构建 C 与 Python

安装 Docker Desktop，启用 WSL 2 后端，并确保使用 Linux containers。打开 PowerShell，
进入保存 `bot.c` / `bot.py` 的目录。Windows 上的编译器和本机 PyInstaller 会生成 PE，
不能作为平台上传文件；必须通过下列 Linux amd64 容器构建。

### C：Alpine 静态编译

```powershell
docker run --rm --platform linux/amd64 `
  --mount "type=bind,source=$($PWD.Path),target=/work" `
  -w /work alpine:3.20 sh -lc '
    apk add --no-cache build-base file &&
    cc -O2 -pipe -static -s -o /work/bot_c_linux_amd64 /work/bot.c &&
    chmod a+rx /work/bot_c_linux_amd64 &&
    file /work/bot_c_linux_amd64
  '
```

### Python：Linux PyInstaller 打包

```powershell
docker run --rm --platform linux/amd64 `
  --mount "type=bind,source=$($PWD.Path),target=/work" `
  -w /work python:3.12-bookworm bash -lc '
    python -m pip install --no-cache-dir pyinstaller &&
    pyinstaller --noconfirm --clean --onefile \
      --name bot_py_linux_amd64 \
      --distpath /work \
      --workpath /tmp/pyinstaller \
      --specpath /tmp \
      /work/bot.py &&
    chmod a+rx /work/bot_py_linux_amd64
  '
```

如果你已在 WSL 的 Linux 终端中工作，也可以直接执行上一节的 Linux 命令。关键不是
终端名称，而是构建环境必须为 Linux x86_64。Windows ARM 设备仍应使用 Docker 的
`--platform linux/amd64`，不要上传 WSL 本机生成的 `aarch64` 文件。

## 6. macOS：构建 C 与 Python

安装 Docker Desktop，打开 Terminal，进入保存源码的目录。macOS 本机 `clang` 生成
Mach-O，本机 PyInstaller 也只生成 Mach-O；PyInstaller 不支持从 macOS 原生跨系统打包
Linux ELF。Intel Mac 和 Apple Silicon 都使用下面的 Linux amd64 容器命令，Apple
Silicon 尤其不能删除 `--platform linux/amd64`。

### C：Alpine 静态编译

```bash
docker run --rm --platform linux/amd64 \
  --mount "type=bind,source=$PWD,target=/work" \
  -w /work alpine:3.20 sh -lc '
    apk add --no-cache build-base file &&
    cc -O2 -pipe -static -s -o /work/bot_c_linux_amd64 /work/bot.c &&
    chmod a+rx /work/bot_c_linux_amd64 &&
    file /work/bot_c_linux_amd64
  '
```

### Python：Linux PyInstaller 打包

```bash
docker run --rm --platform linux/amd64 \
  --mount "type=bind,source=$PWD,target=/work" \
  -w /work python:3.12-bookworm bash -lc '
    python -m pip install --no-cache-dir pyinstaller &&
    pyinstaller --noconfirm --clean --onefile \
      --name bot_py_linux_amd64 \
      --distpath /work \
      --workpath /tmp/pyinstaller \
      --specpath /tmp \
      /work/bot.py &&
    chmod a+rx /work/bot_py_linux_amd64
  '
```

## 7. 上传前验证文件类型

任何操作系统都应在上传前检查产物。Linux / macOS Terminal：

```bash
docker run --rm --platform linux/amd64 \
  --mount "type=bind,source=$PWD,target=/work,readonly" \
  alpine:3.20 sh -lc '
    apk add --no-cache file >/dev/null &&
    file /work/bot_c_linux_amd64 /work/bot_py_linux_amd64
  '
```

Windows PowerShell：

```powershell
docker run --rm --platform linux/amd64 `
  --mount "type=bind,source=$($PWD.Path),target=/work,readonly" `
  alpine:3.20 sh -lc '
    apk add --no-cache file >/dev/null &&
    file /work/bot_c_linux_amd64 /work/bot_py_linux_amd64
  '
```

两行都必须包含类似输出：

```text
ELF 64-bit LSB executable, x86-64
```

看到 `PE32`、`MS Windows`、`Mach-O`、`ARM aarch64`、`script` 或仅显示 Python source，
都说明文件不符合上传要求。不要只靠扩展名判断，也不要把错误格式改名后上传。

## 8. 在 Linux 容器中做通信冒烟

以下命令用一个最小 Holdem 首回合请求运行 C 产物。Python 产物只需把最后的文件名换成
`/work/bot_py_linux_amd64`。

```bash
printf '%s\n' '{"requests":[{"num_players":2,"dealer_id":0,"my_id":0,"my_chips":19950,"my_cards":[48,51],"public_cards":[],"history":[],"hand":0,"max_hand":70,"total_win_chips":[0,0],"total_win_games":[0,0]}],"responses":[]}' |
docker run --rm -i --platform linux/amd64 \
  --mount "type=bind,source=$PWD,target=/work,readonly" \
  debian:bookworm-slim /work/bot_c_linux_amd64
```

正确输出的前两行是：

```text
{"response":0}
>>>BOTZONE_REQUEST_KEEP_RUNNING<<<
```

真实 Traditional 对局读取第一行响应后会结束本次进程；真实 LongRunning 对局会校验
第二行握手并继续向同一进程发送增量请求。

## 9. 上传预检

上传新版本时先选择正确的游戏和运行模式。平台预检会：

1. 拒绝不是 Linux x86_64 ELF 的文件；
2. 使用与正式对局首回合相同的完整历史信封；
3. 要求响应对象包含 `response`；预检丢弃可选 `debug`，忽略其他顶层字段；
4. 校验本游戏的 response payload 类型；
5. LongRunning 额外要求精确握手。

预检使用独立的 **8 秒首回合健康检查**。它只证明文件可以启动并完成一次首回合通信；
上传后仍应创建挑战，验证完整历史重放、增量状态和整场策略。该 8 秒不会计入正式对局，
也不会改变 Pencil 每方 900 秒累计棋钟。

平台会先准备 Linux x86_64 沙箱镜像，再开始这 8 秒计时。若镜像仓库或平台沙箱故障，
上传会明确提示平台暂不可用，不会把镜像下载时间误判成 Bot 响应慢。

## 10. 常见故障

| 故障 | 结果 | 修复 |
|------|------|------|
| 上传 `.py`、`.exe`、Mach-O 或 ARM64 ELF | 上传校验拒绝 | 在 Linux amd64 容器中生成 x86-64 ELF |
| Windows/macOS 本机运行 PyInstaller | 生成宿主系统格式 | 使用 `python:3.12-bookworm` + `--platform linux/amd64` |
| 忘记换行或 flush | 决策超时并技术判负 | 每次输出完整行后立即 flush |
| 顶层输出 `0` | `protocol_error` | 输出 `{"response":0}` |
| 棋类输出裸 `{x,y}` | `protocol_error` | 输出 `{"response":{"x":x,"y":y}}` |
| 附加顶层 `debug` | 正式 Bot 对战终局后按权限私有展示；预检丢弃 | 保持小而结构化，绝不放密码或 token；动作仍只由 `response` 决定 |
| 附加 `data/globaldata` | 平台忽略 | 只有 `response` 与可选私有 `debug` 有定义 |
| 单行响应超过 64 KiB | `protocol_error` | 压缩或删减 `debug`，每次只输出一行 JSON |
| LongRunning 未精确握手 | `protocol_error`，不回退 | 首响应后立即输出固定握手行 |
| Holdem 把正数当目标总额 | 游戏动作错误 | 正数是本次额外投入筹码 |
| Traditional 不重放棋类历史 | 后续可能重复落子 | 重放全部 `requests[]/responses[]` |
| 依赖网络或持久磁盘 | 沙箱内失败 | 只读 stdin、写 stdout，状态放内存 |

## 11. 沙箱与时限

- 每个 Bot：1 核、512MB、无网络、只读根文件系统；仅 `/tmp` 可写。
- Holdem / Gomoku 使用平台固定的单步决策时限。
- Pencil 双方各有固定 900 秒累计棋钟；每次思考消耗同一份总预算。
- 棋钟信息只用于页面展示和回放，不改变 Bot 输入协议。

### 推荐的策略调试格式

调试面板适合记录分支、估值和最终选择，例如：

```json
{"response":0,"debug":{"phase":"river","equity":0.41,"pot_odds":0.28,"choice":"call"}}
```

`debug` 可以是字符串、数值、数组或对象；建议用短对象，便于按座位、决策序号和 duplicate
leg 阅读。平台会截断、清洗并脱敏，不能把它当持久存储或秘密保管箱。应用日志仍写 stderr，
但 stderr 只供管理员运维排障，不会混入调试面板。

完整字段与规则：[通信协议](#/wiki?slug=protocol) · [德州扑克](#/wiki?slug=texas) ·
[五子棋](#/wiki?slug=gomoku) · [点格棋](#/wiki?slug=pencil)。
