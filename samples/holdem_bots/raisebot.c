/* raisebot：有合法加注空间时总做最小加注，否则 call/check 或 all-in。 */
#include "poker_util.h"

int main(void) {
    char *line = (char *)malloc(MAXLINE);
    if (!line) return 1;
    while (fgets(line, MAXLINE, stdin)) {
        HoldemState s = holdem_state(line);
        if (s.facing_allin || (s.to_call >= s.chips && s.chips > 0)) emit_int(-2);
        else if (s.can_raise) emit_int(s.min_raise_delta);
        else emit_int(0);
    }
    free(line);
    return 0;
}
