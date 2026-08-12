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
8. 双方各有固定 **900 秒（15 分钟）累计棋钟**，换回合不会重置。

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

## 棋钟与回放

平台只在某一方实际思考时扣减该方累计预算。成功决策产生 `time_used`，耗尽产生
`time_out`。棋钟信息只用于页面展示和回放，不会混入 Bot stdin 请求。
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
返回合法响应。这个短时预检不属于正式比赛，也不会替代或扣减双方各 900 秒累计棋钟。

## 锦标赛流程

点格棋赛事按“草稿 → 开放报名 → 发布排期 → 开赛 → 阶段休息（模板包含时）→ 已结束”推进，
每场仍使用固定 N=6 与双方各 900 秒累计棋钟，胜 / 平 / 负按 **2 / 1 / 0** 计分。内置模板
包括分组双循环后单败，以及瑞士轮后单败。

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
