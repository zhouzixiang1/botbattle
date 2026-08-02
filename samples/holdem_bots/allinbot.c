/* allinbot：永远全押（极端激进，要么大赢要么大输）。 */
#include "poker_util.h"

int main(void) {
    char *line = NULL;
    size_t cap = 0;
    ssize_t n;
    while ((n = getline(&line, &cap, stdin)) != -1) {
        emit("all", 0);
    }
    free(line);
    return 0;
}
