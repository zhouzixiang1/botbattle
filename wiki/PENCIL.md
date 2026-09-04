# 点格棋

本平台 `game_id`：`pencil`。

## 规则

1. 固定 **N=6** 个点，即 6×6 点阵、25 个格子。
2. 内部使用 11×11 交错坐标板：偶偶位置是点，奇偶/偶奇位置是可占边，奇奇位置是格心。
3. 坐标从 0 开始，原点左上，先 `x` 后 `y`。
4. 座位 0 红方先手，座位 1 蓝方后手。
5. 玩家只能占用尚未使用的相邻点之间的边。
6. 某格四边补齐时，最后占边者得分并继续行动；未得分则换手。
7. 一方先到 13 格立即获胜；若走到终局，则格数更多者获胜。
8. 对局创建时会冻结一种平台时限：默认为双方各 **900 秒（15 分钟）累计棋钟**，也可选每步最多 **1 秒**的线上快速模式。

格式正确但占用非法边由裁判判负；Bot 棋钟耗尽为 `timeout` 技术负；信封/response
错误为 `protocol_error` 技术负。

对局结果和回放保留裁判终局原因：`majority` 表示一方先取得过半格子，`score` 表示按
最终得分判定，`draw` 表示最终同分；三者均为正常判定并使用中性文案。`illegal`（非法连边）、
`error`（决策异常）和 `crash`（Bot 运行异常）属于异常判罚。`completed` 表示裁判已经完成
结算，原因只是结果细节；页面不会把正常得分胜负误显示为异常，也不会直接显示内部英文原因码。

## 坐标模型

| 坐标条件 | 类型 |
|----------|------|
| `x%2==0 && y%2==0` | 点 |
| `(x+y)%2==1` | 可占边 |
| `x%2==1 && y%2==1` | 格子中心 |

合法边坐标范围是 `0..10`，并满足 `(x+y)%2==1`。

## 通信 payload

