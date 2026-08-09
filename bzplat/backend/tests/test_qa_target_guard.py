"""QA utilities fail closed before they can write the main runtime stack."""
from __future__ import annotations

import io
import runpy
from pathlib import Path

import pytest

from bzplat.backend.qa_safety import (
    assert_qa_database_isolated,
    assert_qa_runtime_path_isolated,
    assert_qa_server_startup_isolated,
    assert_qa_upload_root_isolated,
    qa_instance_enabled,
)


SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "_qa_target.py"
GUARD = runpy.run_path(str(SCRIPT))


def make_linked_worktree(tmp_path: Path) -> tuple[Path, Path]:
    primary = tmp_path / "repo"
    worktree = primary / ".worktrees" / "qa"
    git_dir = primary / ".git" / "worktrees" / "qa"
    git_dir.mkdir(parents=True)
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text(
        "gitdir: ../../.git/worktrees/qa\n", encoding="utf-8"
    )
    (git_dir / "commondir").write_text("../..\n", encoding="utf-8")
    return primary, worktree


def test_main_port_and_primary_db_are_rejected(tmp_path):
    with pytest.raises(SystemExit, match="50380"):
        GUARD["ensure_qa_base"]("http://127.0.0.1:50380")

    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    with pytest.raises(SystemExit, match="主 checkout"):
        GUARD["qa_db_path"]("botzone.db", root)


def test_linked_worktree_rejects_explicit_primary_checkout_db(tmp_path):
    primary, worktree = make_linked_worktree(tmp_path)

    assert GUARD["primary_checkout_root"](worktree) == primary.resolve()
    with pytest.raises(SystemExit, match="主 checkout"):
        GUARD["qa_db_path"](str(primary / "botzone.db"), worktree)
    with pytest.raises(SystemExit, match="主 checkout 工作树"):
        GUARD["qa_db_path"](str(primary / "qa-created.db"), worktree)

    assert GUARD["qa_db_path"]("botzone.db", worktree) == (
        worktree / "botzone.db"
    ).resolve()
    assert GUARD["qa_db_path"](str(tmp_path / "temp.db"), worktree) == (
        tmp_path / "temp.db"
    ).resolve()


def test_linked_worktree_with_unusable_git_metadata_fails_closed(tmp_path):
    worktree = tmp_path / "qa"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: missing\n", encoding="utf-8")

    with pytest.raises(SystemExit, match="无法确认 QA 数据库隔离边界"):
        GUARD["qa_db_path"]("botzone.db", worktree)


def test_server_guard_rejects_primary_db_uploads_and_hardlinks(tmp_path):
    primary = tmp_path / "repo"
    (primary / ".git").mkdir(parents=True)
    truth_db = primary / "botzone.db"
    truth_db.write_bytes(b"production-sentinel")
    (primary / "bot_uploads").mkdir()

    with pytest.raises(RuntimeError, match="主 checkout"):
        assert_qa_database_isolated(truth_db, primary)
    hardlink = tmp_path / "renamed.db"
    hardlink.hardlink_to(truth_db)
    with pytest.raises(RuntimeError, match="主 checkout"):
        assert_qa_database_isolated(hardlink, primary)
    with pytest.raises(RuntimeError, match="bot_uploads"):
        assert_qa_upload_root_isolated(primary / "bot_uploads" / "nested", primary)

    assert assert_qa_database_isolated(tmp_path / "isolated.db", primary) == (
        tmp_path / "isolated.db"
    ).resolve()
    assert assert_qa_upload_root_isolated(tmp_path / "isolated-uploads", primary) == (
        tmp_path / "isolated-uploads"
    ).resolve()


@pytest.mark.parametrize("dirname", ["bot_uploads", "avatars", "logs"])
def test_runtime_guard_rejects_every_primary_mutation_dir(tmp_path, dirname):
    primary = tmp_path / "repo"
    (primary / ".git").mkdir(parents=True)

    with pytest.raises(RuntimeError, match=dirname):
        assert_qa_runtime_path_isolated(primary / dirname / "nested", primary)


