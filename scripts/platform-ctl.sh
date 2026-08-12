#!/usr/bin/env bash
# botzone-platform 启停
set -euo pipefail
# Runtime state contains the database, session-bearing logs and uploaded Bot
# binaries.  Keep newly created files/directories private even when the caller's
# interactive shell has a permissive umask.
umask 077
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PID_DIR="$ROOT/platform-ctl"
LOG_DIR="$ROOT/logs"
PID_FILE="$PID_DIR/web.pid"
LOG_FILE="$LOG_DIR/web.log"
SYSTEMD_UNIT="botzone-platform.service"
STOP_WAIT_SECONDS=90
READY_WAIT_SECONDS=60

# shellcheck disable=SC1091
[[ -f .env ]] && set -a && source .env && set +a

HOST="${BZ_HOST:-127.0.0.1}"
PORT="${BZ_PORT:-50380}"
ALLOW_LAN_BIND="${BZ_ALLOW_LAN_BIND:-0}"
PY="$ROOT/.venv/bin/python"
HTTP_HOST="$HOST"
[[ "$HOST" == "::1" ]] && HTTP_HOST="[$HOST]"
[[ "$HOST" == "0.0.0.0" ]] && HTTP_HOST="127.0.0.1"
HEALTH_URL="http://$HTTP_HOST:$PORT/api/health"

# This production control script sits behind the local frp/nginx boundary.  A
# stale .env must never reopen the raw Uvicorn port and let clients spoof trusted
# proxy headers or bypass TLS/access-log policy.  Direct development can still
# invoke the CLI explicitly in an isolated worktree.
case "$HOST" in
  127.0.0.1|localhost|::1) ;;
  0.0.0.0)
    case "${ALLOW_LAN_BIND,,}" in
      1|true|yes|on) ;;
      *)
        echo "refusing non-loopback BZ_HOST=$HOST; set BZ_ALLOW_LAN_BIND=1 only after restricting port $PORT to the trusted LAN" >&2
        exit 1
        ;;
    esac
    ;;
  *)
    echo "refusing unsupported non-loopback BZ_HOST=$HOST; only gated 0.0.0.0 is allowed" >&2
    exit 1
    ;;
