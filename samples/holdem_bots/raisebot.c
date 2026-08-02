/* raisebot：永远最小加注（激进但可预测）。to=0 时也加注（最小加注=BB）。 */
#include "poker_util.h"

int main(void) {
    char *line = NULL;
    size_t cap = 0;
    ssize_t n;
    while ((n = getline(&line, &cap, stdin)) != -1) {
        long to = pj_long(line, "to", 0);
        long chips = pj_long(line, "c", 0);
        long cur_bet = to; /* 当前需补到的额度近似为本街已有下注 */
        /* 最小 raise-to = 已有下注 + 2×to（近似 min-raise）；兜底用 BB(100) */
        long raise_to = (cur_bet + to) * 2;
        if (raise_to < 100) raise_to = 100;
        if (raise_to > chips + to) raise_to = chips + to; /* 不超自身筹码 */
        if (raise_to <= to || chips <= to) {
            /* 筹码不足以加注 → 跟注/过牌 */
            emit(to > 0 ? "c" : "k", 0);
        } else {
            emit("r", raise_to);
        }
    }
    free(line);
    return 0;
}
