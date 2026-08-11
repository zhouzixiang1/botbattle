#!/usr/bin/env bash
# 端到端冒烟：独立临时运行时 → 上传 bot → 挑战 → 查排行榜
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY="${BZ_PYTHON:-$ROOT/.venv/bin/python}"
if [[ ! -x "$PY" ]]; then
  GIT_COMMON="$(git -C "$ROOT" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)"
  if [[ -n "$GIT_COMMON" ]] && [[ -x "$(dirname "$GIT_COMMON")/.venv/bin/python" ]]; then
    PY="$(dirname "$GIT_COMMON")/.venv/bin/python"
  fi
fi
if [[ ! -x "$PY" ]]; then
  echo "缺少可用 Python venv；可用 BZ_PYTHON 显式指定" >&2
  exit 2
fi

RUNTIME_PARENT="$(cd "${TMPDIR:-/tmp}" && pwd)"
RUNTIME="$(mktemp -d "$RUNTIME_PARENT/botbattle-e2e.XXXXXX")"
PID=""
cleanup() {
  if [[ -n "$PID" ]] && kill -0 "$PID" 2>/dev/null; then
    kill "$PID" 2>/dev/null || true
    for _ in $(seq 1 50); do
      kill -0 "$PID" 2>/dev/null || break
      sleep 0.1
    done
    if kill -0 "$PID" 2>/dev/null; then
      kill -9 "$PID" 2>/dev/null || true
    fi
    wait "$PID" 2>/dev/null || true
  fi
  case "$RUNTIME" in
    "$RUNTIME_PARENT"/botbattle-e2e.*) rm -rf -- "$RUNTIME" ;;
    *) echo "拒绝清理非预期临时目录：$RUNTIME" >&2 ;;
  esac
}
trap cleanup EXIT
export BZ_BOT_LOCAL=1
export BZ_QA_INSTANCE=1
export BZ_SKIP_CAPTCHA=1
export BZ_TEST_CAPTCHA=1
export BZ_DB_PATH="$RUNTIME/botzone.db"
export BZ_AVATAR_DIR="$RUNTIME/avatars"
export BZ_LOG_DIR="$RUNTIME/logs"
# 阻止仓库 .env 的真实 SMTP 配置被开发冒烟误用。
export SMTP_HOST="" SMTP_USER="" SMTP_PASSWORD="" SMTP_FROM=""
export BZ_HOST=127.0.0.1
if [[ -n "${BZ_E2E_PORT:-}" ]]; then
  export BZ_PORT="$BZ_E2E_PORT"
else
  export BZ_PORT="$($PY - <<'PY'
import socket
with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
)"
fi
if [[ ! "$BZ_PORT" =~ ^[0-9]+$ ]] || [[ "$BZ_PORT" == "50380" ]]; then
  echo "拒绝无效/主服务端口：$BZ_PORT" >&2
  exit 2
fi
export BZ_E2E_BASE_URL="http://$BZ_HOST:$BZ_PORT"
mkdir -p "$BZ_AVATAR_DIR" "$BZ_LOG_DIR"

# 后台启动
"$PY" -m bzplat.backend.cli serve --host "$BZ_HOST" --port "$BZ_PORT" \
  >"$BZ_LOG_DIR/e2e-server.log" 2>&1 &
PID=$!

for i in $(seq 1 30); do
  if curl -sf "$BZ_E2E_BASE_URL/api/health" >/dev/null; then break; fi
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "隔离 E2E 服务启动失败：" >&2
    tail -n 80 "$BZ_LOG_DIR/e2e-server.log" >&2 || true
    exit 1
  fi
  sleep 0.3
done
curl -sf "$BZ_E2E_BASE_URL/api/health" | tee "$RUNTIME/health.json"
"$PY" - "$RUNTIME/health.json" <<'PY'
import json
import sys
health = json.loads(open(sys.argv[1], encoding="utf-8").read())
if health.get("ok") is not True or health.get("qa_instance") is not True:
    raise SystemExit(f"health 未确认隔离 QA 实例：{health}")
PY

"$PY" <<'PY'
import json, os, urllib.request, http.cookiejar
from pathlib import Path

BASE = os.environ["BZ_E2E_BASE_URL"]
cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

def api(method, path, data=None, form=None, files=None):
    url = BASE + path
    headers = {}
    body = None
    if form is not None or files is not None:
        import uuid
        boundary = uuid.uuid4().hex
        parts = []
        if form:
            for k, v in form.items():
                parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
        if files:
            for name, (filename, content, ctype) in files.items():
                parts.append(
                    f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; filename=\"{filename}\"\r\nContent-Type: {ctype}\r\n\r\n".encode()
                    + content + b"\r\n"
                )
        parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(parts)
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    elif data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with opener.open(req, timeout=60) as resp:
        raw = resp.read().decode()
        return json.loads(raw) if raw else {}

# captcha
cap = api("GET", "/api/auth/captcha")
# 直接从 store 不可用；开发：用 create-admin 绕过。改为用 CLI 建用户后验证。
print("captcha ok", bool(cap.get("captcha_id")))
PY

# 用 CLI 建两个用户并验证邮箱
"$PY" - <<'PY'
from bzplat.backend.store import Store
from bzplat.backend.crypto import hash_password
from bzplat.backend.store.schema import ROLE_ORGANIZER
import os
store = Store(os.environ["BZ_DB_PATH"])
for name, email, role in [
    ("alice", "alice@example.com", "user"),
    ("bob", "bob@example.com", "user"),
    ("org1", "org@example.com", ROLE_ORGANIZER),
]:
    u = store.get_user_by_username(name)
    if not u:
        u = store.create_user(name, email, hash_password("password123"), role=role, display_name=name)
    store.update_user(u["id"], email_verified=1, role=role, is_active=1)
