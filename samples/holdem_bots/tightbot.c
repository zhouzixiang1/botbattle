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

int main(void) {
    char *line = (char *)malloc(MAXLINE);
    if (!line) return 1;
    while (fgets(line, MAXLINE, stdin)) {
        HoldemState s = holdem_state(line);
        long r1 = best_hole_rank(line);
        /* 底牌 rank ≥ 10（10/J/Q/K/A）才玩。 */
        int strong = (r1 >= 10);
        if (s.facing_allin) {
            emit_int(strong ? -2 : -1);
        } else if (s.to_call == 0) {
            /* 可 check：强牌偶尔加注，否则 check（绝不主动 fold 免费牌） */
            if (strong && s.can_raise && (next_rand_simple() % 3 == 0))
                emit_int(s.min_raise_delta);
            else emit_int(0);
        } else {
            /* 需跟注：强牌 call，弱牌 fold（筹码不足也 fold） */
            if (strong && s.chips > s.to_call) emit_int(0);
            else emit_int(-1);
        }
    }
    free(line);
    return 0;
}
