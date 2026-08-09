"""Shared target guard for browser/API QA scripts.

All mutating QA must run against an isolated worktree stack. Port 50380 is the
main service and is therefore rejected unconditionally.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import urllib.request
from urllib.parse import urlsplit

from bzplat.backend.qa_safety import (
    assert_qa_database_isolated,
    assert_qa_runtime_path_isolated,
    primary_checkout_root,
)


def ensure_qa_base(base: str) -> str:
    base = base.rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SystemExit(f"无效 QA 地址：{base!r}")
    if parsed.port == 50380:
        raise SystemExit(
            "拒绝对 50380 main 服务运行 QA；请指向 worktree 栈"
        )
    return base


def qa_base(default: str = "http://127.0.0.1:5173") -> str:
    return ensure_qa_base(
        os.environ.get("BZ_E2E_BASE_URL")
        or os.environ.get("BZ_QA_BASE_URL")
        or default
    )


def qa_db_path(raw: str, root: Path) -> Path:
    """Resolve a QA database and reject the primary checkout's truth-source DB."""
    candidate = Path(raw)
    db_path = (candidate if candidate.is_absolute() else root / candidate).resolve()
    try:
        return assert_qa_database_isolated(db_path, root)
    except RuntimeError as exc:
        raise SystemExit(f"无法确认 QA 数据库隔离边界：{exc}") from exc


def qa_runtime_path(
    raw: str | None,
    db_path: str | Path,
    root: Path,
    dirname: str,
) -> Path:
    """Resolve a mutable QA artifact beside its isolated DB by default.

    Explicit relative paths are also rooted at ``db.parent`` rather than process
    CWD, so moving the DB to a temporary runtime cannot accidentally leave uploads
    or logs in the checkout.
    """
    db = qa_db_path(str(db_path), root)
    value = (raw or "").strip()
    candidate = Path(value).expanduser() if value else Path(dirname)
    if not candidate.is_absolute():
        candidate = db.parent / candidate
    try:
        return assert_qa_runtime_path_isolated(
            candidate,
            root,
            purpose=f"QA {dirname} ",
        )
    except RuntimeError as exc:
        raise SystemExit(f"无法确认 QA 产物隔离边界：{exc}") from exc


def qa_upload_root(raw: str | None, db_path: str | Path, root: Path) -> Path:
    return qa_runtime_path(raw, db_path, root, "bot_uploads")


def assert_qa_instance(base: str) -> None:
    """Verify that the HTTP target explicitly opted into destructive QA."""
    try:
        with urllib.request.urlopen(f"{base.rstrip('/')}/api/health", timeout=10) as response:
            health = json.load(response)
    except Exception as exc:  # noqa: BLE001 - CLI should fail closed with context
        raise SystemExit(f"无法验证 QA 实例 {base}: {exc}") from exc
    if health.get("qa_instance") is not True:
        raise SystemExit(
            "目标未设置 BZ_QA_INSTANCE=1，拒绝运行会写数据的 QA"
        )