def test_runtime_guard_rejects_primary_root_and_arbitrary_primary_path(tmp_path):
    primary, worktree = make_linked_worktree(tmp_path)

    with pytest.raises(RuntimeError, match="主 checkout 工作树"):
        assert_qa_runtime_path_isolated(primary, worktree)
    with pytest.raises(RuntimeError, match="主 checkout 工作树"):
        assert_qa_runtime_path_isolated(primary / "qa-output", worktree)

    allowed = worktree / "runtime" / "logs"
    assert assert_qa_runtime_path_isolated(allowed, worktree) == allowed.resolve()


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", " on "])
def test_qa_instance_true_values_are_shared(value):
    assert qa_instance_enabled(value) is True


@pytest.mark.parametrize("value", [None, "", "0", "false", "off", "enabled"])
def test_qa_instance_false_values_are_shared(value):
    assert qa_instance_enabled(value) is False


def test_script_runtime_defaults_and_relatives_follow_isolated_db(tmp_path):
    _primary, worktree = make_linked_worktree(tmp_path)
    db_path = worktree / "runtime" / "qa.db"

    assert GUARD["qa_upload_root"](None, db_path, worktree) == (
        db_path.parent / "bot_uploads"
    ).resolve()
    assert GUARD["qa_upload_root"]("custom-uploads", db_path, worktree) == (
        db_path.parent / "custom-uploads"
    ).resolve()


def test_server_startup_guard_is_pure_and_resolves_relative_to_cwd(tmp_path):
    _primary, worktree = make_linked_worktree(tmp_path)
    runtime = worktree / "runtime"

    database, logs, avatars = assert_qa_server_startup_isolated(
        port=50381,
        db_path="qa.db",
        log_dir="qa-logs",
        avatar_dir="qa-avatars",
        source_root=worktree,
        cwd=runtime,
    )

    assert database == (runtime / "qa.db").resolve()
    assert logs == (runtime / "qa-logs").resolve()
    assert avatars == (runtime / "qa-avatars").resolve()
    assert not runtime.exists(), "纯护栏不得创建 CWD、数据库或运行时目录"


def test_server_startup_guard_rejects_main_port_and_primary_runtime(tmp_path):
    primary, worktree = make_linked_worktree(tmp_path)
    isolated = worktree / "runtime"

    with pytest.raises(RuntimeError, match="50380"):
        assert_qa_server_startup_isolated(
            port=50380,
            db_path=isolated / "qa.db",
            log_dir=isolated / "logs",
            avatar_dir=isolated / "avatars",
            source_root=worktree,
            cwd=worktree,
        )

    with pytest.raises(RuntimeError, match="logs"):
        assert_qa_server_startup_isolated(
            port=50381,
            db_path=isolated / "qa.db",
            log_dir=primary / "logs",
            avatar_dir=isolated / "avatars",
            source_root=worktree,
            cwd=worktree,
        )

    with pytest.raises(RuntimeError, match="avatars"):
        assert_qa_server_startup_isolated(
            port=50381,
            db_path=isolated / "qa.db",
            log_dir=isolated / "logs",
            avatar_dir=primary / "avatars",
            source_root=worktree,
            cwd=worktree,
        )


def test_http_target_requires_explicit_qa_marker(monkeypatch):
    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    urlopen = GUARD["assert_qa_instance"].__globals__["urllib"].request.urlopen
    monkeypatch.setattr(
        GUARD["assert_qa_instance"].__globals__["urllib"].request,
        "urlopen",
        lambda *_args, **_kwargs: Response(b'{"ok":true,"qa_instance":false}'),
    )
    with pytest.raises(SystemExit, match="BZ_QA_INSTANCE"):
        GUARD["assert_qa_instance"]("http://127.0.0.1:50381")

    monkeypatch.setattr(
        GUARD["assert_qa_instance"].__globals__["urllib"].request,
        "urlopen",
        lambda *_args, **_kwargs: Response(b'{"ok":true,"qa_instance":true}'),
    )
    GUARD["assert_qa_instance"]("http://127.0.0.1:50381")
    monkeypatch.setattr(
        GUARD["assert_qa_instance"].__globals__["urllib"].request,
        "urlopen",
        urlopen,
    )
