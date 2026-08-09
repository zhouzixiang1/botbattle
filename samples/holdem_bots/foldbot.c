/* foldbot：永远弃牌（最弱策略）。平台协议：response=-1。 */
#include "poker_util.h"

int main(void) {
    char *line = (char *)malloc(MAXLINE);
    if (!line) return 1;
    while (fgets(line, MAXLINE, stdin)) {
        emit_int(-1);  /* fold */
    }
    free(line);
    return 0;
}
