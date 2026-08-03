/* loosebot：散漫策略——几乎每手都跟注/check 进池（极少 fold），
 * 偶尔小加注。比 callbot 更爱进池，但不下重注，长期小亏。
 * Botzone 协议：raise = raise_to - street_bet 的额外量。
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
        long to_call = req_long(line, "to_call", 0);
        long chips = req_long(line, "my_chips", 0);
        long sb = req_long(line, "street_bet", 0);
        unsigned long r = lrnd() % 100;
        if (to_call == 0) {
            /* 可 check：70% check，25% 小加注，5% fold（极少弃免费牌） */
            if (r < 70) emit_int(0);                 /* check */
            else if (r < 95) {
                long rt = sb + 100;
                if (chips >= 100) emit_int(raise_delta(rt, sb));
                else emit_int(0);
            } else emit_int(-1);                     /* fold */
        } else {
            /* 需跟注：75% call，15% 小加注，10% fold（筹码不足必 fold） */
            if (chips < to_call) emit_int(-1);      /* fold */
            else if (r < 75) emit_int(0);           /* call */
            else if (r < 90) {
                long rt = sb + to_call + 100;
                if (rt > sb + chips) rt = sb + chips;
                if (rt > sb) emit_int(raise_delta(rt, sb));
                else emit_int(0);                   /* call */
            } else emit_int(-1);                    /* fold */
        }
    }
    free(line);
    return 0;
}
