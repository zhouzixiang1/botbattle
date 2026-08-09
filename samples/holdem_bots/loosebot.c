/* loosebot：散漫策略——几乎每手都跟注/check 进池（极少 fold），
 * 偶尔小加注。比 callbot 更爱进池，但不下重注，长期小亏。
 */
#include "poker_util.h"

static unsigned long _ls = 0;
static unsigned long lrnd(void) {
    if (_ls == 0) {
        FILE *f = fopen("/dev/urandom", "rb");
        if (f) { if (fread(&_ls, sizeof(_ls), 1, f)) {} fclose(f); }
        if (_ls == 0) _ls = 777;
    }
    _ls = _ls * 6364136223846793005UL + 1442695040888963407UL;
    return _ls >> 16;
}

int main(void) {
    char *line = (char *)malloc(MAXLINE);
    if (!line) return 1;
    while (fgets(line, MAXLINE, stdin)) {
        HoldemState s = holdem_state(line);
        unsigned long r = lrnd() % 100;
        if (s.facing_allin || (s.to_call >= s.chips && s.chips > 0)) {
            emit_int(r < 75 ? -2 : -1);
        } else if (s.to_call == 0) {
            /* 可 check：70% check，25% 小加注，5% fold（极少弃免费牌） */
            if (r < 70) emit_int(0);                 /* check */
            else if (r < 95 && s.can_raise) emit_int(s.min_raise_delta);
            else if (r < 95) emit_int(0);
            else emit_int(-1);
        } else {
            /* 需跟注：75% call，15% 小加注，10% fold（筹码不足必 fold） */
            if (r < 75) emit_int(0);
            else if (r < 90 && s.can_raise) emit_int(s.min_raise_delta);
            else if (r < 90) emit_int(0);
            else emit_int(-1);
        }
    }
    free(line);
    return 0;
}
