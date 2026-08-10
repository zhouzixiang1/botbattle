"""赛事对阵图 + 显示 Bot 名测试（PR-6）。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bzplat.backend.crypto import hash_password
from bzplat.backend.main import create_app
from bzplat.backend.store import Store


def _store(tmp_path) -> Store:
    return Store(str(tmp_path / "cb.db"))


def test_contest_bracket_resolves_names(tmp_path):
    s = _store(tmp_path)
    o = s.create_user("org", "o@ex.com", "x", role="organizer")
    u1 = s.create_user("alice", "a@ex.com", "x")
    u2 = s.create_user("bob", "b@ex.com", "x")
    b1 = s.create_bot(u1["id"], "botA", binary_path="/tmp", format="elf", game_id="holdem", display_name="阿尔法")
    b2 = s.create_bot(u2["id"], "botB", binary_path="/tmp", format="elf", game_id="holdem")
    cid = s.create_contest("Cup", organizer_id=o["id"], game_id="holdem")["id"]
    s.add_entry(cid, u1["id"], b1["id"])
    s.add_entry(cid, u2["id"], b2["id"])
    # 插一条对阵
    s._conn.execute(
        "INSERT INTO contest_pairings(contest_id,round_num,bot_a_id,bot_b_id,status,stage_idx,stage_key) "
        "VALUES(?,?,?,?,?,?,?)",
        (cid, 1, b1["id"], b2["id"], "pending", 0, "rr"),
    )
    s._conn.commit()
    br = s.contest_bracket(cid)
    assert len(br) == 1
    row = br[0]
    assert row["bot_a_name"] == "botA"
    assert row["bot_a_display"] == "阿尔法"
    assert row["bot_b_name"] == "botB"
    assert row["owner_a_name"] == "alice"
    assert row["owner_b_name"] == "bob"
    s.close()


@pytest.mark.parametrize("persisted_game_id", ["", "unknown-game"])
def test_contest_bracket_rejects_invalid_persisted_game_id(
    tmp_path, persisted_game_id
):
    """赛事持久化 game_id 不可缺失或静默猜成 Holdem。"""
    s = _store(tmp_path)
    organizer = s.create_user("org", "o@ex.com", "x", role="organizer")
    contest_id = s.create_contest(
        "Cup", organizer_id=organizer["id"], game_id="holdem"
    )["id"]
    with s._tx() as conn:
        conn.execute(
            "UPDATE contests SET game_id=? WHERE id=?",
            (persisted_game_id, contest_id),
        )
    with pytest.raises(ValueError, match="game_id"):
        s.contest_bracket(contest_id)
    s.close()


def test_contest_bracket_rejects_missing_contest_identity(tmp_path):
    """不存在的赛事也不得借默认游戏查询错表。"""
    s = _store(tmp_path)
    with pytest.raises(ValueError, match="game_id"):
        s.contest_bracket(999_999)
    s.close()


def test_contest_entries_named(tmp_path):
    s = _store(tmp_path)
    o = s.create_user("org", "o@ex.com", "x", role="organizer")
    u = s.create_user("alice", "a@ex.com", "x")
    b = s.create_bot(u["id"], "botA", binary_path="/tmp", format="elf", game_id="holdem")
    cid = s.create_contest("Cup", organizer_id=o["id"], game_id="holdem")["id"]
    s.add_entry(cid, u["id"], b["id"])
    ents = s.contest_entries_named(cid)
    assert len(ents) == 1
    assert ents[0]["bot_name"] == "botA"
    assert ents[0]["owner_name"] == "alice"
    s.close()


def test_bracket_includes_match_winner(tmp_path):
    s = _store(tmp_path)
    o = s.create_user("org", "o@ex.com", "x", role="organizer")
    u1 = s.create_user("alice", "a@ex.com", "x")
    u2 = s.create_user("bob", "b@ex.com", "x")
    b1 = s.create_bot(u1["id"], "botA", binary_path="/tmp", format="elf", game_id="holdem")
    b2 = s.create_bot(u2["id"], "botB", binary_path="/tmp", format="elf", game_id="holdem")
    cid = s.create_contest("Cup", organizer_id=o["id"], game_id="holdem")["id"]
    s.add_entry(cid, u1["id"], b1["id"])
    s.add_entry(cid, u2["id"], b2["id"])
    # 建对局 + 对阵关联
    mid = "20260101-test1"
    s.create_match(mid, bot_a_id=b1["id"], bot_b_id=b2["id"], owner_id=o["id"])
    s.update_match(mid, status="completed", winner=0)
    s._conn.execute(
        "INSERT INTO contest_pairings(contest_id,round_num,bot_a_id,bot_b_id,match_id,status,stage_idx,stage_key) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (cid, 1, b1["id"], b2["id"], mid, "completed", 0, "rr"),
    )
    s._conn.commit()
    br = s.contest_bracket(cid)
    assert br[0]["match_winner"] == 0
    s.close()


def test_bracket_endpoint_and_detail_named(tmp_path):
    app = create_app(db_path=str(tmp_path / "app.db"))
    store = app.state.store
    o = store.create_user("org", "o@ex.com", hash_password("pw123456"), role="organizer")
    store.update_user(o["id"], email_verified=1)
    u1 = store.create_user("alice", "a@ex.com", hash_password("pw123456"))
    store.update_user(u1["id"], email_verified=1)
    u2 = store.create_user("bob", "b@ex.com", hash_password("pw123456"))
    store.update_user(u2["id"], email_verified=1)
    b1 = store.create_bot(u1["id"], "botA", binary_path="/tmp", format="elf", game_id="holdem")
    b2 = store.create_bot(u2["id"], "botB", binary_path="/tmp", format="elf", game_id="holdem")
    _, tok = app.state.auth.authenticate("org", "pw123456")
    _, atok = app.state.auth.authenticate("alice", "pw123456")
    _, btok = app.state.auth.authenticate("bob", "pw123456")
    c = TestClient(app)
    h = {"Authorization": f"Bearer {tok}"}
    ah = {"Authorization": f"Bearer {atok}"}
    bh = {"Authorization": f"Bearer {btok}"}
    # 建赛事 + 报名（alice + bob 各一）+ 开始（round_robin 至少需 2 报名）
    r = c.post("/api/contests", json={"title": "Cup", "template_id": "holdem_rr"}, headers=h)
    cid = r.json()["contest"]["id"]
    c.post(f"/api/contests/{cid}/open", headers=h)
    c.post(f"/api/contests/{cid}/register", json={"bot_id": b1["id"]}, headers=ah)
    c.post(f"/api/contests/{cid}/register", json={"bot_id": b2["id"]}, headers=bh)
    # bracket 端点（无对阵也应有报名）
    r = c.get(f"/api/contests/{cid}/bracket")
    assert r.status_code == 200 and "pairings" in r.json()
    # detail 含 named entries
    r = c.get(f"/api/contests/{cid}")
    ents = r.json()["entries"]
    assert len(ents) >= 2, f"entries={len(ents)}"
    assert "bot_name" in ents[0]
