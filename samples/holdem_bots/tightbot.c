/* tightbot：保守策略——翻前只玩中等以上底牌（至少一张 ≥10），否则 fold；
 * 翻后若需跟注则 call/check，不加注。避免大损失。
 * Botzone 协议请求含 my_cards（card_int 数组，poker rank = card//4+2，2..14）。
 */
#include "poker_util.h"

static unsigned long _ts = 0;
static unsigned long next_rand_simple(void) {
    if (_ts == 0) {
        FILE *f = fopen("/dev/urandom", "rb");
        if (f) { if (fread(&_ts, sizeof(_ts), 1, f)) {} fclose(f); }
        if (_ts == 0) _ts = 999;
    }
    _ts = _ts * 1103515245UL + 12345UL;
    return _ts >> 16;
}

/* 取手牌第一张的 poker rank（card_int//4 + 2）。粗略解析 my_cards 数组首元素。 */
static long first_hole_rank(const char *s) {
    const char *p = strstr(s, "\"my_cards\"");
    if (!p) return -1;
    p = strchr(p, '[');
    if (!p) return -1;
    p++;
    while (*p == ' ' || *p == '\t') p++;
    long c = atol(p);
    return c / 4 + 2; /* poker 点数 2..14 */
}

int main(void) {
    char *line = (char *)malloc(MAXLINE);
    if (!line) return 1;
    while (fgets(line, MAXLINE, stdin)) {
        long to_call = req_long(line, "to_call", 0);
        long chips = req_long(line, "my_chips", 0);
        long sb = req_long(line, "street_bet", 0);
        long r1 = first_hole_rank(line);
        /* 底牌 rank ≥ 10（10/J/Q/K/A）才玩。 */
        int strong = (r1 >= 10);
        if (to_call == 0) {
            /* 可 check：强牌偶尔加注，否则 check（绝不主动 fold 免费牌） */
            if (strong && (next_rand_simple() % 3 == 0)) {
                long rt = sb + 100;
                if (chips >= 100) emit_int(raise_delta(rt, sb));
                else emit_int(0);
            } else emit_int(0);  /* check */
        } else {
            /* 需跟注：强牌 call，弱牌 fold（筹码不足也 fold） */
            if (strong && chips >= to_call) emit_int(0);  /* call */
            else emit_int(-1);                            /* fold */
        }
    }
    free(line);
    return 0;
}
