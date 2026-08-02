/* randombot：随机合法动作（check/call/fold/raise 随机），不可预测。
 * 用 /dev/urandom 取种子 + 线性同余，保证跨进程不同序列。 */
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
    char *line = NULL;
    size_t cap = 0;
    ssize_t n;
    while ((n = getline(&line, &cap, stdin)) != -1) {
        long to = pj_long(line, "to", 0);
        long chips = pj_long(line, "c", 0);
        unsigned long r = next_rand() % 100;
        if (to == 0) {
            /* 可 check：60% check，30% raise(最小)，10% fold(故意弱) */
            if (r < 60) emit("k", 0);
            else if (r < 90) {
                long rt = 100;
                if (chips >= rt) emit("r", rt);
                else emit("k", 0);
            } else emit("f", 0);
        } else {
            /* 需跟注：40% call，35% fold，25% raise */
            if (r < 40) emit("c", 0);
            else if (r < 75) emit("f", 0);
            else {
                long rt = to * 2 + 100;
                if (rt > chips + to) rt = chips + to;
                if (rt > to && chips > to) emit("r", rt);
                else emit(to > 0 && chips >= to ? "c" : "f", 0);
            }
        }
    }
    free(line);
    return 0;
}
