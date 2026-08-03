/* randombot：随机合法动作（check/call/fold/raise 随机），不可预测。
 * 用 /dev/urandom 取种子 + 线性同余，保证跨进程不同序列。
 * Botzone 协议：raise = raise_to - street_bet 的额外量。
 */
#include "poker_util.h"

static unsigned long rng_state = 0;
static unsigned long next_rand(void) {
    if (rng_state == 0) {
        FILE *f = fopen("/dev/urandom", "rb");
        if (f) { if (fread(&rng_state, sizeof(rng_state), 1, f)) {} fclose(f); }
        if (rng_state == 0) rng_state = 12345;
    }
    rng_state = rng_state * 6364136223846793005UL + 1442695040888963407UL;
    return rng_state >> 16;
}

int main(void) {
    char *line = (char *)malloc(MAXLINE);
    if (!line) return 1;
    while (fgets(line, MAXLINE, stdin)) {
        long to_call = req_long(line, "to_call", 0);
        long chips = req_long(line, "my_chips", 0);
        long sb = req_long(line, "street_bet", 0);
        unsigned long r = next_rand() % 100;
        if (to_call == 0) {
            /* 可 check：60% check，30% raise(最小)，10% fold(故意弱) */
            if (r < 60) emit_int(0);                 /* check */
            else if (r < 90) {
                long rt = sb + 100;
                if (chips >= 100) emit_int(raise_delta(rt, sb));  /* raise */
                else emit_int(0);                    /* check */
            } else emit_int(-1);                     /* fold */
        } else {
            /* 需跟注：40% call，35% fold，25% raise */
            if (r < 40) emit_int(0);                 /* call */
            else if (r < 75) emit_int(-1);           /* fold */
            else {
                long rt = sb + to_call + 100;
                if (rt > sb + chips) rt = sb + chips;
                if (rt > sb && chips > to_call) emit_int(raise_delta(rt, sb));
                else if (chips >= to_call) emit_int(0);  /* call */
                else emit_int(-1);                       /* fold */
            }
        }
    }
    free(line);
    return 0;
}
