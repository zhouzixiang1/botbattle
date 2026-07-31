#!/usr/bin/env bash
# 编译 Linux ELF 样例 bot
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
OUT="$ROOT/callbot_linux_amd64"
cc -O2 -static -o "$OUT" "$ROOT/callbot.c" 2>/dev/null || cc -O2 -o "$OUT" "$ROOT/callbot.c"
chmod +x "$OUT"
file "$OUT"
echo "built: $OUT"
