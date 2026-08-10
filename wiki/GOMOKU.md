# 五子棋

本平台 `game_id`：`gomoku`。棋盘与胜负规则固定。

## 规则

- 棋盘固定 **15×15**，坐标范围 `0..14`，原点左上，先 `x` 后 `y`。
- 座位 0 为黑方并先手，座位 1 为白方。
- 横、竖或两条斜线任一方向连续不少于 5 子即胜；长连同样算胜。
- 无禁手。
- 棋盘填满且无人连五为平局。
- 格式正确但越界或落在已占位置，由裁判判负；信封/response 错误或 Bot 超时属于
  平台技术负。

对局结果和回放会保留裁判的终局原因：`five` 表示连五、`draw` 表示棋盘填满；这两种是
正常判定，页面用中性文案展示。`illegal`（非法落子）、`error`（决策异常）和 `crash`
（Bot 运行异常）表示异常判罚，页面会明确警示。对局行的 `completed` 只表示裁判已经完成结算，
具体原因是附加说明，不会把正常完成误显示成取消，也不会直接显示内部英文原因码。网页展示座位
从 1 开始；本页协议中的 `me`、胜者和落子方仍按 0/1 编号。

## 通信 payload

通信必须使用[统一信封](#/wiki?slug=protocol)。请求 payload：

```json
{"x":7,"y":7,"me":1}
```

- `x`,`y` 是对手最近一手；黑方首回合为 `-1,-1`。
- `me` 为本方座位，`0` 黑、`1` 白。

响应必须是完整对象：

```json
{"response":{"x":7,"y":8}}
```

顶层裸 `{x,y}` 不合法。Traditional 必须重放全部 `requests[]/responses[]`；
LongRunning 首响应后必须精确握手，后续按单 request 增量维护棋盘。

## 快速开始

下面两个程序都能从完整历史重建棋盘，也能在 LongRunning 模式中增量维护棋盘。上传文件
必须是 Linux x86_64 ELF；Windows、Linux、macOS 的构建命令见
[Bot 开发指南](#/wiki?slug=bot-dev)。实现自己的策略时请保留这些状态处理要点：

- Traditional 每次从完整 `requests[]/responses[]` 重建 15×15 棋盘；
- LongRunning 在首回合重建棋盘并握手，后续把每个增量 request 落入内存棋盘；
- 选择响应点前同时排除对手历史着法与自己过去的 response；
- 策略随机也不能返回已占点、越界点或非整数坐标。

### 完整 C 示例

把以下内容保存为 `bot.c`，再按 [Bot 开发指南](#/wiki?slug=bot-dev)中你的操作系统对应
的 C 命令构建。

<!-- SAMPLE:gomoku:c -->
```c
/* 五子棋随机空点 Bot — 平台唯一 JSON 信封协议。
 * 请求信封: {"requests":[...]} 或 {"request":{"x":int,"y":int,"me":0|1}}
 *   - 黑方(me=0)首手 x=y=-1；之后 x,y 为对方上一手
 * 响应信封: {"response":{"x":int,"y":int}}
 * Traditional 从完整 requests/responses 重建占用；LongRunning 首响应后严格握手，
 * 后续按单 request 增量维护。两种模式可使用同一个二进制。
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define SIZE 15
#define KEEP_RUNNING ">>>BOTZONE_REQUEST_KEEP_RUNNING<<<"
static int board[SIZE][SIZE]; /* 0=空，1=已占 */

static int number_after(const char *s, const char *key, int def) {
    char pat[32];
    snprintf(pat, sizeof(pat), "\"%s\"", key);
    const char *p = strstr(s, pat);
    if (!p) return def;
    p = strchr(p + strlen(pat), ':');
    if (!p) return def;
    return atoi(p + 1);
}

static void reset_board(void) {
    memset(board, 0, sizeof(board));
}

/* 标准历史信封中的 requests[] / responses[] 都只用 x/y 表示落子；
 * 扫描全部坐标即可恢复占用状态，首手 -1,-1 会自然跳过。 */
static void replay_all_moves(const char *line) {
    const char *p = line;
    while ((p = strstr(p, "\"x\"")) != NULL) {
        int x = number_after(p, "x", -1);
        int y = number_after(p, "y", -1);
        if (x >= 0 && x < SIZE && y >= 0 && y < SIZE)
            board[x][y] = 1;
        p += 3;
    }
}

int main(void) {
    srand((unsigned)time(NULL));
    reset_board();
    int first_response = 1;
    char *line = NULL;
    size_t cap = 0;
    ssize_t n;
    while ((n = getline(&line, &cap, stdin)) != -1) {
        while (n > 0 && (line[n - 1] == '\n' || line[n - 1] == '\r'))
            line[--n] = 0;
        if (n <= 0) continue;

        if (strstr(line, "\"requests\"") != NULL) reset_board();
        replay_all_moves(line);

        /* 随机选一个空点 */
        int xs[SIZE * SIZE], ys[SIZE * SIZE], ec = 0;
        for (int x = 0; x < SIZE; x++)
            for (int y = 0; y < SIZE; y++)
                if (board[x][y] == 0) { xs[ec] = x; ys[ec] = y; ec++; }
        if (ec == 0) {
            fputs("{\"response\":{\"x\":-1,\"y\":-1}}\n", stdout);
        } else {
            int idx = rand() % ec;
            int x = xs[idx], y = ys[idx];
            board[x][y] = 1;
            printf("{\"response\":{\"x\":%d,\"y\":%d}}\n", x, y);
        }
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

### 完整 Python 示例

把以下内容保存为 `bot.py`，再按开发指南使用 Linux amd64
`python:3.12-bookworm` 容器中的 PyInstaller 打包；不要上传源文件本身。

<!-- SAMPLE:gomoku:python -->
```python
#!/usr/bin/env python3
"""五子棋随机空点样例 Bot（平台唯一 JSON 信封协议）。

Traditional 使用完整历史；LongRunning 首回合使用完整历史并严格握手，之后使用单 request。
请求负载：{x,y,me}；响应信封：{"response": {"x":.., "y":..}}。
"""
from __future__ import annotations

import json
import random
import sys

SIZE = 15
KEEP_RUNNING = ">>>BOTZONE_REQUEST_KEEP_RUNNING<<<"


def make_board() -> list[list[int]]:
    return [[-1] * SIZE for _ in range(SIZE)]


board = make_board()


def place(x: int, y: int, p: int) -> None:
    if 0 <= x < SIZE and 0 <= y < SIZE:
        board[x][y] = p


def _mark_payload(move: object, player: int) -> None:
    if not isinstance(move, dict):
        raise ValueError("落子 payload 不是对象")
    x, y = int(move.get("x", -1)), int(move.get("y", -1))
    if 0 <= x < SIZE and 0 <= y < SIZE:
        place(x, y, player)


def load_turn(envelope: dict) -> dict:
    """按所选运行模式的标准信封更新棋盘并返回当前 request。"""
    global board
    requests = envelope.get("requests")
    if isinstance(requests, list):
        if not requests or not isinstance(requests[-1], dict):
            raise ValueError("完整历史信封缺少当前 request")
        board = make_board()
        me = int(requests[-1].get("me", 0))
        for request in requests:
            _mark_payload(request, 1 - me)
        responses = envelope.get("responses")
        if not isinstance(responses, list):
            raise ValueError("完整历史信封缺少 responses")
        for response in responses:
            _mark_payload(response, me)
        return requests[-1]

    request = envelope.get("request")
    if not isinstance(request, dict):
        raise ValueError("增量信封缺少 request")
    me = int(request.get("me", 0))
    _mark_payload(request, 1 - me)
    return request


def main() -> None:
    first_response = True
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            env = json.loads(line)
            if not isinstance(env, dict):
                raise ValueError("信封不是对象")
            req = load_turn(env)
        except (json.JSONDecodeError, TypeError, ValueError):
            print(json.dumps({"response": {"x": -1, "y": -1}}), flush=True)
        else:
            me = int(req.get("me", 0))
            empties = [
                (x, y)
                for x in range(SIZE)
                for y in range(SIZE)
                if board[x][y] < 0
            ]
            if not empties:
                x, y = -1, -1
            else:
                x, y = random.choice(empties)
                place(x, y, me)
            print(json.dumps({"response": {"x": x, "y": y}}), flush=True)
        if first_response:
            print(KEEP_RUNNING, flush=True)
            first_response = False


if __name__ == "__main__":
    main()
```