esac
if [[ ! "$PORT" =~ ^[0-9]{1,5}$ ]] || (( 10#$PORT < 1 || 10#$PORT > 65535 )); then
  echo "invalid BZ_PORT=$PORT; expected an integer from 1 to 65535" >&2
  exit 1
fi
SYSTEMD_HEALTH_URL="$HEALTH_URL"
SYSTEMD_PORT="$PORT"

systemd_property() {
  local property="$1"
  command -v systemctl >/dev/null 2>&1 || return 1
  systemctl --user show "$SYSTEMD_UNIT" \
    --property="$property" --value 2>/dev/null
}

uses_user_systemd() {
  local load_state working_directory
  load_state="$(systemd_property LoadState)" || return 1
  [[ "$load_state" == "loaded" ]] || return 1
  working_directory="$(systemd_property WorkingDirectory)" || return 1
  [[ -n "$working_directory" ]] || return 1
  [[ "$(realpath -m "$working_directory")" == "$(realpath -m "$ROOT")" ]]
}

port_state() {
  local port="${1:-$PORT}" listeners
  if ! command -v ss >/dev/null 2>&1; then
    echo "cannot verify port $port: ss is unavailable" >&2
    return 2
  fi
  if ! listeners="$(ss -H -ltn "sport = :$port" 2>/dev/null)"; then
    echo "cannot verify port $port: ss failed" >&2
    return 2
  fi
  [[ -n "$listeners" ]]
}

require_port_free() {
  local port="${1:-$PORT}" rc
  if port_state "$port"; then
    echo "refusing to start: port $port is already listening; do not start a second platform process" >&2
    return 1
  else
    rc=$?
    [[ "$rc" -eq 1 ]] || return "$rc"
  fi
}

health_ready() {
  local url="${1:-$HEALTH_URL}"
  command -v curl >/dev/null 2>&1 || return 1
  curl --fail --silent --show-error --max-time 2 "$url" >/dev/null 2>&1
}

wait_pid_ready() {
  local pid="$1" deadline=$((SECONDS + READY_WAIT_SECONDS))
  while (( SECONDS < deadline )); do
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "platform process exited before becoming healthy; pid=$pid" >&2
      return 1
    fi
    if health_ready "$HEALTH_URL"; then
      return 0
    fi
    sleep 1
  done
  echo "service did not become healthy within ${READY_WAIT_SECONDS}s: $HEALTH_URL" >&2
  return 1
}

wait_systemd_ready() {
  local deadline=$((SECONDS + READY_WAIT_SECONDS)) active_state main_pid
  while (( SECONDS < deadline )); do
    active_state="$(systemd_property ActiveState)" || {
      echo "cannot verify $SYSTEMD_UNIT state during startup" >&2
      return 1
    }
    if [[ "$active_state" == "failed" || "$active_state" == "inactive" ]]; then
      echo "$SYSTEMD_UNIT became $active_state before it was healthy" >&2
      return 1
    fi
    if [[ "$active_state" == "active" ]]; then
      main_pid="$(systemd_property MainPID)" || {
        echo "cannot verify $SYSTEMD_UNIT MainPID during startup" >&2
        return 1
      }
      if [[ ! "$main_pid" =~ ^[1-9][0-9]*$ ]]; then
        echo "$SYSTEMD_UNIT is active without a valid MainPID" >&2
        return 1
      fi
      if health_ready "$SYSTEMD_HEALTH_URL"; then
        return 0
      fi
    fi
    sleep 1
  done
  echo "service did not become healthy within ${READY_WAIT_SECONDS}s: $SYSTEMD_HEALTH_URL" >&2
  return 1
}

wait_pid_stopped() {
  local pid="$1" deadline=$((SECONDS + STOP_WAIT_SECONDS)) rc
  while (( SECONDS < deadline )); do
    if ! kill -0 "$pid" 2>/dev/null; then
      if port_state "$PORT"; then
        :
      else
        rc=$?
        [[ "$rc" -eq 1 ]] && return 0
        return "$rc"
      fi
    fi
    sleep 1
  done
  echo "service did not stop within ${STOP_WAIT_SECONDS}s; pid=$pid or port=$PORT is still active" >&2
  return 1
}

wait_systemd_stopped() {
  local deadline=$((SECONDS + STOP_WAIT_SECONDS)) active_state rc
  while (( SECONDS < deadline )); do
    active_state="$(systemd_property ActiveState)" || {
      echo "cannot verify $SYSTEMD_UNIT state after stop" >&2
      return 1
    }
    if [[ "$active_state" == "inactive" || "$active_state" == "failed" ]]; then
      if port_state "$SYSTEMD_PORT"; then
        :
      else
        rc=$?
        [[ "$rc" -eq 1 ]] && return 0
        return "$rc"
      fi
    fi
    sleep 1
  done
  echo "$SYSTEMD_UNIT did not release port $SYSTEMD_PORT within ${STOP_WAIT_SECONDS}s" >&2
  return 1
}

start_systemd() {
  local active_state
  active_state="$(systemd_property ActiveState)" || {
    echo "cannot verify $SYSTEMD_UNIT state before start" >&2
    return 1
  }
  case "$active_state" in
    inactive|failed) require_port_free "$SYSTEMD_PORT" ;;
    active|activating) ;;
    *)
      echo "refusing to start $SYSTEMD_UNIT while state=$active_state" >&2
      return 1
      ;;
  esac
  systemctl --user start "$SYSTEMD_UNIT"
  wait_systemd_ready
  echo "started $SYSTEMD_UNIT $SYSTEMD_HEALTH_URL"
}

stop_systemd() {
  systemctl --user stop "$SYSTEMD_UNIT"
  wait_systemd_stopped
  echo "stopped $SYSTEMD_UNIT"
}

restart_systemd() {
  local active_state main_pid
  active_state="$(systemd_property ActiveState)" || {
    echo "cannot verify $SYSTEMD_UNIT state before restart" >&2
    return 1
  }
  case "$active_state" in
    inactive|failed) require_port_free "$SYSTEMD_PORT" ;;
    active)
      main_pid="$(systemd_property MainPID)" || {
        echo "cannot verify $SYSTEMD_UNIT MainPID before restart" >&2
        return 1
      }
      if [[ ! "$main_pid" =~ ^[1-9][0-9]*$ ]]; then
        echo "refusing to restart $SYSTEMD_UNIT without a valid MainPID" >&2
        return 1
      fi
      ;;
    activating) ;;
    *)
      echo "refusing to restart $SYSTEMD_UNIT while state=$active_state" >&2
      return 1
      ;;
  esac
  systemctl --user restart "$SYSTEMD_UNIT"
  wait_systemd_ready
  echo "restarted $SYSTEMD_UNIT $SYSTEMD_HEALTH_URL"
}

