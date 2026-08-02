/* tightbot：保守策略——翻前只玩中等以上底牌（至少一张 ≥10），否则 fold；
 * 翻后若需跟注则 call/check，不加注。避免大损失。
 * 协议请求含 mc（手牌 card_int 数组，rank = card//4，0=2..12=A）。
 */
#include "poker_util.h"

/* 简单随机（前置声明，定义在文件末尾） */
static unsigned long _ts = 0;
static unsigned long next_rand_simple(void);

/* 取手牌第一张的 rank（card_int//4）。粗略解析 mc 数组首元素。 */
static long first_hole_rank(const char *s) {
    const char *p = strstr(s, "\"mc\"");
    if (!p) return -1;
    p = strchr(p, '[');
    if (!p) return -1;
    p++;
    while (*p == ' ' || *p == '\t') p++;
    long c = atol(p);
    return c / 4; /* 0=2 .. 12=A */
}

int main(void) {
    char *line = NULL;
    size_t cap = 0;
    ssize_t n;
    while ((n = getline(&line, &cap, stdin)) != -1) {
        long to = pj_long(line, "to", 0);
        long chips = pj_long(line, "c", 0);
        long r1 = first_hole_rank(line);
        /* 翻前判断（无 pc 公共牌时）：底牌 rank ≥ 8（即 ≥10）才玩。
         * 简化：只看第一张。rank 8=10,11=J,12=Q,13?——实际 0-12: 8=10,9=J,10=Q,11=K,12=A */
        int strong = (r1 >= 8); /* 第一张 ≥10 视为可玩 */
        if (to == 0) {
            /* 可 check：强牌偶尔加注，否则 check（绝不主动 fold 免费牌） */
            if (strong && (next_rand_simple() % 3 == 0)) {
                long rt = 100;
                if (chips >= rt) emit("r", rt);
                else emit("k", 0);
            } else emit("k", 0);
        } else {
            /* 需跟注：强牌 call，弱牌 fold（筹码不足也 fold） */
            if (strong && chips >= to) emit("c", 0);
            else emit("f", 0);
        }
    }
    free(line);
    return 0;
}

/* 简单随机（避免 randombot 那么讲究，够用） */
static unsigned long next_rand_simple(void) {
    if (_ts == 0) {
        FILE *f = fopen("/dev/urandom", "rb");
        if (f) { if (fread(&_ts, sizeof(_ts), 1, f)) {} fclose(f); }
        if (_ts == 0) _ts = 999;
    }
    _ts = _ts * 1103515245UL + 12345UL;
    return _ts >> 16;
}
