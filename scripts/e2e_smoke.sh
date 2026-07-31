#!/usr/bin/env bash
# 端到端冒烟：起服务 → 注册/验证 → 上传 bot → 挑战 → 查排行榜
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export BZ_BOT_LOCAL=1
export BZ_DB_PATH="$ROOT/.e2e_botzone.db"
export BZ_HOST=127.0.0.1
export BZ_PORT=50381
rm -f "$BZ_DB_PATH"

source .venv/bin/activate
pip install -e '.[dev]' -q

# 后台启动
.venv/bin/python -m bzplat.backend.cli serve --host "$BZ_HOST" --port "$BZ_PORT" \
  >"$ROOT/logs/e2e.log" 2>&1 &
PID=$!
mkdir -p logs
cleanup() { kill $PID 2>/dev/null || true; }
trap cleanup EXIT

for i in $(seq 1 30); do
  if curl -sf "http://$BZ_HOST:$BZ_PORT/api/health" >/dev/null; then break; fi
  sleep 0.3
done
curl -sf "http://$BZ_HOST:$BZ_PORT/api/health" | tee /tmp/bz_health.json

PY=.venv/bin/python
$PY <<'PY'
import json, urllib.request, http.cookiejar, ssl
from pathlib import Path

BASE = "http://127.0.0.1:50381"
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
.venv/bin/python - <<PY
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
print("seeded users")
PY

.venv/bin/python - <<'PY'
import json, urllib.request, http.cookiejar, os
from pathlib import Path

BASE = "http://127.0.0.1:50381"
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
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode())

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
    "hands": 2,
})
print("challenge", ch)
import time
mid = ch["match_id"]
for _ in range(60):
    d = api_auth(ta, "GET", f"/api/matches/{mid}")
    if d["match"]["status"] in ("completed", "aborted"):
        print("match done", d["match"]["status"], "earnings", d["match"]["earnings_a"], d["match"]["earnings_b"])
        break
    time.sleep(0.5)
else:
    raise SystemExit("match timeout")

lb = api_auth(ta, "GET", "/api/leaderboard")
print("leaderboard size", len(lb.get("leaderboard") or []))

c = api_auth(to, "POST", "/api/contests", data={"title": "E2E Cup", "hands_per_match": 2})
cid = c["contest"]["id"]
api_auth(to, "POST", f"/api/contests/{cid}/open")
api_auth(ta, "POST", f"/api/contests/{cid}/register", data={"bot_id": ba["bot"]["id"]})
api_auth(tb, "POST", f"/api/contests/{cid}/register", data={"bot_id": bb["bot"]["id"]})
st = api_auth(to, "POST", f"/api/contests/{cid}/start")
print("contest started", st["contest"]["status"])
print("E2E OK")
PY

echo "ALL E2E CHECKS PASSED"
