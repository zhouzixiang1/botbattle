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
