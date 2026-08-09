/* 平台德州扑克样例共享工具。
 *
 * 只依赖平台固定 11 字段，当前下注状态由 current request 的 history 重放得出。
 */
#ifndef POKER_UTIL_H
#define POKER_UTIL_H

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define MAXLINE 4000000
#define SMALL_BLIND 50
#define BIG_BLIND 100
#define KEEP_RUNNING ">>>BOTZONE_REQUEST_KEEP_RUNNING<<<"

typedef struct {
    long my_id;
    long chips;
    long street_bet;
    long current_bet;
    long to_call;
    long min_raise_to;
    long min_raise_delta;
    int facing_allin;
    int can_raise;
} HoldemState;

static inline const char *last_token(const char *s, const char *token) {
    const char *last = NULL;
    const char *p = s;
    while ((p = strstr(p, token)) != NULL) {
        last = p;
        p += strlen(token);
    }
    return last;
}

/* 当前 request 以 num_players 开头；取最后一次出现即可跳过较早的历史请求。 */
static inline const char *current_request(const char *line) {
    const char *p = last_token(line, "\"num_players\"");
    return p ? p : line;
}

static inline long json_long(const char *s, const char *key, long def) {
    char pat[64];
    snprintf(pat, sizeof(pat), "\"%s\"", key);
    const char *p = strstr(s, pat);
    if (!p) return def;
    p = strchr(p + strlen(pat), ':');
    if (!p) return def;
    return strtol(p + 1, NULL, 10);
}

static inline int public_card_count(const char *req) {
    const char *p = strstr(req, "\"public_cards\"");
    if (!p || !(p = strchr(p, '['))) return 0;
    const char *end = strchr(++p, ']');
    if (!end) return 0;
    int count = 0;
    while (p < end) {
        char *next = NULL;
        (void)strtol(p, &next, 10);
        if (next != p) {
            count++;
            p = next;
        } else p++;
    }
    return count;
}

static inline int current_round(const char *req) {
    int n = public_card_count(req);
    if (n >= 5) return 3;
    if (n == 4) return 2;
    if (n >= 3) return 1;
    return 0;
}

static inline HoldemState holdem_state(const char *line) {
    const char *req = current_request(line);
    HoldemState s = {0};
    s.my_id = json_long(req, "my_id", 0);
    s.chips = json_long(req, "my_chips", 0);
    long dealer = json_long(req, "dealer_id", 0);
    int round = current_round(req);
    long bets[2] = {0, 0};
    long last_raise_to = BIG_BLIND / 2;
    if (round == 0) {
        bets[dealer == 1 ? 1 : 0] = SMALL_BLIND;
        bets[dealer == 1 ? 0 : 1] = BIG_BLIND;
        s.current_bet = BIG_BLIND;
        last_raise_to = BIG_BLIND;
    }

    const char *hist = strstr(req, "\"history\"");
    const char *end = hist ? strchr(hist, ']') : NULL;
    const char *p = hist;
    while (p && end && (p = strstr(p, "\"round\"")) != NULL && p < end) {
        const char *obj_end = strchr(p, '}');
        if (!obj_end || obj_end > end) break;
        int action_round = (int)json_long(p, "round", -1);
        if (action_round == round) {
            int player = (int)json_long(p, "player_id", -1);
            long action = json_long(p, "action", 0);
            const char *kind = strstr(p, "\"action_type\"");
            if (kind && kind < obj_end && player >= 0 && player < 2) {
                if (strstr(kind, "\"raise\"") && strstr(kind, "\"raise\"") < obj_end) {
                    bets[player] += action;
                    if (bets[player] > s.current_bet) s.current_bet = bets[player];
                    last_raise_to = bets[player];
                } else if (strstr(kind, "\"call\"") && strstr(kind, "\"call\"") < obj_end) {
                    bets[player] = s.current_bet;
                } else if (strstr(kind, "\"allin\"") && strstr(kind, "\"allin\"") < obj_end) {
                    s.facing_allin = 1;
                }
            }
        }
        p = obj_end + 1;
    }

    s.street_bet = bets[s.my_id == 1 ? 1 : 0];
    s.to_call = s.current_bet > s.street_bet ? s.current_bet - s.street_bet : 0;
    s.min_raise_to = 2 * last_raise_to;
    if (s.min_raise_to < BIG_BLIND) s.min_raise_to = BIG_BLIND;
    s.min_raise_delta = s.min_raise_to - s.street_bet;
    long max_raise_to = s.street_bet + s.chips;
    s.can_raise = !s.facing_allin && s.chips > s.to_call
        && max_raise_to > s.current_bet && s.min_raise_to <= max_raise_to
        && s.min_raise_delta > 0;
    return s;
}

/* 解析当前 request 的两张底牌；返回较高点数（2..14）。 */
static inline long best_hole_rank(const char *line) {
    const char *req = current_request(line);
    const char *p = strstr(req, "\"my_cards\"");
    if (!p || !(p = strchr(p, '['))) return -1;
    char *end = NULL;
    long c1 = strtol(p + 1, &end, 10);
    if (end == p + 1) return -1;
    while (*end && *end != ',' && *end != ']') end++;
    long c2 = c1;
    if (*end == ',') c2 = strtol(end + 1, NULL, 10);
    long r1 = c1 / 4 + 2, r2 = c2 / 4 + 2;
    return r1 > r2 ? r1 : r2;
}

static int _first_emit = 1;
static inline void emit_int(long response) {
    printf("{\"response\":%ld}\n", response);
    if (_first_emit) {
        fputs(KEEP_RUNNING "\n", stdout);
        _first_emit = 0;
    }
    fflush(stdout);
}

#endif /* POKER_UTIL_H */
