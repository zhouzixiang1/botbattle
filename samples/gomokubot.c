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
#define BLACK5_CANDIDATE_COUNT 2
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

static void emit_candidates(void) {
    fputs("{\"response\":{\"action\":\"black5_candidates\",\"points\":[", stdout);
    for (int index = 0; index < BLACK5_CANDIDATE_COUNT; index++) {
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
        emit_candidates();
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
