/* aggressivebot: 有加注空间则加注（≈ to_call + BB），否则 call/check。
 * Botzone 协议：raise = raise_to - street_bet 的额外量；call/check/allin = 0/-2。
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static long peek_long(const char *s, const char *key) {
    char pat[32];
    snprintf(pat, sizeof(pat), "\"%s\"", key);
    const char *p = strstr(s, pat);
    if (!p) return 0;
    p = strchr(p, ':');
    if (!p) return 0;
    return atol(p + 1);
}

int main(void) {
    char *line = (char *)malloc(4000000);
    if (!line) return 1;
    while (fgets(line, 4000000, stdin)) {
        long to_call = peek_long(line, "to_call");
        long bb = peek_long(line, "bb");
        long chips = peek_long(line, "my_chips");
        long sb = peek_long(line, "street_bet");
        if (bb <= 0) bb = 100;
        /* 加注目标 = 补跟注 + BB（即 raise_to = sb + to_call + bb） */
        long raise_to = sb + to_call + bb;
        if (raise_to >= sb + chips) {
            /* 筹码足够全押则 allin */
            fputs("{\"response\":-2}\n", stdout);
        } else if (chips > to_call) {
            /* raise：delta = raise_to - street_bet */
            long delta = raise_to - sb;
            if (delta <= 0) delta = bb;
            printf("{\"response\":%ld}\n", delta);
        } else {
            /* call/check 都是 0 */
            fputs("{\"response\":0}\n", stdout);
        }
        fflush(stdout);
    }
    free(line);
    return 0;
}
