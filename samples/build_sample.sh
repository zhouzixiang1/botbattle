#!/usr/bin/env bash
# 编译可直接上传的 Linux ELF 样例 Bot（holdem + pencil）
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
CC="${CC:-cc}"
OUT_DIR="${OUT_DIR:-$ROOT}"
mkdir -p "$OUT_DIR"

build() {
  local src="$1" out="$2"
  "$CC" -O2 -static -o "$out" "$src" 2>/dev/null \
    || "$CC" -O2 -o "$out" "$src"
  chmod +x "$out"
  file "$out"
}

build "$ROOT/callbot.c" "$OUT_DIR/callbot_linux_amd64"
build "$ROOT/pencilbot.c" "$OUT_DIR/pencilbot_linux_amd64"

echo "built:"
echo "  $OUT_DIR/callbot_linux_amd64 (holdem)"
echo "  $OUT_DIR/pencilbot_linux_amd64 (pencil)"
