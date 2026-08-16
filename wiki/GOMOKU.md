# 五子棋

本平台 `game_id` 为 `gomoku`，当前规则代际为
`gomoku_ccgc_2013_v1`，Bot 协议为 `gomoku_action_v2`。这一代以
《中国五子棋竞赛规则—2013》的指定开局棋规和《2025 全国机器博弈竞赛程序册》
的五子棋项目约束为依据。

规则来源的页码口径如下：

- 《中国五子棋竞赛规则—2013》第 2–14 页规定棋盘、术语、指定开局、三手交换、
  五手 N 打和黑方禁手，第 20–26 页规定用时、胜负与和棋；
- 《2025 全国机器博弈竞赛程序册》印刷第 6–7 页规定本届赛制、2/1/0 计分和单方
  包干 15 分钟，印刷第 65–68 页重申五子棋项目规则。程序册 PDF 中对应物理页为
  15–16、74–77。

程序册的 45 队分组结构属于 2025 届赛事安排，不会被写进通用单局裁判；线下的
棋钟操作、申诉和“由对手指出禁手”等程序，也由平台的累计棋钟与确定性数字裁判替代。

## 单局规则

- 棋盘固定 **15×15**。线协议坐标为 `x/y=0..14`，原点在网页棋盘左上角；
  这只是数字传输表示，不改变竞赛棋盘的 A–O / 1–15 几何位置。
- **座位不等于棋色**。座位 0 先提交指定开局；座位 1 在第三子后决定是否交换，
  交换后座位 1 执黑、座位 0 执白。请求中的 `me` 始终是座位，`color` 才是当前棋色。
- 黑1固定在天元 H8（内部 `(7,7)`）；开局方同时提交相邻的白2、中心 5×5
  范围内的黑3，对称归一后必须属于直指/斜指各 13 种的 **26 种指定开局**。
- 开局方同时声明 `N=2..5`。三手交换后，最终白方落白4，最终黑方提交正好 N 个
  **互不重复、均为空点且不同形**的黑5候选点，最终白方只保留其中一点作为真实黑5。“不同形”按当前黑白四子局面仍保持不变的旋转/镜像对称判定；未选候选点不进入棋盘。
- 黑方落子若形成恰好五连，立即获胜；否则黑方长连、三三或四四是禁手，由数字裁判
  自动判黑方负。“恰好五连与其他禁手同时形成”时以五连胜优先。
- 白方在横、竖或两条斜线任一方向连续不少于五子即胜，白方长连也胜。
- 前五子必须按开局流程完成，不能 PASS。此后可选择 PASS；两方连续 PASS 则和棋。
  棋盘填满且没有胜者也是和棋。
- 每个参赛座位有 **900 秒累计棋钟**；开局、交换、候选与普通行棋都计入同一侧总时间。
- 越界、重复占位、阶段不匹配或候选数不等于 N 等规则非法动作由裁判判负；
  信封/响应格式错误、Bot 崩溃或棋钟耗尽是平台技术终局。

平台会在对局、回放和时间线中显示指定开局、是否交换、N 打候选与选择、棋色、
PASS 以及禁手类型。线下规则中的“由对手指出/裁判确认”在平台上改为自动、确定性判定，
不允许 Bot 选择接受非法着。

## v2 通信动作

