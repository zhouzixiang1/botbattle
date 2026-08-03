/* allinbot：永远全押（极端激进，要么大赢要么大输）。Botzone 协议：response=-2。 */
#include "poker_util.h"

int main(void) {
    char *line = (char *)malloc(MAXLINE);
    if (!line) return 1;
    while (fgets(line, MAXLINE, stdin)) {
        emit_int(-2);  /* allin */
    }
    free(line);
    return 0;
}
