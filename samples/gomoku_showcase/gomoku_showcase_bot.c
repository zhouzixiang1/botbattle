/* 全国机器博弈竞赛五子棋 v2 的确定性赛事演示 Bot。
 *
 * PROFILE=1 tactical、2 steady、3 foundation。三档均覆盖指定开局、
 * 三手交换、五手二打与 PASS，并仅从当前 request 的完整棋盘决策；没有随机数、
 * 时钟或进程内历史，因此 Traditional / LongRunning 的轨迹一致。
 */
#define _GNU_SOURCE
#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifndef PROFILE
#define PROFILE 3
#endif
#if PROFILE < 1 || PROFILE > 3
#error "PROFILE must be 1, 2, or 3"
#endif

#define SIZE 15
#define CELLS (SIZE * SIZE)
#define EMPTY (-1)
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
    const char *request = last_occurrence(line, "\"protocol_version\"");
    return request ? request : last_occurrence(line, "\"phase\"");
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
            if (end == cursor || value < -1 || value > 1) return 0;
            board[count / SIZE][count % SIZE] = (int)value;
            count++;
            cursor = end;
        } else {
            cursor++;
        }
    }
    return count == CELLS;
}

static int take_if_empty(int x, int y, int *out_x, int *out_y) {
    if (x < 0 || x >= SIZE || y < 0 || y >= SIZE || board[x][y] != EMPTY)
        return 0;
    *out_x = x;
    *out_y = y;
    return 1;
}

static int choose_profile_move(int *out_x, int *out_y) {
#if PROFILE == 1
    for (int y = 1; y <= 5; y++)
        if (take_if_empty(1, y, out_x, out_y)) return 1;
#elif PROFILE == 2
    for (int y = 1; y <= 5; y++)
        if (take_if_empty(3, y, out_x, out_y)) return 1;
#else
    (void)out_x;
    (void)out_y;
#endif
    return 0;
}

static int choose_white4(int *out_x, int *out_y) {
#if PROFILE == 1
    if (take_if_empty(1, 1, out_x, out_y)) return 1;
#elif PROFILE == 2
    if (take_if_empty(3, 1, out_x, out_y)) return 1;
#else
    if (take_if_empty(5, 1, out_x, out_y)) return 1;
#endif
    for (int x = 0; x < SIZE; x++)
        for (int y = 0; y < SIZE; y++)
            if (take_if_empty(x, y, out_x, out_y)) return 1;
    return 0;
}

static void candidate_preference(int index, int *x, int *y) {
#if PROFILE == 1
    static const int points[5][2] = {{3, 2}, {13, 13}, {0, 14}, {14, 0}, {0, 0}};
#elif PROFILE == 2
    static const int points[5][2] = {{5, 2}, {13, 13}, {0, 14}, {14, 0}, {0, 0}};
#else
    static const int points[5][2] = {{13, 13}, {0, 0}, {0, 14}, {14, 0}, {1, 13}};
#endif
    *x = points[index][0];
    *y = points[index][1];
}

static int choose_candidate(int index, int *out_x, int *out_y) {
    int wanted = index;
    for (int i = 0; i < 5; i++) {
        int x, y;
        candidate_preference(i, &x, &y);
        if (board[x][y] != EMPTY) continue;
        if (wanted-- == 0) {
            *out_x = x;
            *out_y = y;
            return 1;
        }
    }
    for (int x = 0; x < SIZE; x++) {
        for (int y = 0; y < SIZE; y++) {
            if (board[x][y] != EMPTY) continue;
            int preferred = 0;
            for (int i = 0; i < 5; i++) {
                int px, py;
                candidate_preference(i, &px, &py);
                if (px == x && py == y) preferred = 1;
            }
            if (preferred) continue;
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
        choose_candidate(index, &x, &y);
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
    } else if (strcmp(phase, "white4") == 0) {
        int x = -99, y = -99;
        choose_white4(&x, &y);
        printf("{\"response\":{\"action\":\"move\",\"x\":%d,\"y\":%d}}\n", x, y);
    } else if (strcmp(phase, "black5_candidates") == 0) {
        emit_candidates();
    } else if (strcmp(phase, "black5_select") == 0) {
        fputs("{\"response\":{\"action\":\"black5_select\",\"index\":0}}\n", stdout);
    } else if (strcmp(phase, "normal_play") == 0) {
        int x = -99, y = -99;
        if (choose_profile_move(&x, &y))
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