通信必须使用[统一信封](#/wiki?slug=protocol)。每个请求都自包含
`protocol_version=2`、`ruleset`、`phase`、`me`、`color`、`seat_colors`、
`board`与 `pass_allowed`；特殊阶段还会带 `n`、`candidates`、`last` 等字段。

Bot 须根据 `phase` 返回下列标准信封之一：

```json
{"response":{"action":"opening","white2":{"x":7,"y":8},"black3":{"x":8,"y":7},"n":2}}
{"response":{"action":"swap","swap":false}}
{"response":{"action":"move","x":6,"y":8}}
{"response":{"action":"black5_candidates","points":[{"x":6,"y":7},{"x":8,"y":8}]}}
{"response":{"action":"black5_select","index":0}}
{"response":{"action":"pass"}}
```

字段必须与动作类型精确匹配，顶层裸动作、旧 `{x,y}` 响应和缺少 `action` 均不合法。
Traditional 每次从完整 `requests[]/responses[]` 恢复状态；LongRunning 在首次响应后完成精确握手，
后续处理单个 `request`。

> 规则升级采用硬切换：旧 `gomoku_xy_v1` 二进制版本已废弃，不能回滚、选用、排队或参赛。
> 平台为已有五子棋 Bot 建立一个通过 v2 完整对局验收的新标准版本并设为当前版本；
> 旧文件和版本元数据只作历史审计保留。新规则使用独立评分池，所有 Bot 从默认评分、
> 0 场开始；旧规则评分只随历史对局归档展示，不会混入新排行榜或日后的评分重建。

硬切换会撤销已有五子棋本地接入令牌。使用本地 Bot 的玩家须先把本机程序更新为 v2
动作协议，再在“我的 Bot”中重新创建接入；原显示名称可以复用。旧令牌和旧 `{x,y}`
程序不会被自动转换。

## 锦标赛流程

五子棋赛事按“草稿 → 开放报名 → 发布排期 → 开赛 → 阶段休息（模板包含时）→ 已结束”推进，
新建的每场对局都冻结 `gomoku_ccgc_2013_v1`，胜 / 平 / 负按 **2 / 1 / 0** 计分。
《2025 全国机器博弈竞赛程序册》中 45 队的“9 个 5 人组双循环 → 3 个 6 人组双循环 →
JA/JB/JC 各 3 人双循环”是该届赛事编排，不作为任意人数赛事的单局规则。
平台当前只对新建五子棋赛事提供 **五子棋双循环** 内置模板：所有 Bot 两两比赛两次并
交换“开局提案方 / 交换决策方”，全部对局完成后按 2 / 1 / 0 积分固化名次。

历史的“分组双循环 → 单败”和“瑞士 → 单败”模板仍可用于展示旧赛事与只读演示快照，
但已停用新建，也不会出现在新建赛事的模板列表中。原因是五子棋规则允许连续 PASS 或满盘和棋，
而这两个历史模板没有声明重赛、加赛或其他破和规则。平台不会擅自用座位、种子或分差指定晋级者：
历史或自定义单败阶段出现 `winner=null` 时，赛事保持运行中并显式阻塞，不会错误晋级、快照或完赛。

发布排期会冻结该轮 Bot 版本。完赛正式榜对同积分选手展示实际破同分字段，不用前端
行号伪造名次依据。完整生命周期、时间和全局排队规则见[平台功能指南](#/wiki?slug=guide)。

## 快速开始

下面两个程序均理解全部 v2 阶段，能从完整历史恢复状态，也能在 LongRunning 模式中增量运行。上传文件
必须是 Linux x86_64 ELF；Windows、Linux、macOS 的构建命令见
[Bot 开发指南](#/wiki?slug=bot-dev)。实现自己的策略时请保留这些状态处理要点：

- 必须根据 `phase`而不是假定“每回合都落一子”；
- `me` 不会因交换改变，棋色以 `color` / `seat_colors` 为准；
- 黑5候选必须正好提交请求中的 N 个互不重复、均为空且不同形的点，选择阶段只返回有效索引；
- 黑方普通行棋要避免长连、三三和四四；策略即使是随机的，也不能返回已占点或越界点。

### 完整 C 示例

把以下内容保存为 `bot.c`，再按 [Bot 开发指南](#/wiki?slug=bot-dev)中你的操作系统对应
的 C 命令构建。

<!-- SAMPLE:gomoku:c -->
```c
/* 全国机器博弈竞赛五子棋 v2 确定性样例 Bot。
 *
 * 当前 request 自带完整 15x15 棋盘；Traditional 取 requests[] 中最后一个
 * request，LongRunning 取 request。响应覆盖 opening / swap / move /
 * black5_candidates / black5_select / pass，并始终使用标准 response 信封。
 */
#define _GNU_SOURCE
#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define SIZE 15
#define CELLS (SIZE * SIZE)
#define EMPTY (-1)
#define BLACK 0
#define WHITE 1
#define KEEP_RUNNING ">>>BOTZONE_REQUEST_KEEP_RUNNING<<<"

static int board[SIZE][SIZE];

static const char *last_occurrence(const char *text, const char *needle) {
    const char *found = NULL;
    const char *cursor = text;
    while ((cursor = strstr(cursor, needle)) != NULL) {
        found = cursor;
        cursor += strlen(needle);
    }
    return found;
}

static const char *current_request(const char *line) {
    /* protocol_version 只出现在 request；最后一次即 Traditional 当前回合。 */
    const char *request = last_occurrence(line, "\"protocol_version\"");
    return request ? request : last_occurrence(line, "\"phase\"");
}

static int number_after(const char *text, const char *key, int fallback) {
    char pattern[40];
    snprintf(pattern, sizeof(pattern), "\"%s\"", key);
    const char *cursor = strstr(text, pattern);
    if (!cursor) return fallback;
    cursor = strchr(cursor + strlen(pattern), ':');
    if (!cursor) return fallback;
    cursor++;
    while (isspace((unsigned char)*cursor)) cursor++;
    if (*cursor != '-' && !isdigit((unsigned char)*cursor)) return fallback;
    return (int)strtol(cursor, NULL, 10);
}

static int string_after(
    const char *text, const char *key, char *output, size_t output_size
) {
    char pattern[40];
    snprintf(pattern, sizeof(pattern), "\"%s\"", key);
    const char *cursor = strstr(text, pattern);
    if (!cursor) return 0;
    cursor = strchr(cursor + strlen(pattern), ':');
    if (!cursor) return 0;
    cursor++;
    while (isspace((unsigned char)*cursor)) cursor++;
    if (*cursor++ != '"') return 0;
    const char *end = strchr(cursor, '"');
    if (!end || (size_t)(end - cursor) >= output_size) return 0;
    memcpy(output, cursor, (size_t)(end - cursor));
    output[end - cursor] = '\0';
    return 1;
}

static int parse_board(const char *request) {
    for (int x = 0; x < SIZE; x++)
        for (int y = 0; y < SIZE; y++) board[x][y] = EMPTY;

    const char *cursor = strstr(request, "\"board\"");
    if (!cursor || !(cursor = strchr(cursor, '['))) return 0;
    int count = 0;
    while (*cursor && count < CELLS) {
        if (*cursor == '-' || isdigit((unsigned char)*cursor)) {
            char *end = NULL;
            long value = strtol(cursor, &end, 10);
            if (end == cursor || value < EMPTY || value > WHITE) return 0;
            board[count / SIZE][count % SIZE] = (int)value;
            count++;
            cursor = end;
        } else {
            cursor++;
        }
    }
    return count == CELLS;
}

static int empty_at(int x, int y) {
    return x >= 0 && x < SIZE && y >= 0 && y < SIZE && board[x][y] == EMPTY;
}

static int take_if_empty(int x, int y, int *out_x, int *out_y) {
    if (!empty_at(x, y)) return 0;
    *out_x = x;
    *out_y = y;
    return 1;
}

static int choose_white_move(int *out_x, int *out_y) {
    for (int y = 2; y <= 6; y++)
        if (take_if_empty(2, y, out_x, out_y)) return 1;
    for (int x = 0; x < SIZE; x++)
        for (int y = 0; y < SIZE; y++)
            if (take_if_empty(x, y, out_x, out_y)) return 1;
    return 0;
}

static int black_move_is_conservatively_safe(int x, int y) {
    static const int directions[4][2] = {{1, 0}, {0, 1}, {1, 1}, {1, -1}};
    if (!empty_at(x, y)) return 0;
    for (int d = 0; d < 4; d++) {
        for (int step = -4; step <= 4; step++) {
            if (step == 0) continue;
            int cx = x + step * directions[d][0];
            int cy = y + step * directions[d][1];
            if (cx >= 0 && cx < SIZE && cy >= 0 && cy < SIZE
                    && board[cx][cy] == BLACK)
                return 0;
        }
    }
    return 1;
}

static int choose_safe_black_move(int *out_x, int *out_y) {
    /* 97 与 225 互质：固定序列不重复地检查全盘。 */
    for (int index = 0; index < CELLS; index++) {
        int position = (CELLS - 1 - index * 97) % CELLS;
        if (position < 0) position += CELLS;
        int x = position / SIZE;
        int y = position % SIZE;
        if (black_move_is_conservatively_safe(x, y)) {
            *out_x = x;
            *out_y = y;
            return 1;
        }
    }
    return 0;
}

static int choose_candidate(int index, int *out_x, int *out_y) {
    static const int preferred[][2] = {
        {0, 0}, {14, 14}, {0, 14}, {14, 0}, {1, 13}
    };
    int wanted = index;
    for (size_t i = 0; i < sizeof(preferred) / sizeof(preferred[0]); i++) {
        int x = preferred[i][0], y = preferred[i][1];
        if (!empty_at(x, y)) continue;
        if (wanted-- == 0) {
            *out_x = x;
            *out_y = y;
            return 1;
        }
    }
    for (int x = 0; x < SIZE; x++) {
        for (int y = 0; y < SIZE; y++) {
            if (!empty_at(x, y)) continue;
            int already_preferred = 0;
            for (size_t i = 0; i < sizeof(preferred) / sizeof(preferred[0]); i++)
                if (preferred[i][0] == x && preferred[i][1] == y)
                    already_preferred = 1;
            if (already_preferred) continue;
            if (wanted-- == 0) {
                *out_x = x;
                *out_y = y;
                return 1;
            }
        }
    }
    return 0;
}

static void emit_candidates(int count) {
    if (count < 2 || count > 5) count = 2;
    fputs("{\"response\":{\"action\":\"black5_candidates\",\"points\":[", stdout);
    for (int index = 0; index < count; index++) {
        int x = -99, y = -99;
        if (!choose_candidate(index, &x, &y)) {
            x = -99;
            y = -99;
        }
        if (index) fputc(',', stdout);
        printf("{\"x\":%d,\"y\":%d}", x, y);
    }
    fputs("]}}\n", stdout);
}

static void respond(const char *request) {
    char phase[40];
    if (!request || !string_after(request, "phase", phase, sizeof(phase))
            || !parse_board(request)) {
        fputs("{\"response\":{\"action\":\"move\",\"x\":-99,\"y\":-99}}\n", stdout);
        return;
    }

    if (strcmp(phase, "opening_proposal") == 0) {
        fputs("{\"response\":{\"action\":\"opening\",\"white2\":{\"x\":7,\"y\":8},"
              "\"black3\":{\"x\":8,\"y\":8},\"n\":2}}\n", stdout);
    } else if (strcmp(phase, "swap_choice") == 0) {
        fputs("{\"response\":{\"action\":\"swap\",\"swap\":false}}\n", stdout);
    } else if (strcmp(phase, "black5_candidates") == 0) {
        emit_candidates(number_after(request, "n", 2));
    } else if (strcmp(phase, "black5_select") == 0) {
        fputs("{\"response\":{\"action\":\"black5_select\",\"index\":0}}\n", stdout);
    } else if (strcmp(phase, "white4") == 0) {
        int x = -99, y = -99;
        choose_white_move(&x, &y);
        printf("{\"response\":{\"action\":\"move\",\"x\":%d,\"y\":%d}}\n", x, y);
    } else if (strcmp(phase, "normal_play") == 0) {
        int x = -99, y = -99;
        int color = number_after(request, "color", WHITE);
        int found = color == BLACK
            ? choose_safe_black_move(&x, &y)
            : choose_white_move(&x, &y);
        if (found)
            printf("{\"response\":{\"action\":\"move\",\"x\":%d,\"y\":%d}}\n", x, y);
        else
            fputs("{\"response\":{\"action\":\"pass\"}}\n", stdout);
    } else {
        fputs("{\"response\":{\"action\":\"move\",\"x\":-99,\"y\":-99}}\n", stdout);
    }
}

int main(void) {
    int first_response = 1;
    char *line = NULL;
    size_t capacity = 0;
    ssize_t length;
    while ((length = getline(&line, &capacity, stdin)) != -1) {
        while (length > 0 && (line[length - 1] == '\n' || line[length - 1] == '\r'))
            line[--length] = '\0';
        if (length <= 0) continue;
        respond(current_request(line));
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
"""全国机器博弈竞赛五子棋 v2 确定性样例 Bot。

Traditional 读取 ``requests[-1]``，LongRunning 读取 ``request``；两种模式
收到的当前请求都带完整棋盘。Bot 覆盖指定开局、三手交换、白4、N 打、
候选选择和正常行棋，并始终使用平台标准 ``response`` 信封。
"""
from __future__ import annotations

import json
import sys
from typing import Any, Iterable

SIZE = 15
EMPTY = -1
BLACK = 0
WHITE = 1
KEEP_RUNNING = ">>>BOTZONE_REQUEST_KEEP_RUNNING<<<"

PHASE_OPENING = "opening_proposal"
PHASE_SWAP = "swap_choice"
PHASE_WHITE4 = "white4"
PHASE_BLACK5_CANDIDATES = "black5_candidates"
PHASE_BLACK5_SELECT = "black5_select"
PHASE_NORMAL = "normal_play"

_DIRECTIONS = ((1, 0), (0, 1), (1, 1), (1, -1))
_WHITE_PLAN = tuple((2, y) for y in range(2, 7))
_CANDIDATE_PLAN = ((0, 0), (14, 14), (0, 14), (14, 0), (1, 13))


def _current_request(envelope: dict[str, Any]) -> dict[str, Any]:
    requests = envelope.get("requests")
    if isinstance(requests, list):
        if not requests or not isinstance(requests[-1], dict):
            raise ValueError("Traditional 信封缺少当前 request")
        return requests[-1]
    request = envelope.get("request")
    if not isinstance(request, dict):
        raise ValueError("LongRunning 信封缺少 request")
    return request


def _board_from(request: dict[str, Any]) -> list[list[int]]:
    raw = request.get("board")
    if not isinstance(raw, list) or len(raw) != SIZE:
        raise ValueError("board 必须是 15 列")
    board: list[list[int]] = []
    for column in raw:
        if not isinstance(column, list) or len(column) != SIZE:
            raise ValueError("board 每列必须有 15 个交叉点")
        normalized: list[int] = []
        for cell in column:
            if isinstance(cell, bool) or not isinstance(cell, int) or cell not in {
                EMPTY,
                BLACK,
                WHITE,
            }:
                raise ValueError("board 只允许 -1/0/1")
            normalized.append(cell)
        board.append(normalized)
    return board


def _all_empty(board: list[list[int]]) -> Iterable[tuple[int, int]]:
    for x in range(SIZE):
        for y in range(SIZE):
            if board[x][y] == EMPTY:
                yield x, y


def _first_empty(
    board: list[list[int]], preferred: Iterable[tuple[int, int]] = ()
) -> tuple[int, int] | None:
    seen: set[tuple[int, int]] = set()
    for x, y in (*tuple(preferred), *_all_empty(board)):
        if (x, y) in seen:
            continue
        seen.add((x, y))
        if 0 <= x < SIZE and 0 <= y < SIZE and board[x][y] == EMPTY:
            return x, y
    return None


def _black_move_is_conservatively_safe(
    board: list[list[int]], x: int, y: int
) -> bool:
    """只选不可能由该手形成三、四、五或长连的黑点。

    所有禁手和五连都必须在包含新子的五格窗口（长连则包含相邻连续子）
    中出现。四条线上距新点四步内完全没有其他黑子，是容易独立审计的
    充分安全条件；条件过严时 Bot 可以按规则 PASS。
    """

    if not (0 <= x < SIZE and 0 <= y < SIZE) or board[x][y] != EMPTY:
        return False
    for dx, dy in _DIRECTIONS:
        for step in range(-4, 5):
            if step == 0:
                continue
            cx, cy = x + step * dx, y + step * dy
            if 0 <= cx < SIZE and 0 <= cy < SIZE and board[cx][cy] == BLACK:
                return False
    return True


def _safe_black_move(board: list[list[int]]) -> tuple[int, int] | None:
    # 97 与 225 互质，因此这一固定序列会且只会检查每个交叉点一次。
    for index in range(SIZE * SIZE):
        position = (SIZE * SIZE - 1 - index * 97) % (SIZE * SIZE)
        x, y = divmod(position, SIZE)
        if _black_move_is_conservatively_safe(board, x, y):
            return x, y
    return None


def _candidate_points(
    board: list[list[int]], count: int
) -> list[dict[str, int]]:
    points: list[dict[str, int]] = []
    reserved: set[tuple[int, int]] = set()
    for point in (*_CANDIDATE_PLAN, *_all_empty(board)):
        x, y = point
        if point in reserved or board[x][y] != EMPTY:
            continue
        reserved.add(point)
        points.append({"x": x, "y": y})
        if len(points) == count:
            return points
    raise ValueError("棋盘没有足够的黑5候选点")


def _respond(request: dict[str, Any]) -> dict[str, Any]:
    phase = request.get("phase")
    board = _board_from(request)

    if phase == PHASE_OPENING:
        # 黑1由裁判固定在 H8；白2相邻，黑3位于中心 5x5，对应合法指定开局。
        return {
            "action": "opening",
            "white2": {"x": 7, "y": 8},
            "black3": {"x": 8, "y": 8},
            "n": 2,
        }
    if phase == PHASE_SWAP:
        return {"action": "swap", "swap": False}
    if phase == PHASE_WHITE4:
        point = _first_empty(board, _WHITE_PLAN)
        if point is None:
            raise ValueError("白4无空点")
        return {"action": "move", "x": point[0], "y": point[1]}
    if phase == PHASE_BLACK5_CANDIDATES:
        count = request.get("n")
        if isinstance(count, bool) or not isinstance(count, int) or not 2 <= count <= 5:
            raise ValueError("n 必须是 2..5")
        return {"action": "black5_candidates", "points": _candidate_points(board, count)}
    if phase == PHASE_BLACK5_SELECT:
        candidates = request.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise ValueError("缺少黑5候选点")
        return {"action": "black5_select", "index": 0}
    if phase == PHASE_NORMAL:
        color = request.get("color")
        point = (
            _safe_black_move(board)
            if color == BLACK
            else _first_empty(board, _WHITE_PLAN)
        )
        if point is None:
            return {"action": "pass"}
        return {"action": "move", "x": point[0], "y": point[1]}
    raise ValueError(f"未知 phase: {phase!r}")


def main() -> None:
    first_response = True
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            envelope = json.loads(line)
            if not isinstance(envelope, dict):
                raise ValueError("信封不是对象")
            response = _respond(_current_request(envelope))
        except (json.JSONDecodeError, TypeError, ValueError):
            response = {"action": "move", "x": -99, "y": -99}
        print(json.dumps({"response": response}, separators=(",", ":")), flush=True)
        if first_response:
            print(KEEP_RUNNING, flush=True)
            first_response = False


if __name__ == "__main__":
    main()
```
