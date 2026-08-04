/* Botzone 标准协议共享工具（德州扑克样例 Bot）。
 *
 * Botzone 信封（参考 https://wiki.botzone.org.cn/index.php?title=Bot）：
 *   - Traditional / LongRunning 首回合：{"requests":[...], "responses":[...], ...}
 *   - LongRunning 后续回合：{"request": <单条请求负载>}
 * 本平台默认 LongRunning：进程长驻，每回合发一行。
 *
 * 请求负载字段（Botzone TexasHoldem2p 全名）：
 *   num_players/dealer_id/my_id/my_chips/my_cards[0-51]/public_cards[0-51]/
 *   history[{round,player_id,action,action_type}]/hand/max_hand/
 *   total_win_chips/total_win_games
 *   平台扩展（标准 Bot 可忽略）：to_call/sb/bb/opp_chips。
 *
 * 响应：{"response": <裸整数>}
 *   -1=fold, -2=allin, 0=call/check, >0=raise「额外下注筹码」(=raise_to - my_street_bet)。
 */
#ifndef POKER_UTIL_H
#define POKER_UTIL_H

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define MAXLINE 4000000  /* 单行可能含完整历史（Traditional），预留 4MB */

/* 从 JSON 串里取数字字段的值（粗略解析，够用）。找不到返回 def。
 * 注意：会匹配第一个出现的 "key"，故顶层字段（不在 history[] 数组里）须独有。 */
static long pj_long(const char *s, const char *key, long def) {
    char pat[64];
    snprintf(pat, sizeof(pat), "\"%s\"", key);
    const char *p = strstr(s, pat);
    if (!p) return def;
    p = strchr(p, ':');
    if (!p) return def;
    return atol(p + 1);
}

/* 从 Botzone 信封行里取出「当前回合」的请求负载的某个数字字段。
 *
 * ⚠️ 限制：本简化解析（pj_long 用 strstr 命中第一个出现的字段名）只在以下情况正确：
 *  - **LongRunning 后续回合**（平台默认）：信封是 {"request":{...}}，只有 1 个请求。
 *  - **首回合**（traditional/longrunning）：requests 数组只有 1 个元素。
 * 在 **Traditional 多回合**信封 {"requests":[req0,req1,...]} 里，pj_long 会命中 req0
 * （最早一回合）的字段值而非当前回合。本平台默认 LongRunning 故不受影响；若你的 Bot
 * 需支持 Traditional 多回合，请自行解析 requests[] 的最后一个元素。
 */
static long req_long(const char *line, const char *key, long def) {
    return pj_long(line, key, def);
}

/* 输出 Botzone 裸整数响应。response 为 -1/-2/0/>0。 */
static void emit_int(long response) {
    printf("{\"response\":%ld}\n", response);
    fflush(stdout);
}

/* 牌点数（Botzone card//4+2，2..14）。 */
static int card_rank(int card) { return card / 4 + 2; }

/* raise response：given raise_to_total 与本方当前 street_bet，
 * delta = raise_to - street_bet（必须 > 0）。 */
static long raise_delta(long raise_to, long street_bet) {
    long d = raise_to - street_bet;
    return d > 0 ? d : 1;
}

#endif /* POKER_UTIL_H */