通信必须使用[统一信封](#/wiki?slug=protocol)。请求 payload：

```json
{"x":3,"y":4,"pass":0,"me":0,"scores":[1,0]}
```

| 字段 | 含义 |
|------|------|
| `x`,`y` | 对手最近占用的边；首回合/连走通知为 `-1,-1` |
| `pass` | 对手是否得分连走 |
| `me` | 本方座位 |
| `scores` | `[红方得分, 蓝方得分]` |

普通响应：

```json
{"response":{"x":5,"y":4}}
```

当 `pass=1`，本方不实际占边，必须响应：

```json
{"response":{"x":-1,"y":-1}}
```

顶层裸 `{x,y}` 不合法。Traditional 重放全部历史；LongRunning 必须精确握手且后续
只接收单 request，没有模式回退。

## 连走时序

1. A 占边并成格。
2. 平台向 B 发送该边且 `pass=1`；B 回 `-1,-1`。
3. 平台向 A 发送 `x=y=-1, pass=0`；A 继续占边。
4. 未成格时直接换手并发送 `pass=0`。

## 时限与回放

累计模式 `pencil_per_side_total_900s_v1` 为每方分别累计 900 秒，每局重置；
单步模式 `pencil_per_decision_1s_v1` 每次决策重新获得 1 秒。两种模式都只计“完整请求交给已就绪 Bot”到“完整响应到达”的区间，平台排队、进程启动与容器预热不计入。
成功决策产生 `time_used`，耗尽产生 `time_out`。时限信息只用于页面展示和回放，不会混入 Bot stdin 请求。
人机练习只对 Bot 使用所选时限，真人仍使用页面的防挂机等待时限；这是页面会明确标记的非对称练习，不计 Rating。
网页会把这些事件显示为“座位 N · 已用/剩余时间”或“座位 N · 超时”；非法连边和错误让行
也会显示为中文判罚，不直接展示内部事件码。
裁判因非法连边、运行错误或崩溃直接判负时，页面按裁判的 2:0 技术结果显示；协议错误或
超时则保留终止前棋盘比分，并另行标明技术判负。

网页棋盘按 6×6 点阵的真实方形比例展示：浅灰边表示尚未占用，红/蓝粗边分别属于座位
1/2，最近一条边带金色外框，已闭合格直接填满四边围出的区域并标记座位数字 1/2。格内
不使用 Bot 名首字母，避免两名 Bot 同名或首字母相同时无法区分。

观赛和真人页面会按屏幕比例重排：超宽屏依次显示局面概览、棋盘和动作时序；普通桌面
把紧凑概览置于棋盘上方，并在右侧保留动作时序；横屏平板把概览与棋盘并排；手机竖向
堆叠。局面概览始终显示双方比分与棋钟、已连/剩余边、实际已占/未决格、行动方和最近
连边；超宽屏还会展开格子归属缩略图、红蓝连边构成与过半门槛。技术判负时，裁判比分
与终止前实际棋盘占格会分别显示，不会把判定分数伪装成已闭合格。棋盘不会为了填满横向
空白而拉伸变形，动作时序过长时在自己的区域内滚动。

真人对战只会吸附到鼠标附近**尚未占用的合法边**。点击点、格心、已占边或棋盘外区域时，
页面会提示重新选择，但不会向裁判发送动作；合法 hover 会以绿色预览将要提交的整条边。
这层前端过滤用于避免误点，不能替代裁判：绕过页面提交非法坐标仍会按本页规则判负。
不用鼠标时，可用 Tab 聚焦棋盘、方向键在尚未占用的边之间切换，再按 Enter 或空格提交；
读屏标签会播报当前交错坐标。

如果对手刚围成格并继续连走，页面会禁用棋盘并显示“确认让行”。点击后只发送协议要求的
`{"response":{"x":-1,"y":-1}}`；此时点击任何边都不会发送。让行完成后，轮到真人正常
连边时才重新启用棋盘。

上传时的预检是独立的 **8 秒首回合健康检查**，只确认程序能启动、读取完整首回合信封并
返回合法响应。这个短时预检不属于正式比赛，也不会替代或扣减随后对局冻结的时限。

## 锦标赛流程

点格棋赛事按“草稿 → 开放报名 → 发布排期 → 开赛 → 阶段休息（模板包含时）→ 已结束”推进，
每场固定 N=6，胜 / 平 / 负按 **2 / 1 / 0** 计分。创建页把“赛制”与“对局时限”分开：
线上预赛快捷预设使用每步 1 秒，线下决赛预设使用每方累计 900 秒；两者都可选全员或分组双循环。选择 `pencil_drr` 或 `pencil_group_drr` 时，可在“关联赛事（可选）”中选择另一场当前账号有权查看的点格棋赛事；普通组织者可选择公开赛事及自己的隐藏赛事，管理员可查看全部。来源不必已经结束或生成正式榜。这个链接只方便在两场独立赛事间导航，不复制名单、成绩或晋级关系，也可保持“不关联其他赛事”。
累计 900 秒模式的每局 ETA 上界为 1800 秒；每步 1 秒模式按 84 次定时请求估算：60 次占边请求，加上最多 24 次得分后的强制 `pass` 确认。基础 ETA 不包含既有队列等待或阶段休息。

当前 6 个模板都可新建，建议人数、用途与时长等级只作指导，不阻断自由选择：

- `pencil_drr`：双循环，每对 Bot 交换红/蓝方各赛一局；
- `pencil_group_drr`：单阶段随机均衡分组双循环，不附带淘汰赛；组织者选择分组数，发布时必须至少 2 组且每组至少 2 人；
- `pencil_group_drr_ko`：四组双循环、每组前二晋级单败；
- `pencil_swiss_ranked`：瑞士制产生全员最终排名；
- `pencil_swiss_ko`：瑞士筛出 8 强后单败；
- `pencil_ko`：纯单败，基础对局数为参赛人数减一。

全员与分组循环都没有人数硬上限；完整排期进入持久队列，页面会显示基础对局、基础计分场、
基础 ETA 及超过 8/24 小时风险，并可建议 Swiss 或纯单败，但不会拒绝发布。Pencil 模板不使用
Holdem/Gomoku 的成对换座无限决胜 marker，估算也不会显示该项不封顶风险。

- **随机均衡分组双循环**：平台只抽签一次，组间人数差不超过 1；算法版本、审计值、组规模与最终分组在发布时冻结，重试不会重新抽签。页面同时显示各组权威榜与跨组总榜。跨组依次比较组内名次、每局积分率、标准化对手强度、每局归一化分差、技术负率和冻结抽签序，不在不同组之间使用直接交手。

- **分组双循环 → 单败**：按种子蛇形分成 4 组，组内每对 Bot 交换先后手比赛两次；各组
  前 2 名在休息期确认晋级并进入单败对阵树；分组人数没有硬上限，只会增加组内场数和耗时。
- **瑞士 → 单败**：每轮结束后再按当前积分生成下一轮，优先安排同分且未交手的 Bot；完成
  系统按人数计算的轮数后，前 8 名晋级单败。
- **单败阶段**：每场胜者进入下一轮，轮空直接晋级；对局时限在每场独立计算，不会从分组或瑞士阶段带入淘汰赛。

发布排期会冻结该轮 Bot 版本；模板允许时，参赛者只能在阶段休息中更换后续派遣 Bot。详情页
按阶段展示对阵、明确赛果、积分和晋级，完赛正式榜对同积分行显示后端保存的真实破同分字段。
完整生命周期、时间与全局排队规则见[平台功能指南](#/wiki?slug=guide)。

## 快速开始

下面两个程序会重放完整历史、正确处理 `pass`，并支持两种运行模式。上传文件必须是
Linux x86_64 ELF；Windows、Linux、macOS 的构建命令见
[Bot 开发指南](#/wiki?slug=bot-dev)。实现自己的策略时请保留这些状态处理要点：

- Traditional 从完整 `requests[]/responses[]` 重建所有已占边；
- LongRunning 首回合重建后握手，后续在内存中增量维护边与比分；
- `pass=1` 时不要选择新边，只返回 `-1,-1`；
- 对手成格后的 pass 通知和自己连走通知都不能误记成实际边。

### 完整 C 示例

把以下内容保存为 `bot.c`，再按 [Bot 开发指南](#/wiki?slug=bot-dev)中你的操作系统对应
的 C 命令构建。该示例会重放完整历史、处理 pass，并可用于两种运行模式。

<!-- SAMPLE:pencil:c -->
```c
/* 点格棋随机合法边样例 Bot（平台 Traditional + LongRunning）。
 *
 * Traditional 每次启动都会收到完整 requests/responses 历史；本程序先从历史重建
 * 已占边，再选择新边。LongRunning 首回合同样重建历史，响应后输出标准握手，后续
 * 根据单条 request 增量维护棋盘。两种模式可使用同一个二进制。
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define N 6
#define SIZE (2 * N - 1)
#define EDGE 4
#define EDGE_USED 5
#define KEEP_RUNNING ">>>BOTZONE_REQUEST_KEEP_RUNNING<<<"

static int g[SIZE][SIZE];

static void init_board(void) {
    for (int x = 0; x < SIZE; x++)
        for (int y = 0; y < SIZE; y++)
            g[x][y] = ((x + y) % 2 == 1) ? EDGE : 0;
}

static int is_legal_edge(int x, int y) {
    return x >= 0 && x < SIZE && y >= 0 && y < SIZE && g[x][y] == EDGE;
}

static int number_after(const char *p, const char *key, int def) {
    char pat[32];
    snprintf(pat, sizeof(pat), "\"%s\"", key);
    p = strstr(p, pat);
    if (!p) return def;
    p = strchr(p + strlen(pat), ':');
    return p ? atoi(p + 1) : def;
}

/* 信封里只有 Pencil request/response 对象含 x/y；扫描全部坐标即可同时重放
 * requests[]（对手边）与 responses[]（自己的边）。重复坐标无害。 */
static void replay_all_edges(const char *line) {
    const char *p = line;
    while ((p = strstr(p, "\"x\"")) != NULL) {
        int x = number_after(p, "x", -1);
        int y = number_after(p, "y", -1);
        if (is_legal_edge(x, y)) g[x][y] = EDGE_USED;
        p += 3;
    }
}

static int last_number(const char *line, const char *key, int def) {
    char pat[32];
    snprintf(pat, sizeof(pat), "\"%s\"", key);
    const char *p = line;
    const char *last = NULL;
    while ((p = strstr(p, pat)) != NULL) {
        last = p;
        p += strlen(pat);
    }
    return last ? number_after(last, key, def) : def;
}

static void emit_move(int x, int y) {
    printf("{\"response\":{\"x\":%d,\"y\":%d}}\n", x, y);
    fflush(stdout);
}

int main(void) {
    srand((unsigned)time(NULL));
    init_board();
    int first_response = 1;
    char *line = NULL;
    size_t cap = 0;
    ssize_t n;

    while ((n = getline(&line, &cap, stdin)) != -1) {
        while (n > 0 && (line[n - 1] == '\n' || line[n - 1] == '\r'))
            line[--n] = 0;
        if (n <= 0) continue;

        /* 完整历史是权威状态。Traditional 每回合进程会重启；这里重置也让手工
         * 连续喂多份完整信封时保持正确。 */
        if (strstr(line, "\"requests\"") != NULL) init_board();
        replay_all_edges(line);

        int pass = last_number(line, "pass", 0);
        if (pass == 1) {
            emit_move(-1, -1);
        } else {
            int xs[SIZE * SIZE], ys[SIZE * SIZE], count = 0;
            for (int x = 0; x < SIZE; x++)
                for (int y = 0; y < SIZE; y++)
                    if (g[x][y] == EDGE) {
                        xs[count] = x;
                        ys[count] = y;
                        count++;
                    }
            if (count == 0) {
                emit_move(-1, -1);
            } else {
                int idx = rand() % count;
                int x = xs[idx], y = ys[idx];
                g[x][y] = EDGE_USED;
                emit_move(x, y);
            }
        }

        /* Traditional 模式读取第一行后停止该进程，因此额外握手无副作用；
         * LongRunning 模式会校验它并切换为后续单 request。 */
        if (first_response) {
            fputs(KEEP_RUNNING "\n", stdout);
            fflush(stdout);
            first_response = 0;
        }
    }
    free(line);
    return 0;
}
```

### 完整 Python 示例

把以下内容保存为 `bot.py`，再按开发指南使用 Linux amd64
`python:3.12-bookworm` 容器中的 PyInstaller 打包；不要上传源文件本身。

<!-- SAMPLE:pencil:python -->
```python
#!/usr/bin/env python3
"""点格棋随机合法边样例（源码；支持平台 Traditional/LongRunning）。"""
from __future__ import annotations

import json
import random
import sys
from typing import Any

N = 6
SIZE = 2 * N - 1
KEEP_RUNNING = ">>>BOTZONE_REQUEST_KEEP_RUNNING<<<"


def make_board() -> list[list[bool]]:
    """True 表示尚未占用的合法边。"""
    return [[(x + y) % 2 == 1 for y in range(SIZE)] for x in range(SIZE)]


board = make_board()


def mark(move: Any) -> None:
    if not isinstance(move, dict):
        return
    try:
        x, y = int(move.get("x", -1)), int(move.get("y", -1))
    except (TypeError, ValueError):
        return
    if 0 <= x < SIZE and 0 <= y < SIZE and (x + y) % 2 == 1:
        board[x][y] = False


def load_turn(envelope: dict[str, Any]) -> dict[str, Any]:
    """返回当前 request，并按完整历史或单 request 更新棋盘。"""
    global board
    if isinstance(envelope.get("requests"), list):
        board = make_board()
        requests = envelope["requests"]
        responses = envelope.get("responses") or []
        for request in requests:
            mark(request)
        for response in responses:
            mark(response)
        return requests[-1] if requests and isinstance(requests[-1], dict) else {}

    request = envelope.get("request")
    if not isinstance(request, dict):
        raise ValueError("增量信封缺少 request")
    mark(request)
    return request


def choose_move(request: dict[str, Any]) -> tuple[int, int]:
    if int(request.get("pass") or 0) == 1:
        return -1, -1
    legal = [
        (x, y)
        for x in range(SIZE)
        for y in range(SIZE)
        if board[x][y]
    ]
    if not legal:
        return -1, -1
    x, y = random.choice(legal)
    board[x][y] = False
    return x, y


def main() -> None:
    first_response = True
    for line in sys.stdin:
        try:
            envelope = json.loads(line)
            if not isinstance(envelope, dict):
                raise ValueError("信封不是对象")
            request = load_turn(envelope)
            x, y = choose_move(request)
        except (json.JSONDecodeError, TypeError, ValueError):
            x, y = -1, -1
        print(json.dumps({"response": {"x": x, "y": y}}, separators=(",", ":")), flush=True)
        if first_response:
            print(KEEP_RUNNING, flush=True)
            first_response = False


if __name__ == "__main__":
    main()
```
