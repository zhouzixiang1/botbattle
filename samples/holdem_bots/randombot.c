/* randombot：从重放 history 得到的合法动作中随机选择。
 * 用 /dev/urandom 取种子 + 线性同余，保证跨进程不同序列。
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
        HoldemState s = holdem_state(line);
        unsigned long r = next_rand() % 100;
        if (s.facing_allin || (s.to_call >= s.chips && s.chips > 0)) {
            emit_int(r < 50 ? -2 : -1);
        } else if (s.to_call == 0) {
            /* 可 check：60% check，30% raise(最小)，10% fold(故意弱) */
            if (r < 60) emit_int(0);                 /* check */
            else if (r < 90 && s.can_raise) emit_int(s.min_raise_delta);
            else if (r < 90) emit_int(0);
            else emit_int(-1);
        } else {
            /* 需跟注：40% call，35% fold，25% raise */
            if (r < 40) emit_int(0);                 /* call */
            else if (r < 75) emit_int(-1);           /* fold */
            else if (s.can_raise) emit_int(s.min_raise_delta);
            else emit_int(0);
        }
    }
    free(line);
    return 0;
}
