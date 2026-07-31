#!/usr/bin/env bash
# 一键重建前端产物并重启服务（解决"改了代码没生效"问题）。
# 前端产物（bzplat/frontend/dist）由后端 StaticFiles 托管，且后端代码由运行中的
# 进程加载——所以改前端或后端后，必须 build + restart 才会生效。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "==> 构建前端（npm run build）"
(cd bzplat/frontend && npm run build)

echo "==> 重启后端服务"
bash scripts/platform-ctl.sh restart

echo "==> 完成。服务状态："
bash scripts/platform-ctl.sh status
