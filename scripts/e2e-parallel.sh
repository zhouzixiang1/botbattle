#!/usr/bin/env bash
# 三浏览器并行 e2e：每个浏览器项目一套独立 QA 栈（DB 副本 + 独立后端端口），
# 共享同一份前端构建产物。套件因共享队列/配额/同名夹具必须单 worker 串行，
# 跨项目并行是安全的提速方式（45min 串行 → 约 15min）。
#
# 用法（在仓库根或本脚本所在目录）：
#   bash scripts/e2e-parallel.sh            # 默认占用 50384-50386
#   BASE_PORT=50390 bash scripts/e2e-parallel.sh
# 前置：BZ_E2E_REF_DB 指向只读真相源 DB（默认 <repo>/botzone.db）。
# 产物：每项目结果打印在末尾；任一项目失败则整体退出码非零。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

BASE_PORT="${BASE_PORT:-50384}"
PORTS=("$BASE_PORT" "$((BASE_PORT + 1))" "$((BASE_PORT + 2))")
PROJECTS=(chromium firefox webkit)
REF_DB="${BZ_E2E_REF_DB:-$ROOT/botzone.db}"

if [[ "$BASE_PORT" == 50380 || "$BASE_PORT" == 5038[0-3] ]]; then
  echo "refusing: BASE_PORT=$BASE_PORT 与生产/保留端口段冲突（使用 50384+）" >&2
  exit 1
fi
for port in "${PORTS[@]}"; do
  if ss -tlnp | grep -q ":$port "; then
    echo "refusing: 端口 $port 已被占用" >&2
    exit 1
  fi
done
[[ -f "$REF_DB" ]] || { echo "refusing: 找不到参考 DB $REF_DB" >&2; exit 1; }

echo "==> 构建前端产物（三栈共享同一 dist）"
(cd bzplat/frontend && npm run build > /dev/null)

declare -A DB_FILE PID_MAP LOG_FILE
PIDS=()
cleanup() {
  for pid in "${PIDS[@]:-}"; do kill "$pid" 2>/dev/null || true; done
}
trap cleanup EXIT

for i in "${!PROJECTS[@]}"; do
  project="${PROJECTS[$i]}"
  port="${PORTS[$i]}"
  db="$ROOT/.e2e-parallel-$project.db"
  DB_FILE[$project]="$db"
  if [[ ! -f "$db" || "${FRESH_DB:-0}" == 1 ]]; then
    echo "==> 复制隔离 DB：$project <- $REF_DB"
    python3 - "$REF_DB" "$db" <<'PY'
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close(); src.close()
PY
  fi
  python3 scripts/seed_test_accounts.py --db "$db" --with-role-accounts > /dev/null
  key="qa-e2e-parallel-$project"
  echo "==> 启动 $project 栈：:$port instance=$key"
  # 浏览器直连后端（后端静态托管 dist），Origin 即 http://127.0.0.1:$port。
  BZ_DB_PATH="$db" BZ_INSTANCE_KEY="$key" \
    BZ_DOCKER_HOST="${BZ_DOCKER_HOST:-unix:///var/run/docker.sock}" \
    BZ_QA_INSTANCE=1 BZ_SKIP_CAPTCHA=1 \
    BZ_PUBLIC_ORIGIN="http://127.0.0.1:$port" \
    "$ROOT/.venv/bin/python" -m bzplat.backend.cli serve \
    --host 127.0.0.1 --port "$port" \
    > "/tmp/e2e-parallel-$project.backend.log" 2>&1 &
  PIDS+=("$!")
done

echo "==> 等待全部后端健康"
for port in "${PORTS[@]}"; do
  for _ in $(seq 1 30); do
    if curl -sf -m 2 "http://127.0.0.1:$port/api/health" > /dev/null; then break; fi
    sleep 1
  done
  curl -sf -m 2 "http://127.0.0.1:$port/api/health" > /dev/null || {
    echo "refusing: :$port 未在 30s 内健康" >&2; exit 1; }
done

start=$(date +%s)
for i in "${!PROJECTS[@]}"; do
  project="${PROJECTS[$i]}"
  port="${PORTS[$i]}"
  LOG_FILE[$project]="/tmp/e2e-parallel-$project.run.log"
  # 并发 npx 会竞态到全局缓存副本（找不到本仓库 config），必须用本地二进制。
  ( cd bzplat/frontend && BZ_E2E_BASE_URL="http://127.0.0.1:$port" \
      ./node_modules/.bin/playwright test --project="$project" ) \
    > "${LOG_FILE[$project]}" 2>&1 &
  PID_MAP[$project]=$!
  PIDS+=("${PID_MAP[$project]}")
done

fail=0
for project in "${PROJECTS[@]}"; do
  if wait "${PID_MAP[$project]}"; then :; else fail=1; fi
  echo "--- $project ---"
  grep -E "passed|failed|flaky|did not run" "${LOG_FILE[$project]}" | tail -3 || true
done
end=$(date +%s)
echo "== 并行总耗时 $((end - start))s =="
exit "$fail"
