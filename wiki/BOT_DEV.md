# Bot 开发指南

本页教你从零编写一个可上传到平台参赛的 Bot。协议细节请先读[协议规范](#/wiki?slug=protocol)。

支持的游戏：`holdem`（德州）、`gomoku`（五子棋）、`pencil`（点格棋）。上传时必须选择正确的游戏类型。

## 0. 与 Botzone Bot 模型对照

本平台**完全遵循 [Botzone](https://wiki.botzone.org.cn/index.php?title=Bot) 标准**，你的 Botzone Bot 可直接上传运行：

| 项 | Botzone | 本平台 |
|----|---------|--------|
| 输入信封 | Traditional 完整历史 / LongRunning 首回合完整历史 + 后续单 request | 同左 |
| 输出信封 | `{"response": ...}` | 同左 |
| 德州 response | 裸整数 `-1/-2/0/>0` | 同左 |
| 运行模式 | Traditional / LongRunning | 都支持（上传时标明） |
| 长时运行握手 | `>>>BOTZONE_REQUEST_KEEP_RUNNING<<<` | 同左 |
| 资源 | 1 核 / 256MB / 默认 1s | 1 核 / 512MB / 默认 60s（可配） |

> 差异：本平台 Bot 进程**整场长驻**（不每回合重启）；Botzone 标准 Bot 无需改动即可运行（见 [协议](#/wiki?slug=protocol) §10）。手数默认 **70**（Botzone 文档 50）。

### 选择运行模式

上传 Bot 时需选择运行模式：

- **Traditional（传统）**：每个决策点收到**完整历史信封** `{"requests":[...],"responses":[...]}`，Bot 自己重放重建状态。适合无状态、易调试的 Bot。
- **LongRunning（长驻，默认推荐）**：首回合收到完整历史信封，之后只收到单条 `{"request":...}`。Bot 须自维护内存状态。首回合响应后输出 `>>>BOTZONE_REQUEST_KEEP_RUNNING<<<` 握手。适合有昂贵初始化（如神经网络）的 Bot。

棋类请分别阅读 [Gomoku](#/wiki?slug=gomoku)、[Pencil](#/wiki?slug=pencil)；样例：`samples/callbot.py`、`samples/gomokubot.py`、`samples/pencilbot.py`。

## 1. 核心思路

平台通过你的 Bot 的 **stdin / stdout** 与它通信（Botzone 信封）：

1. 平台往 stdin 写**一行 JSON** 信封（Traditional 完整历史 / LongRunning 单 request）。
2. 你读取、解析、从 `requests[-1]` 或 `request` 取当前决策负载。
3. 往 stdout 写**一行 JSON** 信封 `{"response": <裸整数>}`（德州：`-1` fold / `-2` allin / `0` call-check / `>0` raise 额外量）。
4. **立即换行并刷新缓冲区**，平台才能立刻读到。
5. LongRunning 模式下，首回合响应后再输出一行 `>>>BOTZONE_REQUEST_KEEP_RUNNING<<<` 声明长驻。
6. 不要退出进程——下一个决策点平台会再写一行，循环往复直到对局结束。

用伪代码表示就是：

```
for 每一行 stdin:
    信封 = JSON解析(这一行)
    请求 = 信封["request"] 或 信封["requests"][-1]   # 当前决策
    响应 = 做决策(请求)                              # 裸整数
    print(JSON序列化({"response": 响应}))            # 必须以 \n 结尾
    flush(stdout)                                    # 关键！
```

## 2. 最小 Bot（Python）

下面是一个只跟注 / 过牌的最小 Bot（仓库 `samples/callbot.py` 的核心逻辑，Botzone 信封）：

```python
#!/usr/bin/env python3
import json, sys

def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            env = json.loads(line)
        except json.JSONDecodeError:
            print(json.dumps({"response": -1}), flush=True)   # 无法解析就弃牌
            continue
        # 取当前决策负载：LongRunning 后续是 {"request":...}，否则 {"requests":[...]}
        if "request" in env:
            req = env["request"]
        else:
            reqs = env.get("requests") or []
            req = reqs[-1] if reqs else {}
        # callbot：永远 call/check（response=0）
        print(json.dumps({"response": 0}), flush=True)

if __name__ == "__main__":
    main()
```

要点：

- `flush=True`（或 `sys.stdout.flush()`）**必不可少**——Python 默认会缓冲 stdout，不刷新平台就读不到你的响应，最终超时判 fold。
- 解析失败时回一条 `{"response":-1}` 比让进程崩溃更安全。
- LongRunning 模式下，首回合响应后记得输出 `>>>BOTZONE_REQUEST_KEEP_RUNNING<<<`（否则平台会等待握手行直到超时；详见 [协议](#/wiki?slug=protocol) §1）。
- 德州 `response=0` 既是 call 也是 check——平台按 `to_call` 合法性自动判定。

## 3. 最小 Bot（C）

仓库 `samples/callbot.c` 用 Botzone 信封协议，编译后是独立 ELF 可执行文件，可直接上传：

```c
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* 从 Botzone 信封行粗略提取顶层字段（请求负载字段名独有，顶层搜即命中）。 */
static long peek_long(const char *s, const char *key) {
    char pat[32];
    snprintf(pat, sizeof(pat), "\"%s\"", key);
    const char *p = strstr(s, pat);
    if (!p) return 0;
    p = strchr(p, ':');
    if (!p) return 0;
    return atol(p + 1);
}

int main(void) {
    char *line = (char *)malloc(4000000);  /* 完整历史可能很长 */
    if (!line) return 1;
    while (fgets(line, 4000000, stdin)) {
        /* callbot：永远 call/check（Botzone 裸整数 0） */
        fputs("{\"response\":0}\n", stdout);
        fflush(stdout);                          /* 关键！必须刷新 */
    }
    free(line);
    return 0;
}
```

要点：

- `\n` 和 `fflush(stdout)` **缺一不可**。
- C 里不必用完整 JSON 库——`strstr`/`strchr`/`atol` 提取单个整数字段已足够，轻量且无依赖。
- 缓冲区要足够大（如 4MB）：Traditional 模式完整历史信封可能很长。

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
- **只读根文件系统** + 仅 `/tmp` 可写且 **可执行**（`--tmpfs /tmp:rw,exec,nosuid`；PyInstaller 自解压 / 动态链接需可执行映射；勿依赖在 `/tmp` 外写文件）。
- **最小权限**：`--cap-drop=ALL`、`--security-opt no-new-privileges`、以非 root 用户（65534）运行。
- **无 setuid**：禁止提权。

> 结论：Bot 应是**纯计算**程序，只读 stdin、写 stdout，不要尝试联网或依赖持久可写目录。

## 7. 本地调试

无需 Docker 也能在本地跑同架构 ELF（平台支持「本地执行」模式）：

```bash
# 在项目根目录
export BZ_BOT_LOCAL=1
scripts/platform-ctl.sh start
```

设置 `BZ_BOT_LOCAL=1` 后，平台直接在本机用子进程运行你上传的 ELF（绕过 Docker），方便快速调试。正式部署/比赛时应使用 Docker 沙箱。

### 直接手测你的 Bot

你也可以脱离平台，手动给 Bot 喂请求行来验证输出（Botzone 信封，LongRunning 首回合）：

```bash
echo '{"requests":[{"num_players":2,"dealer_id":0,"my_id":0,"my_chips":19950,"my_cards":[48,51],"public_cards":[],"history":[],"hand":0,"max_hand":70,"total_win_chips":[0,0],"total_win_games":[0,0],"to_call":50,"street_bet":50,"current_bet":100,"sb":50,"bb":100,"opp_chips":19900}],"responses":[]}' | ./mybot
# 期望输出: {"response":0}
```

## 8. 常见陷阱

| 陷阱 | 后果 | 正确做法 |
|------|------|----------|
| 忘了 `flush` stdout | 60 秒超时判 fold | 每次输出后 flush（Python `flush=True`、C `fflush`） |
| 输出不带换行 `\n` | 平台可能读不到完整行 | 响应以 `\n` 结尾 |
| 用 `print` 后进程阻塞缓冲 | 同上 | 显式刷新或关闭缓冲 |
| response 不是裸整数 | 判 fold（协议违规） | 德州 response 必须是 `-1/-2/0/>0` 整数 |
| `raise` 的正整数当成「加注到总额」 | 加注额不对被判 fold | 正整数是**额外下注筹码**（= 目标总额 − 本街已投） |
| `raise` 换算后总额低于最小加注 | 判 fold | 目标总额 ≥ 上次下注的 2 倍 |
| LongRunning 首回合没输出握手串 | 平台等待握手行直到超时 | 首回合响应后输出 `>>>BOTZONE_REQUEST_KEEP_RUNNING<<<` |
| 进程崩溃 / 主动 exit | 中途崩溃 → 计分判负（`completed`）；启动失败非赛事 → `aborted`（`bot_crashed`） | 保持进程存活，出错就回安全默认动作（扑克 `{"response":-1}`） |
| 上传 macOS 二进制 | 被拒绝 | 交叉编译为 Linux ELF |
| 依赖联网 / 文件写入 | 调用失败 | 纯计算，只用 stdin/stdout |

## 9. 进阶：做更聪明的 Bot

最小 Bot 只看 `to_call`。要做强 Bot，可以逐步利用请求负载里的更多信息：

- **`my_cards` 手牌**：解码（0–51）后判断起手牌强度（见协议规范的卡牌编码）。
- **`public_cards` 公共牌**：结合手牌评估当前牌力（一对 / 两对 / 顺子听牌等）。
- **`history` 历史**：推断对手本轮的动作模式（激进 / 被动）。
- **`my_chips` / `opp_chips` 筹码**：做基于筹码比的博弈（短码全押、深码价值下注）。
- **`hand` / `max_hand`**：知道当前手数与总手数，调整策略激进程度。
- **`total_win_chips` / `total_win_games`**：累计净筹码与赢手数，判断当前形势。

仓库 `samples/aggressivebot.c` 给出了一个稍微进阶的例子：在无人下注时主动加注，否则跟注/过牌，可作为改进起点。`samples/holdem_bots/` 下还有 6 种风格（fold/allin/raise/random/tight/loose）可供参考。

## 10. 运行时资源

Bot 在 Docker 中运行：`--cpus=1`、`--memory=512m`、无网络。决策超时默认 60s。详见[运行时与资源限制](#/wiki?slug=runtime)。
