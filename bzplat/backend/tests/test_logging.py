"""日志配置 + admin 日志端点测试。"""
from __future__ import annotations

import logging

from fastapi.testclient import TestClient

from bzplat.backend.crypto import hash_password
from bzplat.backend.logging_config import setup_logging
from bzplat.backend.main import create_app


def test_setup_logging_writes_file(tmp_path):
    """setup_logging 后日志落到指定目录的 app.log。"""
    log_dir = tmp_path / "logs"
    setup_logging(log_dir=log_dir, level="INFO")
    log = logging.getLogger("test.logging")
    log.info("一条测试日志 line-xyz")
    # flush handlers
    for h in logging.getLogger().handlers:
        h.flush()
    app_log = log_dir / "app.log"
    assert app_log.is_file()
    content = app_log.read_text(encoding="utf-8")
    assert "line-xyz" in content
    assert "INFO" in content
    assert "[test.logging]" in content  # 含模块名


def _admin_client(tmp_path):
    # 确保日志写入默认 logs/app.log（端点读取的文件）
    setup_logging(level="INFO")
    db = str(tmp_path / "a.db")
    app = create_app(db_path=db)
    store = app.state.store
    u = store.create_user("admin", "a@ex.com", hash_password("password12"), role="admin")
    store.update_user(u["id"], email_verified=1)
    _, token = app.state.auth.authenticate("admin", "password12")
    c = TestClient(app)
    c.headers["Authorization"] = f"Bearer {token}"
    return c


def _flush():
    for h in logging.getLogger().handlers:
        h.flush()


def test_admin_logs_endpoint(tmp_path):
    """admin 能读 logs/app.log，支持关键字过滤。"""
    client = _admin_client(tmp_path)
    log = logging.getLogger("test.adminlogs")
    log.warning("admin-logs-marker-1234")
    _flush()
    r = client.get("/api/admin/logs?q=admin-logs-marker-1234")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data["lines"], list)
    assert any("admin-logs-marker-1234" in ln for ln in data["lines"])


def test_admin_logs_level_filter(tmp_path):
    """按级别过滤：只回 WARNING。"""
    client = _admin_client(tmp_path)
    log = logging.getLogger("test.levelfilter")
    log.info("lf-info-xxx")
    log.warning("lf-warn-yyy")
    _flush()
    r = client.get("/api/admin/logs?level=WARNING&q=lf-")
    lines = r.json()["lines"]
    assert any("lf-warn-yyy" in ln for ln in lines)
    assert not any("lf-info-xxx" in ln for ln in lines)


def test_admin_logs_requires_admin(tmp_path):
    """非 admin 不可访问。"""
    db = str(tmp_path / "b.db")
    app = create_app(db_path=db)
    c = TestClient(app)
    assert c.get("/api/admin/logs").status_code == 401
