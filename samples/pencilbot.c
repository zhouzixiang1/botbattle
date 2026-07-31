/* 点格棋随机占边 bot — 长驻 JSON 行协议（对齐 board_protocol）
 * 请求: {"v":1,"t":"mv","x":int,"y":int,"pass":0|1,"me":0|1,"scores":[r,b]}
 *   - 红方(me=0)首手 x=y=-1,pass=0；之后 x,y 为对方上一手
 *   - pass=1：对方得分连走，须响应 {"x":-1,"y":-1}
 * 响应: {"x":int,"y":int}
 * 交错网格 size=2N-1：偶偶=点(3) 奇偶/偶奇=边(4) 奇奇=格心(2)；占边后置 5。
 * 策略：照对方上一手占边，再随机占一条合法边。
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define N 11
#define SIZE (2 * N - 1)           /* 21 */
#define DOT 3
#define EDGE 4
#define EDGE_USED 5
#define BOX 2

static int g[SIZE][SIZE];

static void init_board(void) {
    for (int x = 0; x < SIZE; x++)
        for (int y = 0; y < SIZE; y++) {
            if (x % 2 == 0 && y % 2 == 0) g[x][y] = DOT;
            else if ((x + y) % 2 == 1)    g[x][y] = EDGE;
            else                          g[x][y] = BOX;
        }
}

static int in_board(int x, int y) { return x >= 0 && x < SIZE && y >= 0 && y < SIZE; }

static int is_legal_edge(int x, int y) {
    return in_board(x, y) && g[x][y] == EDGE;
}

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
    init_board();
    char *line = NULL;
    size_t cap = 0;
    ssize_t n;
    while ((n = getline(&line, &cap, stdin)) != -1) {
        while (n > 0 && (line[n - 1] == '\n' || line[n - 1] == '\r'))
            line[--n] = 0;
        if (n <= 0) continue;

        int pass = field_int(line, "pass");
        int ox = field_int(line, "x");
        int oy = field_int(line, "y");

        /* pass 回合：把对方那一步落到棋盘，然后回 (-1,-1) 把回合交还 */
        if (pass == 1) {
            if (is_legal_edge(ox, oy)) g[ox][oy] = EDGE_USED;
            fputs("{\"x\":-1,\"y\":-1}\n", stdout);
            fflush(stdout);
            continue;
        }

        /* 普通回合：落对方上一手 */
        if (is_legal_edge(ox, oy)) g[ox][oy] = EDGE_USED;

        /* 随机占一条合法边 */
        int xs[SIZE * SIZE], ys[SIZE * SIZE], ec = 0;
        for (int x = 0; x < SIZE; x++)
            for (int y = 0; y < SIZE; y++)
                if (g[x][y] == EDGE) { xs[ec] = x; ys[ec] = y; ec++; }
        if (ec == 0) {
            fputs("{\"x\":-1,\"y\":-1}\n", stdout);
            fflush(stdout);
            continue;
        }
        int idx = rand() % ec;
        int x = xs[idx], y = ys[idx];
        g[x][y] = EDGE_USED;
        printf("{\"x\":%d,\"y\":%d}\n", x, y);
        fflush(stdout);
    }
    free(line);
    return 0;
}
