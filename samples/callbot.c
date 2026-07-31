/* Minimal call/check poker bot — compact JSON line protocol on stdin/stdout */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* Very small JSON peek for "to": number */
static int peek_to_call(const char *s) {
    const char *p = strstr(s, "\"to\"");
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
        int to = peek_to_call(line);
        if (to > 0)
            fputs("{\"a\":\"c\"}\n", stdout);
        else
            fputs("{\"a\":\"k\"}\n", stdout);
        fflush(stdout);
    }
    free(line);
    return 0;
}
