/* aggressivebot: 有加注空间则 raise-to (to+bb*2 近似)，否则 call/check */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int peek_int(const char *s, const char *key) {
    char pat[16];
    snprintf(pat, sizeof(pat), "\"%s\"", key);
    const char *p = strstr(s, pat);
    if (!p) return 0;
    p = strchr(p, ':');
    if (!p) return 0;
    return atoi(p + 1);
}

int main(void) {
    char *line = NULL;
    size_t cap = 0;
    ssize_t n;
    while ((n = getline(&line, &cap, stdin)) != -1) {
        while (n > 0 && (line[n-1] == '\n' || line[n-1] == '\r'))
            line[--n] = 0;
        if (n <= 0) continue;
        int to = peek_int(line, "to");
        int bb = peek_int(line, "bb");
        int my = peek_int(line, "c");
        if (bb <= 0) bb = 100;
        if (to == 0 && my > bb * 4) {
            /* open-ish: raise to 3bb street total approx */
            int x = bb * 3;
            if (x < bb) x = bb;
            if (x >= my) {
                fputs("{\"a\":\"all\"}\n", stdout);
            } else {
                printf("{\"a\":\"r\",\"x\":%d}\n", x);
            }
        } else if (to > 0) {
            fputs("{\"a\":\"c\"}\n", stdout);
        } else {
            fputs("{\"a\":\"k\"}\n", stdout);
        }
        fflush(stdout);
    }
    free(line);
    return 0;
}
