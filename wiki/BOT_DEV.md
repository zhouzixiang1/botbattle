# Bot 开发指南

本页教你从零编写一个可上传到平台参赛的 Bot。协议细节请先读[协议规范](#/wiki?slug=protocol)。

支持的游戏：`holdem`（德州）、`gomoku`（五子棋）、`pencil`（点格棋）。上传时必须选择正确的游戏类型。

## 0. 与 Botzone Bot 模型对照

本平台兼容 [Botzone](https://wiki.botzone.org.cn/index.php?title=Bot) 的信封、运行模式与动作编码。只依赖已列标准字段的 Bot 可迁移；固定规则、资源限制和可选字段能力以本站文档为准：

| 项 | Botzone | 本平台 |
|----|---------|--------|
| 输入信封 | Traditional 完整历史 / LongRunning 首回合完整历史 + 后续单 request | 同左 |
| 输出信封 | `{"response": ...}` | 同左 |
| 德州 response | 裸整数 `-1/-2/0/>0` | 同左 |
| 运行模式 | Traditional / LongRunning | 都支持（上传时标明） |
| 长时运行握手 | `>>>BOTZONE_REQUEST_KEEP_RUNNING<<<` | 同左 |
| 资源 | 1 核 / 256MB / 默认 1s | 1 核 / 512MB；holdem/gomoku 默认 60s/决策（可配），Pencil 固定 900s/方累计 |

> 本平台默认 **Traditional**：每个决策点重启进程并发送完整历史。显式选择 LongRunning 后进程才整场长驻。德州手数固定 **70**（Botzone 文档默认 50）。

### 选择运行模式

上传 Bot 时需选择运行模式：

- **Traditional（传统，默认）**：每个决策点进程重启并收到**完整历史信封** `{"requests":[...],"responses":[...]}`，Bot 自己重放重建状态。适合无状态、易调试的 Bot。
- **LongRunning（长驻，显式选择）**：首回合收到完整历史信封，握手成功后才改收单条 `{"request":...}`。Bot 须自维护内存状态。适合有昂贵初始化（如神经网络）的 Bot。

LongRunning 首个响应后，平台最多等待 1 秒读取握手；没收到时会在**同一进程**继续发送完整历史作兼容回退。这不等于 Traditional 的逐回合重启，状态型 Bot 不要依赖回退。

棋类请分别阅读 [Gomoku](#/wiki?slug=gomoku)、[Pencil](#/wiki?slug=pencil)。`samples/*.py` 是便于阅读和本地调试的**源码**，不能把 `.py` 文件直接上传；可直接上传的是构建脚本产出的 ELF。

## 1. 核心思路

平台通过你的 Bot 的 **stdin / stdout** 与它通信（Botzone 信封）：

1. 平台往 stdin 写**一行 JSON** 信封（Traditional 完整历史 / LongRunning 单 request）。
2. 你读取、解析、从 `requests[-1]` 或 `request` 取当前决策负载。
3. 往 stdout 写**一行 JSON** 信封 `{"response": <裸整数>}`（德州：`-1` fold / `-2` allin / `0` call-check / `>0` raise 额外量）。
4. **立即换行并刷新缓冲区**，平台才能立刻读到。
5. LongRunning 模式下，首回合响应后再输出一行 `>>>BOTZONE_REQUEST_KEEP_RUNNING<<<` 声明长驻。
6. LongRunning 握手成功后不要退出进程；Traditional 每个决策点都会启动一个新进程。

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
    first_response = True
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
        if first_response:
            print(">>>BOTZONE_REQUEST_KEEP_RUNNING<<<", flush=True)
            first_response = False

if __name__ == "__main__":
    main()
```

要点：

- `flush=True`（或 `sys.stdout.flush()`）**必不可少**——Python 默认会缓冲 stdout，不刷新平台就读不到你的响应，最终超时判 fold。
- 解析失败时回一条 `{"response":-1}` 比让进程崩溃更安全。
- LongRunning 模式下，首回合响应后输出 `>>>BOTZONE_REQUEST_KEEP_RUNNING<<<`。未握手不会让本次动作超时，但会增加最多 1 秒探测等待，并进入同进程完整历史的兼容回退。
- 德州 `response=0` 既是 call 也是 check——平台按当前下注合法性自动判定为跟注或过牌。

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
    int first_response = 1;
    while (fgets(line, 4000000, stdin)) {
        /* callbot：永远 call/check（Botzone 裸整数 0） */
        fputs("{\"response\":0}\n", stdout);
        if (first_response) {
            fputs(">>>BOTZONE_REQUEST_KEEP_RUNNING<<<\n", stdout);
            first_response = 0;
        }
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

仓库提供一键脚本，同时构建德州与点格棋的可上传 ELF：

```bash
bash samples/build_sample.sh
# 产出 samples/callbot_linux_amd64（holdem）
#      samples/pencilbot_linux_amd64（pencil）
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

上传时会做一次轻量响应预检，但当前预检只验证单个游戏 payload/响应形状，未按所选 `runtime_mode` 执行完整 Botzone 信封或多回合重放。预检通过不等于能完成整场；请先按第 7 节本地调试，并用正确运行模式创建挑战赛验证。

## 6. 沙箱安全基线

你的 Bot 在受限的沙箱里执行，请确保程序不依赖被禁用的能力：

- **无网络**：沙箱完全断网，任何联网调用都会失败。
- **资源限制**：内存上限 **512MB**、CPU **1 核**。
- **磁盘**：根文件系统只读，Linux ELF Bot **仅 `/tmp` 可写且可执行**（如需写临时文件或 PyInstaller 自解压请放 `/tmp`）。Windows PE 除 `/tmp` 外会获得 Wine 自身使用的隔离临时 `HOME/WINEPREFIX`；它同样位于限容 tmpfs、对局后销毁，不是持久磁盘。Bot 不应依赖这些路径保留数据。
- **最小权限**：以非 root 用户运行，无提权能力。

> 结论：Bot 应是**纯计算**程序，只读 stdin、写 stdout，不要尝试联网或依赖持久可写目录。

## 7. 本地调试

你也可以脱离平台，手动给 Bot 喂请求行来验证输出（Botzone 信封，LongRunning 首回合）：

```bash
echo '{"requests":[{"num_players":2,"dealer_id":0,"my_id":0,"my_chips":19950,"my_cards":[48,51],"public_cards":[],"history":[],"hand":0,"max_hand":70,"total_win_chips":[0,0],"total_win_games":[0,0]}],"responses":[]}' | ./mybot
# 期望输出: {"response":0}
```

## 8. 常见陷阱

| 陷阱 | 后果 | 正确做法 |
|------|------|----------|
| 忘了 `flush` stdout | 等到当前时限后失败：扑克 fold，棋类判负；Pencil 会耗尽该方累计棋钟 | 每次输出后 flush（Python `flush=True`、C `fflush`） |
| 输出不带换行 `\n` | 平台可能读不到完整行 | 响应以 `\n` 结尾 |
| 用 `print` 后进程阻塞缓冲 | 同上 | 显式刷新或关闭缓冲 |
| response 不是裸整数 | 判 fold（协议违规） | 德州 response 必须是 `-1/-2/0/>0` 整数 |
| `raise` 的正整数当成「加注到总额」 | 加注额不对被判 fold | 正整数是**额外下注筹码**（= 目标总额 − 本街已投） |
| `raise` 换算后总额低于最小加注 | 判 fold | 目标总额 ≥ 上次下注的 2 倍 |
| LongRunning 首回合没输出握手串 | 首个响应后额外等待最多 1 秒，随后在同一进程发送完整历史；状态型 Bot 可能错乱 | 首回合响应后立即输出 `>>>BOTZONE_REQUEST_KEEP_RUNNING<<<` |
| 进程崩溃 / 主动 exit | 中途崩溃 → 计分判负；Bot-vs-Bot 启动失败 → `completed` + `technical_loss`；人类对战启动失败 → `aborted` | 保持进程存活，出错就回安全默认动作（扑克 `{"response":-1}`） |
| 上传 macOS 二进制 | 被拒绝 | 交叉编译为 Linux ELF |
| 依赖联网 / 文件写入 | 调用失败 | 纯计算，只用 stdin/stdout |
| 依赖 Wine 配置持久化 | 下场对局配置消失 | PE 的 `HOME/WINEPREFIX` 是单场隔离 tmpfs，不持久保存 |

## 9. 进阶：做更聪明的 Bot

最小 Bot 只看 `history` + `my_chips`（从历史重放推导跟注额、对手筹码）。要做强 Bot，可以逐步利用请求负载里的更多信息：

- **`my_cards` 手牌**：解码（0–51）后判断起手牌强度（见协议规范的卡牌编码）。
- **`public_cards` 公共牌**：结合手牌评估当前牌力（一对 / 两对 / 顺子听牌等）。
- **`history` 历史**：重放重建完整局面（双方本轮下注、当前需跟注额、对手剩余筹码），并推断对手动作模式（激进 / 被动）。
- **`my_chips` 自己的筹码**：结合 `history` 推导对手筹码，做基于筹码比的博弈（短码全押、深码价值下注）。
- **`hand` / `max_hand`**：知道当前手数与总手数，调整策略激进程度。
- **`total_win_chips` / `total_win_games`**：累计净筹码与赢手数，判断当前形势。

仓库 `samples/aggressivebot.c` 给出了一个稍微进阶的例子：在无人下注时主动加注，否则跟注/过牌，可作为改进起点。`samples/holdem_bots/` 下还有 6 种风格（fold/allin/raise/random/tight/loose）可供参考。

## 10. 运行时资源

Bot 在 Docker 中运行：单核、512MB 内存、无网络。holdem / gomoku 单步决策超时默认 60 秒（管理员可配）；Pencil 双方各使用固定 900 秒累计棋钟，每次思考消耗同一份总预算。`time_used` / `time_out` 只进入平台回放和 SSE，不会改变 Bot 的输入协议。
