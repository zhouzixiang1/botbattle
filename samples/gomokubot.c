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
