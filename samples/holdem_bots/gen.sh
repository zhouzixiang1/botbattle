#!/usr/bin/env bash
# 编译全部 holdem 多策略 Bot → 输出 ELF 到本目录。
# 用法：bash samples/holdem_bots/gen.sh
# 产物：foldbot/raisebot/allinbot/randombot/tightbot/loosebot（linux-amd64 ELF）
set -euo pipefail
cd "$(dirname "$0")"
CC="${CC:-cc}"
BOTS=(foldbot raisebot allinbot randombot tightbot loosebot)
for b in "${BOTS[@]}"; do
  echo "==> 编译 $b"
  $CC -O2 -o "$b" "$b.c"
done
echo "完成。产物："
ls -la foldbot raisebot allinbot randombot tightbot loosebot
