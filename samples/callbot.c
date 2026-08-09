/* Minimal call/check poker bot — Botzone Traditional + LongRunning。
 * response=0 同时表示 call/check；首个响应后输出 LongRunning 标准握手。
 */
#include <stdio.h>
#include <stdlib.h>

int main(void) {
    char *line = (char *)malloc(4000000);
    if (!line) return 1;
    int first_response = 1;
    while (fgets(line, 4000000, stdin)) {
        /* call/check 都是 0（Botzone 裸整数 0） */
        fputs("{\"response\":0}\n", stdout);
        if (first_response) {
            fputs(">>>BOTZONE_REQUEST_KEEP_RUNNING<<<\n", stdout);
            first_response = 0;
        }
        fflush(stdout);
    }
    free(line);
    return 0;
}