store.close()
print("seeded users")
PY

"$PY" - <<'PY'
import json, urllib.error, urllib.request, http.cookiejar, os
from pathlib import Path
from scripts._execution_request import (
    execution_request_path,
    require_execution_request,
    wait_for_execution_match,
)

BASE = os.environ["BZ_E2E_BASE_URL"]
ELF = Path("samples/callbot_linux_amd64").read_bytes()

def session():
    cj = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

def api(opener, method, path, data=None, multipart=None):
    url = BASE + path
    headers = {}
    body = None
    if multipart is not None:
        import uuid
        boundary = uuid.uuid4().hex
        parts = []
        for k, v in multipart.items():
            if isinstance(v, tuple):
                filename, content, ctype = v
                parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{filename}\"\r\nContent-Type: {ctype}\r\n\r\n".encode()+content+b"\r\n")
            else:
                parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
        parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(parts)
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    elif data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with opener.open(req, timeout=120) as resp:
        raw = resp.read().decode()
        return json.loads(raw) if raw else {}

def login(name):
    op = session()
    cap = api(op, "GET", "/api/auth/captcha")
    # captcha answer unknown — use store session inject instead
    return op, cap

# 直接用 AuthManager 发 session cookie 太绕；改用 Bearer：从 store 建 session
from bzplat.backend.store import Store
from bzplat.backend.crypto import new_session_token, session_expires

store = Store(os.environ["BZ_DB_PATH"])

def bearer_for(username):
    u = store.get_user_by_username(username)
    tok = new_session_token()
    store.add_session(tok, u["id"], session_expires())
    return tok

def api_auth(token, method, path, data=None, multipart=None):
    url = BASE + path
    headers = {"Authorization": f"Bearer {token}"}
    body = None
    if multipart is not None:
        import uuid
        boundary = uuid.uuid4().hex
        parts = []
        for k, v in multipart.items():
            if isinstance(v, tuple):
                filename, content, ctype = v
                parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{filename}\"\r\nContent-Type: {ctype}\r\n\r\n".encode()+content+b"\r\n")
            else:
                parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
        parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(parts)
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    elif data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode()
            payload = json.loads(raw) if raw else {}
            payload["_status"] = resp.status
            return payload
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"_body": raw[:240]}
        payload["_status"] = exc.code
        return payload

ta, tb, to = bearer_for("alice"), bearer_for("bob"), bearer_for("org1")
ba = api_auth(ta, "POST", "/api/bots", multipart={
    "name": "AliceBot",
    "file": ("bot.bin", ELF, "application/octet-stream"),
})
bb = api_auth(tb, "POST", "/api/bots", multipart={
    "name": "BobBot",
    "file": ("bot.bin", ELF, "application/octet-stream"),
})
print("bots", ba["bot"]["id"], bb["bot"]["id"])
ch = api_auth(ta, "POST", "/api/matches/challenge", data={
    "my_bot_id": ba["bot"]["id"],
    "opponent_bot_id": bb["bot"]["id"],
})
print("challenge", ch)
import time
execution = require_execution_request(
    int(ch.get("_status") or 0),
    ch,
    label="隔离 E2E 挑战",
    detail=str(ch.get("detail") or ch.get("_body") or ""),
)

def fetch_execution(public_id):
    payload = api_auth(
        ta, "GET", execution_request_path(public_id)
    )
    return (
        int(payload.get("_status") or 0),
        payload,
        str(payload.get("detail") or payload.get("_body") or payload)[:240],
    )

mid = wait_for_execution_match(
    execution,
    fetch_execution,
    label="隔离 E2E 挑战",
    timeout=120,
)
print("challenge admitted", execution["public_id"], mid)
for _ in range(60):
    d = api_auth(ta, "GET", f"/api/matches/{mid}")
    status = d["match"]["status"]
    if status == "aborted":
        raise SystemExit(f"match aborted: {d['match'].get('reason')}")
    if status == "completed":
        break
    time.sleep(0.5)
else:
    raise SystemExit("match timeout")

result = d["match"].get("result")
deltas = result.get("deltas") if isinstance(result, dict) else None
if not isinstance(deltas, list) or len(deltas) != 2:
    raise SystemExit(f"completed match has invalid result.deltas: {deltas!r}")
if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in deltas):
    raise SystemExit(f"completed match has non-numeric result.deltas: {deltas!r}")
if deltas[0] + deltas[1] != 0:
    raise SystemExit(f"completed match result.deltas is not zero-sum: {deltas!r}")
print("match completed", "deltas", deltas)

lb = api_auth(ta, "GET", "/api/leaderboard?game_id=holdem")
print("leaderboard size", len(lb.get("leaderboard") or []))

c = api_auth(to, "POST", "/api/contests", data={"title": "E2E Cup"})
cid = c["contest"]["id"]
api_auth(to, "POST", f"/api/contests/{cid}/open")
api_auth(ta, "POST", f"/api/contests/{cid}/register", data={"bot_id": ba["bot"]["id"]})
api_auth(tb, "POST", f"/api/contests/{cid}/register", data={"bot_id": bb["bot"]["id"]})
st = api_auth(to, "POST", f"/api/contests/{cid}/start")
print("contest started", st["contest"]["status"])
store.close()
print("E2E OK")
PY

echo "ALL E2E CHECKS PASSED"
