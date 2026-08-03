# Bot 开发指南

本页教你从零编写一个可上传到平台参赛的 Bot。协议细节请先读[协议规范](#/wiki?slug=protocol)。

支持的游戏：`holdem`（德州）、`gomoku`（五子棋）、`pencil`（点格棋）。上传时必须选择正确的游戏类型。

## 0. 与 Botzone Bot 模型对照

| 项 | Botzone | 本平台 |
|----|---------|--------|
| 输入 | 每回合整包 JSON（含 `requests`/`responses` 历史） | 长驻进程，每步一行当前请求 |
| 输出 | `{"response": ...}` 信封 | 直接一行决策 JSON |
| 资源 | 1 核 / 256MB / 默认 1s | 1 核 / 512MB / 默认 60s（可配） |
| 长时运行 | 可选 `>>>BOTZONE_REQUEST_KEEP_RUNNING<<<` | 默认整场长驻 |

棋类请分别阅读 [Gomoku](#/wiki?slug=gomoku)、[Pencil](#/wiki?slug=pencil)；样例：`samples/callbot.py`、`samples/gomokubot.py`、`samples/pencilbot.py`。

## 1. 核心思路

平台通过你的 Bot 的 **stdin / stdout** 与它通信：

1. 平台往 stdin 写**一行 JSON** 请求（描述当前牌局状态）。
2. 你读取、解析、做出决策。
3. 往 stdout 写**一行 JSON** 响应（`fold` / `call` / `check` / `raise` / `all-in`）。
4. **立即换行并刷新缓冲区**，平台才能立刻读到。
5. 不要退出进程——下一个决策点平台会再写一行，循环往复直到对局结束。

用伪代码表示就是：

```
for 每一行 stdin:
    请求 = JSON解析(这一行)
    响应 = 做决策(请求)
    print(JSON序列化(响应))   # 必须以 \n 结尾
    flush(stdout)             # 关键！
```

## 2. 最小 Bot（Python）

下面是一个只跟注 / 过牌的最小 Bot（仓库 `samples/callbot.py` 的核心逻辑）：

```python
#!/usr/bin/env python3
import json, sys

def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            print(json.dumps({"a": "f"}), flush=True)   # 无法解析就弃牌
            continue
        to_call = int(req.get("to", 0) or 0)
        if to_call > 0:
            print(json.dumps({"a": "c"}), flush=True)   # 有人下注就跟
        else:
            print(json.dumps({"a": "k"}), flush=True)   # 没人下注就过牌

if __name__ == "__main__":
    main()
```

要点：

- `flush=True`（或 `sys.stdout.flush()`）**必不可少**——Python 默认会缓冲 stdout，不刷新平台就读不到你的响应，最终超时判 fold。
- 解析失败时回一条 `{"a":"f"}` 比让进程崩溃更安全。
- `req["to"]` 是跟注额：`>0` 需要补筹码（call），`==0` 无需补（check）。

## 3. 最小 Bot（C）

仓库 `samples/callbot.c` 同样只看 `to` 字段，编译后是一个独立的 ELF 可执行文件，可直接上传：

```c
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* 从一行 JSON 里粗略提取 "to": 数字 的值 */
static int peek_to_call(const char *s) {
    const char *p = strstr(s, "\"to\"");
    if (!p) return 0;
    p = strchr(p, ':');
    if (!p) return 0;
    return atoi(p + 1);
}

int main(void) {
    char *line = NULL;
    size_t cap = 0;
    ssize_t n;
    while ((n = getline(&line, &cap, stdin)) != -1) {
        /* 去掉行尾换行 */
        while (n > 0 && (line[n-1] == '\n' || line[n-1] == '\r'))
            line[--n] = 0;
        if (n <= 0) continue;
        int to = peek_to_call(line);
        if (to > 0)
            fputs("{\"a\":\"c\"}\n", stdout);   /* 跟注 */
        else
            fputs("{\"a\":\"k\"}\n", stdout);   /* 过牌 */
        fflush(stdout);                          /* 关键！必须刷新 */
    }
    free(line);
    return 0;
}
```

要点：

- `\n` 和 `fflush(stdout)` **缺一不可**。
- C 里不必用完整 JSON 库——`strstr`/`strchr`/`atoi` 提取单个整数字段已足够，轻量且无依赖。

## 4. 编译与交叉编译

平台宿主是 **Linux 服务器**。可执行文件必须是 **Linux ELF** 或 **Windows PE**；**macOS Mach-O 会被拒绝上传**。

### 4.1 Linux ELF（推荐）

```bash
# 静态链接，兼容性最好（不依赖目标机器的 glibc 版本）
cc -O2 -static -o mybot callbot.c
# 若静态链接失败（某些系统缺静态库），退而用动态链接：
cc -O2 -o mybot callbot.c
chmod +x mybot
file mybot      # 确认输出 "ELF ... x86-64"
```

仓库提供一键脚本：

```bash
samples/build_sample.sh        # 产出 samples/callbot_linux_amd64
```

### 4.2 Windows PE（经 Wine 容器执行）

若你更习惯在 Windows 工具链下开发，可交叉编译为 PE，平台会在 Wine 容器中运行：

```bash
# MinGW 交叉编译（需安装 mingw-w64）
x86_64-w64-mingw32-gcc -O2 -o mybot.exe callbot.c
```

> 平台运行 PE 依赖 Wine 镜像；若部署环境未配置 Wine，PE Bot 将无法运行。**Linux ELF 是首选**。

### 4.3 macOS Mach-O —— 不支持

平台**无法在 Linux 上可靠沙箱执行 macOS 二进制**，上传时会被直接拒绝。macOS 用户请交叉编译为 Linux ELF 或 Windows PE（见上）。

## 5. 上传与分类

在「我的 Bot」页面：

1. 上传编译好的二进制文件。
2. 平台通过**文件魔数**自动识别格式与架构，无需你声明：
   - `ELF`（`\x7fELF`）→ Linux；进一步识别 `amd64 / arm64 / i386` 架构。
   - `MZ ... PE`（Windows PE 头）→ Windows；识别 `i386 / amd64`。
   - Mach-O（`FEEDFACE / FEEDFACF` 等）→ **拒绝**。
3. 同一个 Bot 名字可多次上传新版本，平台保留版本历史并记录校验和、大小、架构。
4. 上传后记得在 Bot 设置里把它**设为活跃（active）**，否则无法参赛。

## 6. 沙箱安全基线

你的 Bot 在受限的沙箱里执行，请确保程序不依赖被禁用的能力：

- **无网络**：容器以 `--network=none` 启动，任何联网调用都会失败。
- **资源限制**：内存上限（默认 512MB）、CPU（默认 1 核）。
- **只读根文件系统** + 仅 `/tmp` 可写（`noexec`，不能在里面执行程序）。
- **最小权限**：`--cap-drop=ALL`、`--security-opt no-new-privileges`、以非 root 用户（65534）运行。
- **无 setuid**：禁止提权。

> 结论：Bot 应是**纯计算**程序，只读 stdin、写 stdout，不要尝试读写文件系统、联网、调用外部命令。

## 7. 本地调试

无需 Docker 也能在本地跑同架构 ELF（平台支持「本地执行」模式）：

```bash
# 在项目根目录
export BZ_BOT_LOCAL=1
scripts/platform-ctl.sh start
```

设置 `BZ_BOT_LOCAL=1` 后，平台直接在本机用子进程运行你上传的 ELF（绕过 Docker），方便快速调试。正式部署/比赛时应使用 Docker 沙箱。

### 直接手测你的 Bot

你也可以脱离平台，手动给 Bot 喂请求行来验证输出：

```bash
echo '{"v":1,"t":"act","h":0,"H":70,"id":0,"d":0,"mc":[48,51],"pc":[],"hist":[],"c":19950,"o":19900,"sb":50,"bb":100,"to":50}' | ./mybot
# 期望输出: {"a":"c"}
```

## 8. 常见陷阱

| 陷阱 | 后果 | 正确做法 |
|------|------|----------|
| 忘了 `flush` stdout | 60 秒超时判 fold | 每次输出后 flush（Python `flush=True`、C `fflush`） |
| 输出不带换行 `\n` | 平台可能读不到完整行 | 响应以 `\n` 结尾 |
| 用 `print` 后进程阻塞缓冲 | 同上 | 显式刷新或关闭缓冲 |
| `to=0` 时回 `call` | 判 fold（此时只能 check/raise） | `to=0` 用 `{"a":"k"}` |
| `raise` 的 `x` 当成增量 | 加注额不对被判 fold | `x` 是**加注到的总额** |
| `raise` 的 `x` 低于最小加注 | 判 fold | `x` ≥ 上次加注的 2 倍 |
| 进程崩溃 / 主动 exit | 整场对局 aborted（`bot_crashed`） | 保持进程存活，出错就回安全默认动作（扑克 `{"a":"f"}`） |
| 上传 macOS 二进制 | 被拒绝 | 交叉编译为 Linux ELF |
| 依赖联网 / 文件写入 | 调用失败 | 纯计算，只用 stdin/stdout |

## 9. 进阶：做更聪明的 Bot

最小 Bot 只用 `to`。要做强 Bot，可以逐步利用请求里的更多信息：

- **`mc` 手牌**：解码后判断起手牌强度（见协议规范的卡牌编码）。
- **`pc` 公共牌**：结合手牌评估当前牌力（一对 / 两对 / 顺子听牌等）。
- **`hist` 历史**：推断对手本轮的动作模式（激进 / 被动）。
- **`c` / `o` 筹码**：做基于筹码比的博弈（短码全押、深码价值下注）。
- **`h` / `H`**：知道当前手数与总手数，调整策略激进程度。

仓库 `samples/aggressivebot.c` 给出了一个稍微进阶的例子：在无人下注时主动加注，否则跟注/过牌，可作为改进起点。

## 10. 运行时资源

Bot 在 Docker 中运行：`--cpus=1`、`--memory=512m`、无网络。决策超时默认 60s。详见[运行时与资源限制](#/wiki?slug=runtime)。
