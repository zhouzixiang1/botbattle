/* Shared helpers for sample holdem strategy bots.
 * Compact JSON line protocol: stdin 一行请求，stdout 一行 {"a":...,"x"?} 响应。
 * 请求字段（平台紧凑协议）：to=跟注额(0=可check)、c=己方剩余筹码。
 * 响应：a ∈ f(弃牌) c(跟注) k(过牌) r(加注,x=raise-to-total) all(全押)。
 */
#ifndef POKER_UTIL_H
#define POKER_UTIL_H

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* 从 JSON 行里取数字字段的值（粗略解析，够用）。找不到返回 def。 */
static long pj_long(const char *s, const char *key, long def) {
    char pat[32];
    snprintf(pat, sizeof(pat), "\"%s\"", key);
    const char *p = strstr(s, pat);
    if (!p) return def;
    p = strchr(p, ':');
    if (!p) return def;
    return atol(p + 1);
}

/* 取字符串字段的首字符（用于动作解析等）。 */
static char pj_char(const char *s, const char *key, char def) {
    char pat[32];
    snprintf(pat, sizeof(pat), "\"%s\"", key);
    const char *p = strstr(s, pat);
    if (!p) return def;
    p = strchr(p, ':');
    if (!p) return def;
    p++;
    while (*p == ' ' || *p == '\t' || *p == '"') p++;
    return *p ? *p : def;
}

/* 发送一个动作响应。 */
static void emit(const char *action, long x) {
    if (x > 0)
        printf("{\"a\":\"%s\",\"x\":%ld}\n", action, x);
    else
        printf("{\"a\":\"%s\"}\n", action);
    fflush(stdout);
}

#endif /* POKER_UTIL_H */
