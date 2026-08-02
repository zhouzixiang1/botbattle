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
    char *line = NULL;
    size_t cap = 0;
    ssize_t n;
    while ((n = getline(&line, &cap, stdin)) != -1) {
        long to = pj_long(line, "to", 0);
        long chips = pj_long(line, "c", 0);
        unsigned long r = lrnd() % 100;
        if (to == 0) {
            /* 可 check：70% check，25% 小加注，5% fold（极少弃免费牌） */
            if (r < 70) emit("k", 0);
            else if (r < 95) {
                long rt = 100;
                if (chips >= rt) emit("r", rt);
                else emit("k", 0);
            } else emit("f", 0);
        } else {
            /* 需跟注：75% call，15% 小加注，10% fold（筹码不足必 fold） */
            if (chips < to) emit("f", 0);
            else if (r < 75) emit("c", 0);
            else if (r < 90) {
                long rt = to + 100;
                if (rt > chips + to) rt = chips + to;
                if (rt > to) emit("r", rt);
                else emit("c", 0);
            } else emit("f", 0);
        }
    }
    free(line);
    return 0;
}
