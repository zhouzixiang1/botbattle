"""赛事对阵图 + 显示 Bot 名测试（PR-6）。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bzplat.backend.api_routes import _public_contest_pairings
from bzplat.backend.crypto import hash_password
from bzplat.backend.main import create_app
from bzplat.backend.store import Store


def _store(tmp_path) -> Store:
    return Store(str(tmp_path / "cb.db"))


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({}, True),
        ({"entry_b_id": 17}, False),
        ({"bot_b_id": 23}, False),
        ({"match_id": "historical-match"}, False),
        ({"status": "pending"}, False),
    ],
)
def test_public_pairing_bye_requires_all_four_authoritative_conditions(
    override, expected
):
    row = {
        "id": 1,
        "entry_b_id": None,
        "bot_b_id": None,
        "match_id": None,
        "status": "completed",
        **override,
    }
    assert _public_contest_pairings([row])[0]["is_bye"] is expected


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
    normal_pairing = store.add_pairing(
        cid, b1["id"], b2["id"], stage_key="rr",
        entry_a_id=store.get_entry(cid, u1["id"])["id"],
        entry_b_id=store.get_entry(cid, u2["id"])["id"],
        bot_a_version_id=101, bot_b_version_id=102, pairing_seed=777,
    )
    bye_pairing = store.add_pairing(
        cid, b1["id"], None, stage_key="rr", status="completed",
        entry_a_id=store.get_entry(cid, u1["id"])["id"],
    )
    # detail 与 bracket 端点使用同一公开参赛者契约。
    r = c.get(f"/api/contests/{cid}/bracket")
    assert r.status_code == 200 and "pairings" in r.json()
    bracket_rows = {row["id"]: row for row in r.json()["pairings"]}
    bracket_pairing = bracket_rows[normal_pairing["id"]]
    public_pairing_fields = {
        "id", "round_num", "bot_a_id", "bot_b_id", "scheduled_at",
        "match_id", "status", "stage_idx", "stage_key", "group_id",
        "bracket_slot", "bot_a_name", "bot_a_display", "bot_b_name",
        "bot_b_display", "owner_a_name", "owner_a_display",
        "owner_b_name", "owner_b_display", "match_winner", "is_bye",
    }
    assert set(bracket_pairing) <= public_pairing_fields
    assert bracket_pairing["owner_a_name"] == "alice"
    assert bracket_pairing["owner_b_name"] == "bob"
    for internal in (
        "contest_id", "entry_a_id", "entry_b_id", "bot_a_version_id",
        "bot_b_version_id", "pairing_seed", "published_at", "color_first",
    ):
        assert internal not in bracket_pairing
    assert bracket_rows[bye_pairing["id"]]["is_bye"] is True
    # detail 含 named entries
    r = c.get(f"/api/contests/{cid}")
    ents = r.json()["entries"]
    assert len(ents) >= 2, f"entries={len(ents)}"
    assert "bot_name" in ents[0]
    detail_rows = {row["id"]: row for row in r.json()["pairings"]}
    detail_pairing = detail_rows[normal_pairing["id"]]
    assert detail_pairing["owner_a_name"] == "alice"
    assert detail_pairing["owner_b_name"] == "bob"
    assert detail_pairing["is_bye"] is False
    # 公开 pairings 会裁掉 entry ids，但内部阶段读模型仍须用原始关联键，
    # 否则阶段排名会把真实参赛者全部过滤为空。
    stage_rows = [
        row
        for stage in r.json()["stage_standings"]
        for row in stage["rows"]
    ]
    assert {row["owner_name"] for row in stage_rows} == {"alice", "bob"}

    # 低层历史清理会把 bot_b_id SET NULL，但 entry 身份仍在；公开层不能把
    # 这种历史误报成轮空，也应尽量从 entry 恢复所属用户。
    assert store.delete_bot(b2["id"])
    deleted_rows = {
        row["id"]: row
        for row in c.get(f"/api/contests/{cid}/bracket").json()["pairings"]
    }
    deleted_bot_pairing = deleted_rows[normal_pairing["id"]]
    assert deleted_bot_pairing["bot_b_id"] is None
    assert deleted_bot_pairing["is_bye"] is False
    assert deleted_bot_pairing["owner_b_name"] == "bob"


def test_legacy_pairing_recovers_unique_entries_without_guessing_bye(tmp_path):
    """旧 pairing 无 entry ids 时只读恢复唯一报名身份，双 Bot 不误报轮空。"""
    app = create_app(db_path=str(tmp_path / "legacy-pairing.db"))
    store = app.state.store
    organizer = store.create_user("legacy-org", "lo@example.com", "hash", role="organizer")
    alice = store.create_user("legacy-alice", "la@example.com", "hash")
    bob = store.create_user("legacy-bob", "lb@example.com", "hash")
    bot_a = store.create_bot(alice["id"], "legacy-a", binary_path="/tmp", format="elf", game_id="holdem")
    bot_b = store.create_bot(bob["id"], "legacy-b", binary_path="/tmp", format="elf", game_id="holdem")
    contest = store.create_contest(
        "Legacy", organizer_id=organizer["id"], game_id="holdem",
        stages_json='[{"key":"rr","type":"round_robin","scoring":"poker_3_1_0"}]',
    )
    store.update_contest(contest["id"], status="open")
    store.add_entry(contest["id"], alice["id"], bot_a["id"])
    store.add_entry(contest["id"], bob["id"], bot_b["id"])
    pairing = store.add_pairing(
        contest["id"], bot_a["id"], bot_b["id"], stage_key="rr",
        entry_a_id=None, entry_b_id=None,
    )
    match_id = "legacy-completed-pairing"
    store.create_match(
        match_id, bot_a["id"], bot_b["id"], game_id="holdem",
        contest_id=contest["id"], match_type="contest",
    )
    store.update_match(
        match_id, status="completed", winner=1, reason="completed",
        result={"rounds_played": 1, "deltas": [-5, 5], "normalized_delta": -0.05},
    )
    store.update_contest_pairing(pairing["id"], match_id=match_id, status="completed")

    response = TestClient(app).get(f"/api/contests/{contest['id']}")
    assert response.status_code == 200
    public = next(row for row in response.json()["pairings"] if row["id"] == pairing["id"])
    assert public["is_bye"] is False
    stage_rows = response.json()["stage_standings"][0]["rows"]
    assert {row["owner_name"] for row in stage_rows} == {"legacy-alice", "legacy-bob"}
    live_points = {row["owner_name"]: row["points"] for row in stage_rows}
    assert live_points == {"legacy-alice": 0.0, "legacy-bob": 3.0}

    # 旧阶段快照同样可能只有 bot_id；唯一报名映射须在读边界恢复 entry_id。
    with store._tx() as conn:
        conn.executemany(
            "INSERT INTO contest_stage_results("
            "contest_id,stage_idx,stage_key,entry_id,bot_id,points,wins,draws,"
            "losses,delta_total,group_id,payload_json) "
            "VALUES(?,0,'rr',NULL,?,?,?,?,?,?, '', '{}')",
            [
                (contest["id"], bot_a["id"], 7.0, 2, 1, 0, 12),
                (contest["id"], bot_b["id"], 4.0, 1, 1, 1, -12),
            ],
        )
    persisted = TestClient(app).get(f"/api/contests/{contest['id']}").json()
    persisted_rows = persisted["stage_standings"][0]["rows"]
    assert {row["owner_name"]: row["points"] for row in persisted_rows} == {
        "legacy-alice": 7.0,
        "legacy-bob": 4.0,
    }
