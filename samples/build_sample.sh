#!/usr/bin/env bash
# 编译可直接上传的 Linux ELF 样例 Bot（holdem + gomoku + pencil）
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
CC="${CC:-cc}"
OUT_DIR="${OUT_DIR:-$ROOT}"
mkdir -p "$OUT_DIR"

build() {
  local src="$1" out="$2"
  shift 2
  "$CC" -O2 -static "$@" -o "$out" "$src" 2>/dev/null \
    || "$CC" -O2 "$@" -o "$out" "$src"
  chmod +x "$out"
  local description
  description="$(file -b "$out")"
  printf '%s: %s\n' "$out" "$description"
  if [[ "$description" != *"ELF 64-bit"* || "$description" != *"x86-64"* ]]; then
    echo "error: sample upload must be a Linux x86_64 ELF" >&2
    return 1
  fi
}

build "$ROOT/callbot.c" "$OUT_DIR/callbot_linux_amd64"
build "$ROOT/gomokubot.c" "$OUT_DIR/gomokubot_linux_amd64"
build "$ROOT/pencilbot.c" "$OUT_DIR/pencilbot_linux_amd64"

SHOWCASE_OUT_DIR="$OUT_DIR/gomoku_showcase"
mkdir -p "$SHOWCASE_OUT_DIR"
build "$ROOT/gomoku_showcase/gomoku_showcase_bot.c" \
  "$SHOWCASE_OUT_DIR/gomoku_showcase_tactical_linux_amd64" -DPROFILE=1
build "$ROOT/gomoku_showcase/gomoku_showcase_bot.c" \
  "$SHOWCASE_OUT_DIR/gomoku_showcase_steady_linux_amd64" -DPROFILE=2
build "$ROOT/gomoku_showcase/gomoku_showcase_bot.c" \
  "$SHOWCASE_OUT_DIR/gomoku_showcase_foundation_linux_amd64" -DPROFILE=3

echo "built:"
echo "  $OUT_DIR/callbot_linux_amd64 (holdem)"
echo "  $OUT_DIR/gomokubot_linux_amd64 (gomoku competition rules v2)"
echo "  $OUT_DIR/pencilbot_linux_amd64 (pencil)"
echo "  $SHOWCASE_OUT_DIR/gomoku_showcase_{tactical,steady,foundation}_linux_amd64 (showcase gomoku v2)"
