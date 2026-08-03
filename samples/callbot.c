/* Minimal call/check poker bot — Botzone 标准协议（裸整数 response）。
 * to_call>0 → 0(call)；to_call==0 → 0(check)。call/check 都是 0，引擎按合法集判定。
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* 从 Botzone 信封行取顶层字段（当前回合请求的字段名独有）。 */
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
        /* call/check 都是 0（Botzone 裸整数 0） */
        long to_call = peek_long(line, "to_call");
        (void)to_call;  /* callbot 不区分，统一 0 */
        fputs("{\"response\":0}\n", stdout);
        fflush(stdout);
    }
    free(line);
    return 0;
}
