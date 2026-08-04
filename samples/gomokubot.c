/* 五子棋随机空点 bot — Botzone 标准协议（信封）。
 * 请求信封: {"requests":[...]} 或 {"request":{"x":int,"y":int,"me":0|1}}
 *   - 黑方(me=0)首手 x=y=-1；之后 x,y 为对方上一手
 * 响应信封: {"response":{"x":int,"y":int}}
 * 策略：把对方上一手落到本地棋盘，随机选一个空点返回。
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define SIZE 15
static int board[SIZE][SIZE]; /* 0=空 1=黑(me0) 2=白(me1) */

/* 从 JSON 行中取 "key":number 的整数值（极简解析，足够本协议）。 */
static int field_int(const char *s, const char *key) {
    char pat[32];
    snprintf(pat, sizeof(pat), "\"%s\"", key);
    const char *p = strstr(s, pat);
    if (!p) return -1;
    p = strchr(p + strlen(pat), ':');
    if (!p) return -1;
    return atoi(p + 1);
}

int main(void) {
    srand((unsigned)time(NULL));
    char *line = NULL;
    size_t cap = 0;
    ssize_t n;
    while ((n = getline(&line, &cap, stdin)) != -1) {
        while (n > 0 && (line[n - 1] == '\n' || line[n - 1] == '\r'))
            line[--n] = 0;
        if (n <= 0) continue;

        int me = field_int(line, "me");
        if (me < 0) me = 0;
        int my = (me == 0) ? 1 : 2;          /* 我的颜色 */
        int opp = (my == 1) ? 2 : 1;         /* 对方颜色 */

        /* 落对方上一手 */
        int ox = field_int(line, "x");
        int oy = field_int(line, "y");
        if (ox >= 0 && oy >= 0 && ox < SIZE && oy < SIZE && board[ox][oy] == 0)
            board[ox][oy] = opp;

        /* 随机选一个空点 */
        int xs[SIZE * SIZE], ys[SIZE * SIZE], ec = 0;
        for (int x = 0; x < SIZE; x++)
            for (int y = 0; y < SIZE; y++)
                if (board[x][y] == 0) { xs[ec] = x; ys[ec] = y; ec++; }
        if (ec == 0) {
            fputs("{\"response\":{\"x\":-1,\"y\":-1}}\n", stdout);
            fflush(stdout);
            continue;
        }
        int idx = rand() % ec;
        int x = xs[idx], y = ys[idx];
        board[x][y] = my;
        printf("{\"response\":{\"x\":%d,\"y\":%d}}\n", x, y);
        fflush(stdout);
    }
    free(line);
    return 0;
}

