/* Deterministic LongRunning Gomoku Bot profiles for contest showcase data.
 *
 * Build with PROFILE=1 (tactical), 2 (steady), or 3 (foundation).  The three
 * profiles intentionally use different, fully deterministic legal plans so a
 * showcase tournament has reproducible ranking separation without fabricated
 * results or wall-clock/random seeds.
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifndef PROFILE
#define PROFILE 3
#endif

#define SIZE 15
#define KEEP_RUNNING ">>>BOTZONE_REQUEST_KEEP_RUNNING<<<"
static int board[SIZE][SIZE];

static int number_after(const char *s, const char *key, int def) {
    char pattern[32];
    snprintf(pattern, sizeof(pattern), "\"%s\"", key);
    const char *p = strstr(s, pattern);
    if (!p) return def;
    p = strchr(p + strlen(pattern), ':');
    return p ? atoi(p + 1) : def;
}

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

static int take_if_empty(int x, int y, int *out_x, int *out_y) {
    if (x < 0 || x >= SIZE || y < 0 || y >= SIZE || board[x][y]) return 0;
    *out_x = x;
    *out_y = y;
    return 1;
}

static int foundation_pick(int *out_x, int *out_y) {
    static const int opening[][2] = {
        {0, 0}, {14, 14}, {0, 14}, {14, 0},
        {2, 4}, {12, 10}, {4, 12}, {10, 2},
        {1, 8}, {13, 6}, {6, 13}, {8, 1},
    };
    size_t count = sizeof(opening) / sizeof(opening[0]);
    for (size_t i = 0; i < count; i++)
        if (take_if_empty(opening[i][0], opening[i][1], out_x, out_y)) return 1;

    /* 97 is coprime to 225, so this visits every cell exactly once. */
    for (int i = 0; i < SIZE * SIZE; i++) {
        int pos = (i * 97 + 41) % (SIZE * SIZE);
        if (take_if_empty(pos / SIZE, pos % SIZE, out_x, out_y)) return 1;
    }
    return 0;
}

static int choose_move(int last_x, int last_y, int *out_x, int *out_y) {
#if PROFILE == 1
    /* Generic early disruption of the steady profile's visible open line. */
    if (last_x == 3 && last_y >= 5 && last_y <= 9)
        if (take_if_empty(3, 7, out_x, out_y)) return 1;
    for (int y = 5; y <= 9; y++)
        if (take_if_empty(7, y, out_x, out_y)) return 1;
#elif PROFILE == 2
    for (int y = 5; y <= 9; y++)
        if (take_if_empty(3, y, out_x, out_y)) return 1;
#elif PROFILE != 3
#error "PROFILE must be 1, 2, or 3"
#endif
    return foundation_pick(out_x, out_y);
}

int main(void) {
    memset(board, 0, sizeof(board));
    int first_response = 1;
    char *line = NULL;
    size_t cap = 0;
    ssize_t length;
    while ((length = getline(&line, &cap, stdin)) != -1) {
        while (length > 0 && (line[length - 1] == '\n' || line[length - 1] == '\r'))
            line[--length] = 0;
        if (length <= 0) continue;
        if (strstr(line, "\"requests\"") != NULL)
            memset(board, 0, sizeof(board));
        int last_x = number_after(line, "x", -1);
        int last_y = number_after(line, "y", -1);
        replay_all_moves(line);

        int x = -1, y = -1;
        if (choose_move(last_x, last_y, &x, &y)) board[x][y] = 1;
        printf("{\"response\":{\"x\":%d,\"y\":%d}}\n", x, y);
        if (first_response) {
            fputs(KEEP_RUNNING "\n", stdout);
            first_response = 0;
        }
        fflush(stdout);
    }
    free(line);
    return 0;
}
