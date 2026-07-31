#!/usr/bin/env bash
# botzone-platform 启停
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PID_DIR="$ROOT/platform-ctl"
LOG_DIR="$ROOT/logs"
mkdir -p "$PID_DIR" "$LOG_DIR"
PID_FILE="$PID_DIR/web.pid"
LOG_FILE="$LOG_DIR/web.log"

# shellcheck disable=SC1091
[[ -f .env ]] && set -a && source .env && set +a

HOST="${BZ_HOST:-127.0.0.1}"
PORT="${BZ_PORT:-50380}"
PY="$ROOT/.venv/bin/python"

start() {
  if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "already running pid=$(cat "$PID_FILE")"
    return 0
  fi
  if [[ ! -x "$PY" ]]; then
    echo "missing .venv; run: /usr/bin/python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]'"
    exit 1
  fi
  nohup "$PY" -m bzplat.backend.cli serve --host "$HOST" --port "$PORT" \
    >>"$LOG_FILE" 2>&1 &
  echo $! >"$PID_FILE"
  echo "started pid=$(cat "$PID_FILE") http://$HOST:$PORT/"
}

stop() {
  if [[ -f "$PID_FILE" ]]; then
    pid=$(cat "$PID_FILE")
    kill "$pid" 2>/dev/null || true
    rm -f "$PID_FILE"
    echo "stopped $pid"
  else
    echo "not running"
  fi
}

status() {
  if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "running pid=$(cat "$PID_FILE")"
  else
    echo "stopped"
  fi
}

logs() {
  n="${1:-50}"
  tail -n "$n" "$LOG_FILE" 2>/dev/null || echo "(no logs)"
}

case "${1:-}" in
  start) start ;;
  stop) stop ;;
  restart) stop; start ;;
  status) status ;;
  logs) logs "${2:-50}" ;;
  *) echo "usage: $0 start|stop|restart|status|logs [n]"; exit 1 ;;
esac
