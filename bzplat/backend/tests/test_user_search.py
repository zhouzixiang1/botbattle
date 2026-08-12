"""用户搜索 + /api/bots/public owner_id 过滤 测试。"""
from __future__ import annotations

import pytest

from bzplat.backend.crypto import hash_password
from bzplat.backend.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "u.db"))


def _mk(store: Store, username: str, *, display: str = "", active: int = 1):
    u = store.create_user(
        username, f"{username}@ex.com", hash_password("password1"),
        display_name=display or username,
    )
    if not active:
        store.update_user(u["id"], is_active=0)
    return u


def test_search_users_prefix(store: Store):
    _mk(store, "alpha")
    _mk(store, "alpine")
    _mk(store, "beta")
    _mk(store, "alfred", active=0)  # 停用，不应出现

    rows = store.search_users("al")
    names = [r["username"] for r in rows]
    assert set(names) == {"alpha", "alpine"}  # alfred 停用排除
    # 排序：按 username
    assert names == sorted(names)


def test_search_users_empty_q_returns_all_active(store: Store):
    _mk(store, "alpha")
    _mk(store, "beta")
    _mk(store, "gamma", active=0)
    rows = store.search_users("")
    assert {r["username"] for r in rows} == {"alpha", "beta"}


def test_search_users_limit(store: Store):
    for i in range(5):
        _mk(store, f"user{i}")
    assert len(store.search_users("user", limit=3)) == 3


def test_search_users_no_sensitive_fields(store: Store):
    _mk(store, "alpha")
    rows = store.search_users("al")
    assert rows
    for r in rows:
        # 绝不暴露 email / password_hash
        assert "email" not in r
        assert "password_hash" not in r
        assert set(r.keys()) >= {"id", "username", "display_name"}


def test_list_bots_filter_by_owner(store: Store):
    ua = store.create_user("owna", "a@ex.com", hash_password("p1"))
    ub = store.create_user("ownb", "b@ex.com", hash_password("p1"))
    ba = store.create_bot(ua["id"], "bota", binary_path="/tmp/a", format="elf", game_id="holdem")
    bb = store.create_bot(ub["id"], "botb", binary_path="/tmp/b", format="elf", game_id="holdem")
    mine = store.list_bots(owner_id=ua["id"], game_id="holdem")
    assert {b["id"] for b in mine} == {ba["id"]}
    # 不过滤 owner：两个都在（私有 bot 功能已下线，全部可见）
    allp = store.list_bots(game_id="holdem")
    assert {b["id"] for b in allp} == {ba["id"], bb["id"]}


def test_list_bots_owner_inventory_can_include_inactive(store: Store):
    owner = store.create_user("inactive_owner", "inactive@ex.com", hash_password("p1"))
    active = store.create_bot(
        owner["id"], "active_bot", binary_path="/tmp/active", format="elf",
        game_id="holdem",
    )
    inactive = store.create_bot(
        owner["id"], "inactive_bot", binary_path="/tmp/inactive", format="elf",
        game_id="holdem",
    )
    store.update_bot(inactive["id"], is_active=0)

    assert {bot["id"] for bot in store.list_bots(owner_id=owner["id"])} == {
        active["id"]
    }
    assert {
        bot["id"]
        for bot in store.list_bots(owner_id=owner["id"], active_only=False)
    } == {active["id"], inactive["id"]}
