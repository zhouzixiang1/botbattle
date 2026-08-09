"""Test-account seeding must never silently introduce privileged credentials."""
from __future__ import annotations

import runpy
from pathlib import Path

import pytest

from bzplat.backend.crypto import hash_password
from bzplat.backend.store import Store


SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "seed_test_accounts.py"
SEED = runpy.run_path(str(SCRIPT))


def make_linked_worktree(tmp_path: Path) -> tuple[Path, Path]:
    primary = tmp_path / "repo"
    worktree = primary / ".worktrees" / "qa"
    git_dir = primary / ".git" / "worktrees" / "qa"
    git_dir.mkdir(parents=True)
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text(
        f"gitdir: {git_dir}\n", encoding="utf-8"
    )
    (git_dir / "commondir").write_text("../..\n", encoding="utf-8")
    return primary, worktree


def test_role_seed_refuses_to_promote_existing_namesake(tmp_path):
    store = Store(str(tmp_path / "seed.db"))
    user = store.create_user(
        "qa_admin", "real@example.com", hash_password("Different123"), role="user"
    )

    with pytest.raises(RuntimeError, match="拒绝复用已有账号"):
        SEED["get_or_create_role_user"](
            store, "qa_admin", "qa_admin@example.com", "Test1234", "admin"
        )

    assert store.get_user(user["id"])["role"] == "user"
    store.close()


@pytest.mark.parametrize(
    ("email", "password", "role", "mismatch"),
    [
        ("owner@example.com", "Test1234", "user", "email"),
        ("tester1@example.com", "Wrong1234", "user", "password"),
        ("tester1@example.com", "Test1234", "organizer", "role"),
    ],
)
def test_player_seed_rejects_unknown_namesake_without_activating(
    tmp_path, email, password, role, mismatch
):
    store = Store(str(tmp_path / f"seed-{mismatch}.db"))
    user = store.create_user(
        "tester1", email, hash_password(password), role=role
    )
    store.update_user(user["id"], is_active=0, email_verified=0)

    with pytest.raises(RuntimeError, match=mismatch):
        SEED["get_or_create_user"](
            store, "tester1", "tester1@example.com", "Test1234"
        )

    unchanged = store.get_user(user["id"])
    assert unchanged["is_active"] == 0
    assert unchanged["email_verified"] == 0
    assert unchanged["role"] == role
    store.close()


def test_player_seed_reuses_only_exact_identity_then_activates(tmp_path):
    store = Store(str(tmp_path / "seed-exact.db"))
    user = store.create_user(
        "tester2",
        "tester2@example.com",
        hash_password("Test1234"),
        role="user",
    )
    store.update_user(user["id"], is_active=0, email_verified=0)

    seeded = SEED["get_or_create_user"](
        store, "tester2", "tester2@example.com", "Test1234"
    )

    assert seeded["id"] == user["id"]
    assert seeded["is_active"] == 1
    assert seeded["email_verified"] == 1
    store.close()


def test_player_seed_rejects_dedicated_email_owned_by_another_user(tmp_path):
    store = Store(str(tmp_path / "seed-email-owner.db"))
    store.create_user(
        "someone_else",
        "tester1@example.com",
        hash_password("Unrelated1234"),
    )

    with pytest.raises(RuntimeError, match="邮箱已属于其他账号"):
        SEED["get_or_create_user"](
            store, "tester1", "tester1@example.com", "Test1234"
        )

    assert store.get_user_by_username("tester1") is None
    store.close()


def test_role_seed_rejects_primary_checkout_default_db(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    with pytest.raises(RuntimeError, match="主 checkout"):
        SEED["assert_role_account_target"](root, root / "botzone.db")

    # A distinct explicitly supplied database remains allowed.
    SEED["assert_role_account_target"](root, tmp_path / "isolated.db")


def test_role_seed_from_linked_worktree_rejects_explicit_primary_db(tmp_path):
    primary, worktree = make_linked_worktree(tmp_path)

    with pytest.raises(RuntimeError, match="主 checkout"):
        SEED["assert_role_account_target"](worktree, primary / "botzone.db")

    SEED["assert_role_account_target"](worktree, worktree / "botzone.db")


def test_seed_uploads_default_and_relative_paths_follow_database(tmp_path):
    _primary, worktree = make_linked_worktree(tmp_path)
    db_path = worktree / "runtime" / "seed.db"

    resolved_db, default_uploads = SEED["resolve_seed_paths"](
        worktree, str(db_path)
    )
    assert resolved_db == db_path.resolve()
    assert default_uploads == (db_path.parent / "bot_uploads").resolve()

    _resolved_db, relative_uploads = SEED["resolve_seed_paths"](
        worktree, str(db_path), "custom-uploads"
    )
    assert relative_uploads == (db_path.parent / "custom-uploads").resolve()
