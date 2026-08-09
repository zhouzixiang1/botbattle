#!/usr/bin/env bash
# 编译全部 holdem 多策略 Bot → 输出 ELF。
# 用法：bash samples/holdem_bots/gen.sh
# 8 种风格（平台唯一 JSON 信封协议）：
#   foldbot / allinbot / raisebot / randombot / tightbot / loosebot（本目录）
#   callbot / aggressivebot（samples/ 顶层）
# 产物：各风格的 linux-amd64 ELF（本目录同名文件 + 顶层 *_linux_amd64 / *_bin）。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
SAMPLES="$(cd "$ROOT/.." && pwd)"
CC="${CC:-cc}"
cd "$ROOT"

# 本目录 6 风格
BOTS=(foldbot allinbot raisebot randombot tightbot loosebot)
for b in "${BOTS[@]}"; do
  echo "==> 编译 $b"
  $CC -O2 -o "$b" "$b.c"
done

# 顶层 callbot / aggressivebot
echo "==> 编译 callbot"
$CC -O2 -static -o "$SAMPLES/callbot_linux_amd64" "$SAMPLES/callbot.c" 2>/dev/null \
  || $CC -O2 -o "$SAMPLES/callbot_linux_amd64" "$SAMPLES/callbot.c"
chmod +x "$SAMPLES/callbot_linux_amd64"

echo "==> 编译 aggressivebot"
$CC -O2 -static -o "$SAMPLES/aggressivebot_bin" "$SAMPLES/aggressivebot.c" 2>/dev/null \
  || $CC -O2 -o "$SAMPLES/aggressivebot_bin" "$SAMPLES/aggressivebot.c"
chmod +x "$SAMPLES/aggressivebot_bin"

echo "完成。产物："
ls -la "${BOTS[@]/#/$ROOT/}" "$SAMPLES/callbot_linux_amd64" "$SAMPLES/aggressivebot_bin"
