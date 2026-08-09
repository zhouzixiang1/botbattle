"""日志配置 + admin 日志端点测试。"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi.testclient import TestClient

from bzplat.backend.crypto import hash_password
from bzplat.backend.logging_config import setup_logging
from bzplat.backend.main import create_app
from bzplat.backend.qa_safety import primary_checkout_root


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


def _admin_client(tmp_path, monkeypatch):
    # 测试即使从仓库 CWD 运行，也只能写 tmp_path 下的隔离日志。
    log_dir = tmp_path / "logs"
    monkeypatch.setenv("BZ_LOG_DIR", str(log_dir))
    monkeypatch.setenv("BZ_AVATAR_DIR", str(tmp_path / "avatars"))
    setup_logging(log_dir=log_dir, level="INFO")
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


def test_admin_logs_endpoint(tmp_path, monkeypatch):
    """admin 能读 logs/app.log，支持关键字过滤。"""
    client = _admin_client(tmp_path, monkeypatch)
    log = logging.getLogger("test.adminlogs")
    log.warning("admin-logs-marker-1234")
    _flush()
    r = client.get("/api/admin/logs?q=admin-logs-marker-1234")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data["lines"], list)
    assert any("admin-logs-marker-1234" in ln for ln in data["lines"])


def test_admin_logs_level_filter(tmp_path, monkeypatch):
    """按级别过滤：只回 WARNING。"""
    client = _admin_client(tmp_path, monkeypatch)
    log = logging.getLogger("test.levelfilter")
    log.info("lf-info-xxx")
    log.warning("lf-warn-yyy")
    _flush()
    r = client.get("/api/admin/logs?level=WARNING&q=lf-")
    lines = r.json()["lines"]
    assert any("lf-warn-yyy" in ln for ln in lines)
    assert not any("lf-info-xxx" in ln for ln in lines)


def test_admin_logs_requires_admin(tmp_path, monkeypatch):
    """非 admin 不可访问。"""
    monkeypatch.setenv("BZ_AVATAR_DIR", str(tmp_path / "avatars"))
    db = str(tmp_path / "b.db")
    app = create_app(db_path=db)
    c = TestClient(app)
    assert c.get("/api/admin/logs").status_code == 401


def _tree_manifest(path: Path) -> list[tuple[str, int, int]]:
    if not path.exists():
        return []
    return sorted(
        (
            str(item.relative_to(path)),
            item.stat().st_size if item.is_file() else -1,
            item.stat().st_mtime_ns,
        )
        for item in path.rglob("*")
    )


def test_repo_cwd_tmp_db_keeps_primary_uploads_and_logs_isolated(
    tmp_path, monkeypatch
):
    """Regression gate: a tmp-DB test never targets primary mutable artifacts."""
    source_root = Path(__file__).resolve().parents[3]
    primary_root = primary_checkout_root(source_root)
    assert primary_root is not None
    primary_uploads = primary_root / "bot_uploads"
    primary_logs = primary_root / "logs"
    uploads_before = _tree_manifest(primary_uploads)
    marker = "pytest-isolated-log-marker-7f8cecab"

    monkeypatch.chdir(source_root)
    runtime = tmp_path / "runtime"
    log_dir = runtime / "logs"
    monkeypatch.setenv("BZ_LOG_DIR", str(log_dir))
    monkeypatch.setenv("BZ_AVATAR_DIR", str(runtime / "avatars"))
    setup_logging(level="INFO")
    logging.getLogger("test.isolation").warning(marker)
    _flush()

    app = create_app(db_path=str(runtime / "test.db"))
    try:
        assert app.state.bot_manager.upload_root.resolve() == (
            runtime / "bot_uploads"
        ).resolve()
        assert marker in (log_dir / "app.log").read_text(encoding="utf-8")
    finally:
        app.state.store.close()

    assert _tree_manifest(primary_uploads) == uploads_before
    # The production service may append unrelated lines concurrently; the unique
    # test marker itself must never reach any primary log file.
    for log_path in primary_logs.glob("*.log") if primary_logs.exists() else ():
        assert marker not in log_path.read_text(encoding="utf-8", errors="replace")
