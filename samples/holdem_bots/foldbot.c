/* foldbot：永远弃牌（最弱策略，几乎必输）。测试赛制排名兜底用。 */
#include "poker_util.h"

int main(void) {
    char *line = NULL;
    size_t cap = 0;
    ssize_t n;
    while ((n = getline(&line, &cap, stdin)) != -1) {
        /* 不管请求，永远 fold */
        emit("f", 0);
    }
    free(line);
    return 0;
}