status_systemd() {
  local active_state main_pid
  active_state="$(systemd_property ActiveState)" || {
    echo "cannot read $SYSTEMD_UNIT state" >&2
    return 1
  }
  main_pid="$(systemd_property MainPID)" || main_pid=0
  if [[ "$active_state" == "active" ]]; then
    if [[ ! "$main_pid" =~ ^[1-9][0-9]*$ ]]; then
      echo "unhealthy (user systemd) invalid MainPID=$main_pid"
      return 1
    fi
    if health_ready "$SYSTEMD_HEALTH_URL"; then
      echo "running (user systemd) pid=$main_pid"
      return 0
    fi
    echo "unhealthy (user systemd) pid=$main_pid"
    return 1
  fi
  echo "$active_state (user systemd) pid=$main_pid"
  return 1
}

logs_systemd() {
  local n="$1"
  journalctl --user -u "$SYSTEMD_UNIT" -n "$n" --no-pager
}

prepare_pid_runtime() {
  mkdir -p "$PID_DIR" "$LOG_DIR"
}

start_pid() {
  local pid
  prepare_pid_runtime
  if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    pid="$(cat "$PID_FILE")"
    if health_ready "$HEALTH_URL"; then
      echo "already running (pid file) pid=$pid"
      return 0
    fi
    echo "pid file references live pid=$pid but health is unavailable; refusing a second process" >&2
    return 1
  fi
  rm -f "$PID_FILE"
  require_port_free
  if [[ ! -x "$PY" ]]; then
    echo "missing .venv; run: /usr/bin/python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]'"
    exit 1
  fi
  nohup "$PY" -m bzplat.backend.cli serve --host "$HOST" --port "$PORT" \
    >>"$LOG_FILE" 2>&1 &
  echo $! >"$PID_FILE"
  pid="$(cat "$PID_FILE")"
  if ! wait_pid_ready "$pid"; then
    kill "$pid" 2>/dev/null || true
    if ! wait_pid_stopped "$pid"; then
      echo "preserving $PID_FILE because pid=$pid could not be confirmed stopped" >&2
      return 1
    fi
    rm -f "$PID_FILE"
    return 1
  fi
  echo "started (pid file) pid=$pid $HEALTH_URL"
}

stop_pid() {
  local pid
  prepare_pid_runtime
  if [[ -f "$PID_FILE" ]]; then
    pid="$(cat "$PID_FILE")"
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid"
      wait_pid_stopped "$pid"
    else
      require_port_free
    fi
    rm -f "$PID_FILE"
    echo "stopped (pid file) pid=$pid"
  else
    require_port_free
    echo "not running (pid file)"
  fi
}

status_pid() {
  prepare_pid_runtime
  if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    if health_ready "$HEALTH_URL"; then
      echo "running (pid file) pid=$(cat "$PID_FILE")"
      return 0
    else
      echo "unhealthy (pid file) pid=$(cat "$PID_FILE")"
      return 1
    fi
  else
    if port_state; then
      echo "unmanaged listener on $HOST:$PORT (no live pid file)"
      return 1
    else
      local rc=$?
      [[ "$rc" -eq 1 ]] || return "$rc"
      echo "stopped (pid file)"
      return 1
    fi
  fi
}

logs_pid() {
  local n="${1:-50}"
  tail -n "$n" "$LOG_FILE" 2>/dev/null || echo "(no logs)"
}

if uses_user_systemd; then
  case "${1:-}" in
    start) start_systemd ;;
    stop) stop_systemd ;;
    restart) restart_systemd ;;
    status) status_systemd ;;
    logs) logs_systemd "${2:-50}" ;;
    *) echo "usage: $0 start|stop|restart|status|logs [n]"; exit 1 ;;
  esac
else
  case "${1:-}" in
    start) start_pid ;;
    stop) stop_pid ;;
    restart) stop_pid; start_pid ;;
    status) status_pid ;;
    logs) logs_pid "${2:-50}" ;;
    *) echo "usage: $0 start|stop|restart|status|logs [n]"; exit 1 ;;
  esac
fi
