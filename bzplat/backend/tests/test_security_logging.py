"""安全日志与审计测试：access.log + audit.log + 真实 IP 透传 + 验证码脱敏。

覆盖公网暴露加固（PR feat/security-logging）：
- logging_config 三 handler（app/access/audit 独立文件 + propagate 隔离）。
- AccessLogMiddleware 记真实 IP（trust_proxy 开启时读 X-Forwarded-For）。
- audit_log 辅助函数格式（ip/action/result/user/target/detail）+ result=fail 升 WARNING。
- admin_logs file 参数（app/access/audit 三文件白名单，防路径穿越）。
- 验证码脱敏（SMTP 未配置时不打明文 code 到日志）。
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bzplat.backend.crypto import hash_password
from bzplat.backend.logging_config import ACCESS_LOGGER, AUDIT_LOGGER, setup_logging
from bzplat.backend.security import audit_log, client_ip


@pytest.fixture
def log_dir(tmp_path):
    d = tmp_path / "logs"
    d.mkdir()
    setup_logging(log_dir=d, level="INFO")
    return d


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else ""


# ── logging_config：三 handler 独立文件 + propagate 隔离 ────────────────────


def test_three_log_files_created(log_dir):
    """setup_logging 后应创建 app.log / access.log / audit.log 三个文件。"""
    # 触发一条各 logger
    logging.getLogger("bzplat.backend").info("app msg")
    logging.getLogger(ACCESS_LOGGER).info("access msg")
    logging.getLogger(AUDIT_LOGGER).info("audit msg")
    for name in ("app.log", "access.log", "audit.log"):
        # 文件可能尚未落盘（buffer），flush 一下
        for h in logging.getLogger().handlers + logging.getLogger(ACCESS_LOGGER).handlers + logging.getLogger(AUDIT_LOGGER).handlers:
            try:
                h.flush()
            except Exception:
                pass
    # access/audit 各自的 handler 目标文件
    assert (log_dir / "app.log").is_file()
    assert (log_dir / "access.log").is_file()
    assert (log_dir / "audit.log").is_file()


def test_access_audit_do_not_leak_to_app_log(log_dir):
    """access/audit logger 的消息不应进 app.log（propagate=False）。"""
    logging.getLogger(ACCESS_LOGGER).info("ACCESS_ONLY_MARKER")
    logging.getLogger(AUDIT_LOGGER).info("AUDIT_ONLY_MARKER")
    for h in logging.getLogger().handlers + logging.getLogger(ACCESS_LOGGER).handlers + logging.getLogger(AUDIT_LOGGER).handlers:
        try:
            h.flush()
        except Exception:
            pass
    app_content = _read(log_dir / "app.log")
    assert "ACCESS_ONLY_MARKER" not in app_content, "access 日志不应泄漏到 app.log"
    assert "AUDIT_ONLY_MARKER" not in app_content, "audit 日志不应泄漏到 app.log"
    # 但各自文件里要有
    assert "ACCESS_ONLY_MARKER" in _read(log_dir / "access.log")
    assert "AUDIT_ONLY_MARKER" in _read(log_dir / "audit.log")


# ── client_ip：trust_proxy 解析 X-Forwarded-For ─────────────────────────────


class _FakeReq:
    """最小化 Request 替身，用于 client_ip 单测。"""

    def __init__(self, headers: dict[str, str], host: str = "127.0.0.1"):
        self.headers = headers
        self.client = type("C", (), {"host": host})()


def test_client_ip_trust_proxy_reads_xff_rightmost():
    """XFF 取倒数第 hops 跳（受信代理前一跳），非最左可伪造段（审计 P1）。"""
    # 单层 nginx（覆盖式 XFF 只 1 段）：最左==最右，行为不变
    req = _FakeReq({"x-forwarded-for": "203.0.113.5"})
    assert client_ip(req, trust_proxy=True, hops=1) == "203.0.113.5"
    # 追加式 XFF（客户端塞伪造最左 + nginx 追加真实段）：取最右（nginx 加的）
    req = _FakeReq({"x-forwarded-for": "999.999.999.999, 203.0.113.5"})
    assert client_ip(req, trust_proxy=True, hops=1) == "203.0.113.5"


def test_client_ip_trust_proxy_prefers_real_ip():
    """优先 X-Real-IP（nginx 覆盖式设置，客户端无法伪造）。"""
    req = _FakeReq({"x-real-ip": "198.51.100.7", "x-forwarded-for": "1.2.3.4"})
    assert client_ip(req, trust_proxy=True) == "198.51.100.7"


def test_client_ip_no_trust_proxy_uses_socket_peer():
    req = _FakeReq({"x-forwarded-for": "203.0.113.5"}, host="127.0.0.1")
    assert client_ip(req, trust_proxy=False) == "127.0.0.1"


def test_client_ip_xff_spoofed_leftmost_ignored():
    """攻击者在 XFF 最左塞伪造 IP 不应击穿（取最右可信跳）。"""
    req = _FakeReq({"x-forwarded-for": "1.1.1.1, 2.2.2.2, 3.3.3.3"})
    # hops=1 → 取最后 1 段（3.3.3.3 = 受信代理加的真实客户端段）
    assert client_ip(req, trust_proxy=True, hops=1) == "3.3.3.3"
    # hops=2 → 取倒数第 2 段（2.2.2.2，双层代理场景）
    assert client_ip(req, trust_proxy=True, hops=2) == "2.2.2.2"
    # 关键：最左的 1.1.1.1（可伪造）绝不被取
    assert client_ip(req, trust_proxy=True, hops=1) != "1.1.1.1"


# ── audit_log：格式 + result=fail 升级为 WARNING ─────────────────────────────


def test_audit_log_writes_structured_fields(log_dir):
    """audit_log 应记 ip/action/result/user/target/detail 到 audit.log。"""
    req = _FakeReq({"x-forwarded-for": "203.0.113.9"})
    audit_log(
        req, "login", result="ok", user="alice", target="alice", detail="pwd ok",
        trust_proxy=True,
    )
    for h in logging.getLogger(AUDIT_LOGGER).handlers:
        try:
            h.flush()
        except Exception:
            pass
    content = _read(log_dir / "audit.log")
    assert "action=login" in content
    assert "result=ok" in content
    assert "user=alice" in content
    assert "ip=203.0.113.9" in content


def test_audit_log_fail_is_warning(log_dir):
    """result=fail 应记为 WARNING 级别（安全事件优先关注）。"""
    req = _FakeReq({})
    audit_log(req, "login", result="fail", target="bob", detail="invalid_credentials")
    for h in logging.getLogger(AUDIT_LOGGER).handlers:
        try:
            h.flush()
        except Exception:
            pass
    content = _read(log_dir / "audit.log")
    assert " WARNING " in content
    assert "result=fail" in content


# ── admin_logs file 参数：三文件白名单 + 防路径穿越 ──────────────────────────


@pytest.fixture
def admin_client(tmp_path, monkeypatch):
    """起一个带 admin 的测试 app，BZ_LOG_DIR 指向临时目录，BZ_TEST_CAPTCHA 绕验证码。"""
    from bzplat.backend.crypto import hash_password
    from bzplat.backend.store import Store
    from bzplat.backend.main import create_app

    db_path = str(tmp_path / "t.db")
    monkeypatch.setenv("BZ_DB_PATH", db_path)
    monkeypatch.setenv("BZ_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("BZ_TRUST_PROXY", "1")
    monkeypatch.setenv("BZ_TEST_CAPTCHA", "1")
    logd = tmp_path / "logs"
    logd.mkdir(exist_ok=True)
    # 预写三文件
    (logd / "app.log").write_text("APP_LINE_TEST\n", encoding="utf-8")
    (logd / "access.log").write_text("ACCESS_LINE_TEST ip=1.2.3.4\n", encoding="utf-8")
    (logd / "audit.log").write_text("AUDIT_LINE_TEST action=login\n", encoding="utf-8")

    # 先建 admin（create_app 会打开同一个 DB）
    store = Store(db_path)
    u = store.create_user("adminu", "a@ex.com", hash_password("pw123456"), role="admin")
    store.update_user(u["id"], email_verified=1)  # 登录要求邮箱已验证
    store.close()

    app = create_app()
    client = TestClient(app)
    # 取验证码（test 模式返回 answer）
    cap = client.get("/api/auth/captcha").json()
    r = client.post("/api/auth/login", json={
        "username": "adminu", "password": "pw123456",
        "captcha_id": cap["captcha_id"], "captcha_answer": cap["answer"],
    })
    token = r.json().get("token", "")
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


def test_admin_logs_default_app(admin_client):
    r = admin_client.get("/api/admin/logs")
    assert r.status_code == 200
    lines = r.json()["lines"]
    assert any("APP_LINE_TEST" in ln for ln in lines)


def test_admin_logs_file_access(admin_client):
    r = admin_client.get("/api/admin/logs?file=access")
    assert r.status_code == 200
    lines = r.json()["lines"]
    assert any("ACCESS_LINE_TEST" in ln for ln in lines)
    assert "access.log" in r.json()["path"]


def test_admin_logs_file_audit(admin_client):
    r = admin_client.get("/api/admin/logs?file=audit")
    assert r.status_code == 200
    lines = r.json()["lines"]
    assert any("AUDIT_LINE_TEST" in ln for ln in lines)
    assert "audit.log" in r.json()["path"]


def test_admin_logs_rejects_unknown_file(admin_client):
    """file 参数不在白名单 → 回退 app.log（防路径穿越读任意文件）。"""
    r = admin_client.get("/api/admin/logs?file=../../etc/passwd")
    assert r.status_code == 200
    assert "app.log" in r.json()["path"]  # 回退 app，绝不读 /etc/passwd


# ── 验证码脱敏：SMTP 未配置时不打明文 code ──────────────────────────────────


def test_captcha_not_logged_in_plaintext(tmp_path, caplog):
    """SMTP 未配置时，验证码日志应脱敏（不含完整 code）。"""
    from bzplat.backend.auth.auth_manager import AuthManager
    from bzplat.backend.store import Store

    store = Store(str(tmp_path / "c.db"))
    user = store.create_user("masku", "m@ex.com", hash_password("pw123456"))
    store.update_user(user["id"], email_verified=0)
    # mailer=None 模拟 SMTP 未配置
    am = AuthManager(store, mailer=None)
    with caplog.at_level(logging.WARNING):
        with pytest.raises(Exception):
            am.send_verify_code(store.get_user(user["id"]))
    # 收集所有日志消息，不应含完整 6 位验证码
    full_text = " ".join(r.getMessage() for r in caplog.records)
    # 脱敏后只出现 code=XX*** 形式，不应出现连续 6 位数字的明文 code
    import re
    # 找 "code=" 后跟的内容，不应是 6 位纯数字明文
    m = re.search(r"code=(\S+)", full_text)
    if m:
        val = m.group(1)
        assert not re.fullmatch(r"\d{6}", val), f"验证码明文泄漏到日志: code={val}"
