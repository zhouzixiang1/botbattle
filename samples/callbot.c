#include <stdio.h>
#include <stdlib.h>

#define KEEP_RUNNING ">>>BOTZONE_REQUEST_KEEP_RUNNING<<<"
#define MAX_LINE (4 * 1024 * 1024)

int main(void) {
    char *line = malloc(MAX_LINE);
    int first_response = 1;
    if (line == NULL) return 1;

    while (fgets(line, MAX_LINE, stdin) != NULL) {
        /* Holdem: response=0 表示 call/check。 */
        fputs("{\"response\":0}\n", stdout);

        if (first_response) {
            fputs(KEEP_RUNNING "\n", stdout);
            first_response = 0;
        }
        fflush(stdout);
    }

    free(line);
    return 0;
}
