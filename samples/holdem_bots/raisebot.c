/* raisebot：永远最小加注（激进但可预测）。to_call=0 时也加注（最小=BB）。
 * Botzone 协议：raise response = 额外下注筹码 = raise_to - my_street_bet。
 */
#include "poker_util.h"

int main(void) {
    char *line = (char *)malloc(MAXLINE);
    if (!line) return 1;
    while (fgets(line, MAXLINE, stdin)) {
        long to_call = req_long(line, "to_call", 0);
        long chips = req_long(line, "my_chips", 0);
        long sb = req_long(line, "street_bet", 0);
        /* 最小 raise-to = current_bet + BB（或翻后 open = BB）。raise_to = sb + to_call + 100。 */
        long raise_to = sb + to_call + 100;
        if (raise_to > sb + chips) raise_to = sb + chips;  /* 不超自身筹码 */
        if (raise_to > sb && chips > to_call) {
            emit_int(raise_delta(raise_to, sb));  /* raise */
        } else {
            emit_int(0);  /* call/check 都是 0 */
        }
    }
    free(line);
    return 0;
}
