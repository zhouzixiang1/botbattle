"""预赛/决赛 P0：entry 身份改键测试。

验证：
1. 排名/积分键为 contest_entry.id（换 Bot 不丢历史分）
2. 删 Bot 后 entry/pairing/stage_results 保留（bot_id NULL，FK SET NULL）
3. pairing 存 entry_a_id/entry_b_id（生成时快照）
"""
from __future__ import annotations

import pytest

from bzplat.backend.store import Store


def _store(tmp_path):
    return Store(str(tmp_path / "p0.db"))


def test_pairings_have_entry_id_columns(tmp_path):
    """contest_pairings 表有 entry_a_id/entry_b_id 列（P0 迁移）。"""
    s = _store(tmp_path)
    with s._tx() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(contest_pairings)")}
    s.close()
    assert "entry_a_id" in cols and "entry_b_id" in cols


def test_stage_results_has_entry_id_and_unique(tmp_path):
    """contest_stage_results 有 entry_id 列，唯一键含 entry_id。"""
    s = _store(tmp_path)
    with s._tx() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(contest_stage_results)")}
        # 唯一键含 entry_id（UNIQUE(contest_id, stage_idx, entry_id)）
        idxs = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='contest_stage_results'").fetchone()
    s.close()
    assert "entry_id" in cols
    assert "entry_id" in (idxs["sql"] if idxs else "")


def test_bot_fk_is_set_null(tmp_path):
    """contest_entries/pairings/stage_results 的 bot FK 都是 ON DELETE SET NULL（删 bot 留成绩）。"""
    s = _store(tmp_path)
    with s._tx() as c:
        for tbl in ("contest_entries", "contest_pairings", "contest_stage_results"):
            fks = c.execute(f"PRAGMA foreign_key_list({tbl})").fetchall()
            bot_fks = [fk for fk in fks if fk["table"] == "bots"]
            assert bot_fks, f"{tbl} 应有 bots FK"
            for fk in bot_fks:
                assert (fk["on_delete"] or "").upper() == "SET NULL", (
                    f"{tbl}.bot FK 应 SET NULL，实际 {fk['on_delete']}"
                )
    s.close()


def test_standings_keyed_by_entry_id(tmp_path):
    """standings 返回结构含 entry_id（P0 改键）。"""
    s = _store(tmp_path)
    from bzplat.backend.contests.manager import ContestManager

    u = s.create_user("org1", "o@e.com", "x", role="organizer")["id"]
    ua = s.create_user("p0a", "a@e.com", "x")["id"]
    ub = s.create_user("p0b", "b@e.com", "x")["id"]
    ba = s.create_bot(ua, "botA", binary_path="/tmp", format="elf", game_id="holdem")["id"]
    bb = s.create_bot(ub, "botB", binary_path="/tmp", format="elf", game_id="holdem")["id"]
    c = s.create_contest(
        "P0键测试", organizer_id=u, game_id="holdem",
        stages_json='[{"key":"s1","type":"double_round_robin","scoring":"poker_3_1_0"}]',
    )["id"]
    s.add_contest_entry(c, ua, ba)
    s.add_contest_entry(c, ub, bb)
    class _FakeOrch:
        pass
    cm = ContestManager(s, _FakeOrch())  # type: ignore
    standings = cm.standings(c)
    assert len(standings) == 2
    for row in standings:
        assert "entry_id" in row, "standings 行应含 entry_id（P0 改键）"
        assert "bot_id" in row  # bot_id 仍带（展示用）
    s.close()


def test_swap_bot_keeps_history_points(tmp_path):
    """换 Bot 后历史积分不丢（P0 核心价值：entry 身份为键）。

    场景：entry1 用 botA 打 2 场（胜），换 Bot 为 botA2 → standings 仍累计原 entry1 的分。
    """
    s = _store(tmp_path)
    from bzplat.backend.contests.manager import ContestManager

    u = s.create_user("org2", "o2@e.com", "x", role="organizer")["id"]
    ua = s.create_user("sw1", "s1@e.com", "x")["id"]
    ub = s.create_user("sw2", "s2@e.com", "x")["id"]
    ba = s.create_bot(ua, "swbotA", binary_path="/tmp", format="elf", game_id="holdem")["id"]
    bb = s.create_bot(ub, "swbotB", binary_path="/tmp", format="elf", game_id="holdem")["id"]
    c = s.create_contest(
        "P0换Bot", organizer_id=u, game_id="holdem",
        stages_json='[{"key":"s1","type":"double_round_robin","scoring":"poker_3_1_0"}]',
    )["id"]
    s.add_contest_entry(c, ua, ba)
    s.add_contest_entry(c, ub, bb)
    # 取 entry1 的 id
    e1 = s.get_entry(c, ua)
    e2 = s.get_entry(c, ub)
    # 造 2 场已完成对局：entry1(botA) 都赢（winner=0=bot_a 侧）
    # 用 standings 走 pairing.entry_a_id → 需先建 pairing
    s.add_contest_pairing(c, ba, bb, stage_idx=0, stage_key="s1", round_num=1,
                          entry_a_id=e1["id"], entry_b_id=e2["id"])
    s.add_contest_pairing(c, ba, bb, stage_idx=0, stage_key="s1", round_num=2,
                          entry_a_id=e1["id"], entry_b_id=e2["id"])
    # 建对应 match（completed, botA 赢）
    for i, p in enumerate(s.list_contest_pairings(c, stage_idx=0)):
        mid = f"p0swap-{i}"
        s.create_match(
            mid,
            ba,
            bb,
            game_id="holdem",
            contest_id=c,
            match_type="contest",
            match_config={},
        )
        s.update_match(mid, status="completed", winner=0,
                       result={"rounds_played": 2, "deltas": [100, -100]},
                       reason="completed")
        s.update_contest_pairing(p["id"], match_id=mid, status="running")
    class _FakeOrch:
        pass
    cm = ContestManager(s, _FakeOrch())  # type: ignore
    before = {r["entry_id"]: r["points"] for r in cm.standings(c)}
    assert before[e1["id"]] == 6.0  # 2 胜 × poker_3_1_0 = 6
    # 换 Bot：entry1.bot_id 改为新 botA2（但 pairing.entry_a_id 不变）
    ba2 = s.create_bot(ua, "swbotA2", binary_path="/tmp", format="elf", game_id="holdem")["id"]
    s.update_entry(c, ua, bot_id=ba2)
    # standings 仍应累计 entry1 的 6 分（因为键是 entry_id，pairing.entry_a_id 仍指向 entry1）
    after = {r["entry_id"]: r["points"] for r in cm.standings(c)}
    assert after[e1["id"]] == 6.0, (
        f"换 Bot 后 entry1 应保留 6 分（entry 身份为键），实际 {after[e1['id']]}"
    )
    s.close()


def test_delete_bot_preserves_contest_data(tmp_path):
    """删 Bot 后 entry/pairing/stage_results 保留（bot_id NULL，FK SET NULL）。"""
    s = _store(tmp_path)
    u = s.create_user("org3", "o3@e.com", "x", role="organizer")["id"]
    ua = s.create_user("del1", "d1@e.com", "x")["id"]
    ba = s.create_bot(ua, "delbotA", binary_path="/tmp", format="elf", game_id="holdem")["id"]
    c = s.create_contest("P0删Bot", organizer_id=u, game_id="holdem",
                         stages_json='[{"key":"s1","type":"round_robin","scoring":"poker_3_1_0"}]')["id"]
    s.add_contest_entry(c, ua, ba)
    e1 = s.get_entry(c, ua)
    # 建 pairing + stage_result
    s.add_contest_pairing(c, ba, ba, stage_idx=0, stage_key="s1",
                          entry_a_id=e1["id"], entry_b_id=e1["id"])
    s.upsert_stage_result(c, 0, e1["id"], bot_id=ba, points=3.0, wins=1)
    # 删 bot
    s.delete_bot(ba)
    # entry 保留（bot_id NULL）
    e1b = s.get_entry(c, ua)
    assert e1b is not None, "删 bot 后 entry 应保留"
    assert e1b["bot_id"] is None, "删 bot 后 entry.bot_id 应 NULL"
    # pairing 保留
    ps = s.list_contest_pairings(c, stage_idx=0)
    assert len(ps) == 1, "删 bot 后 pairing 应保留"
    # stage_result 保留
    srs = s.list_stage_results(c)
    assert len(srs) == 1, "删 bot 后 stage_result 应保留"
    s.close()


def test_bot_active_references_detects_pending_match(tmp_path):
    """store 层：bot_active_references 正确检测 pending 对局（业务规则在 API 层调用）。"""
    s = _store(tmp_path)
    u = s.create_user("user1a", "u1a@e.com", "x")["id"]
    b = s.create_bot(u, "bot1a", binary_path="/tmp", format="elf", game_id="holdem")["id"]
    s.create_match("m-active", b, b, owner_id=u, match_config={},
                   match_type="challenge", game_id="holdem")
    refs = s.bot_active_references(b)
    assert refs["matches"] >= 1, "pending 对局应被检测到"
    # 直接 store.delete_bot 仍允许（store 层不拦截，FK SET NULL 保历史）——业务规则在 API 层
    assert s.delete_bot(b) is True
    s.close()


def test_bot_active_references_detects_running_contest(tmp_path):
    """store 层：running 赛事的报名/对阵被检测，finished 的不阻拦。"""
    s = _store(tmp_path)
    org = s.create_user("orgA", "oa@e.com", "x", role="organizer")["id"]
    u = s.create_user("user2a", "u2a@e.com", "x")["id"]
    b = s.create_bot(u, "bot2a", binary_path="/tmp", format="elf", game_id="holdem")["id"]
    c = s.create_contest("运行中赛A", organizer_id=org, game_id="holdem",
                         stages_json='[{"key":"s1","type":"round_robin","scoring":"poker_3_1_0"}]')["id"]
    s.add_contest_entry(c, u, b)
    s.add_contest_pairing(c, b, b, stage_idx=0, stage_key="s1")
    s.update_contest(c, status="running")  # 进行中
    refs = s.bot_active_references(b)
    assert refs["pairings"] >= 1, "running 赛事的报名/对阵应被检测到"
    # finished 后不再阻拦
    s.update_contest(c, status="finished")
    refs2 = s.bot_active_references(b)
    assert refs2["matches"] == 0 and refs2["pairings"] == 0, "finished 赛事历史不阻拦"
    s.close()


def test_admin_delete_bot_preserves_active_and_historical_identity(tmp_path):
    """管理员只能停用已参赛 Bot，终局后也不能抹掉公开身份。"""
    from bzplat.backend.crypto import hash_password
    from bzplat.backend.main import create_app
    from fastapi.testclient import TestClient

    app = create_app(db_path=str(tmp_path / "delapi.db"))
    store = app.state.store
    admin = store.create_user("deladmin", "da@e.com", hash_password("pw123456"), role="admin")
    store.update_user(admin["id"], email_verified=1)
    u = store.create_user("deluser", "du@e.com", hash_password("pw123456"))["id"]
    store.update_user(u, email_verified=1)
    b = store.create_bot(u, "delbotX", binary_path="/tmp", format="elf", game_id="holdem")["id"]
    # pending 对局 → 活跃引用
    store.create_match("m-api-active", b, b, owner_id=u, match_config={},
                       match_type="challenge", game_id="holdem")
    _, atok = app.state.auth.authenticate("deladmin", "pw123456")
    h = {"Authorization": f"Bearer {atok}"}
    client = TestClient(app)
    r = client.delete(f"/api/admin/bots/{b}", headers=h)
    assert r.status_code == 409, f"活跃引用应 409 拒绝，实际 {r.status_code} {r.text}"
    # bot 仍在（未删）
    assert store.get_bot(b) is not None
    store.update_match(
        "m-api-active",
        status="completed",
        winner=0,
        result={"deltas": [1, -1]},
        reason="completed",
    )
    r_done = client.delete(f"/api/admin/bots/{b}", headers=h)
    assert r_done.status_code == 409, r_done.text
    assert "历史" in r_done.json()["detail"]
    assert store.get_bot(b) is not None


def test_admin_delete_user_rejects_active_bot_references(tmp_path):
    """删用户不得借 users→bots CASCADE 绕过活跃 Bot 引用保护。"""
    from bzplat.backend.crypto import hash_password
    from bzplat.backend.main import create_app
    from fastapi.testclient import TestClient

    app = create_app(
        db_path=str(tmp_path / "delete-user-active.db"),
        upload_root=tmp_path / "uploads-active",
    )
    store = app.state.store
    admin = store.create_user(
        "userdeladmin", "userdeladmin@example.com", hash_password("pw123456"), role="admin"
    )
    victim = store.create_user(
        "userdelvictim", "userdelvictim@example.com", hash_password("pw123456")
    )
    store.update_user(admin["id"], email_verified=1)
    bot = store.create_bot(
        victim["id"], "victimbot", binary_path="/tmp/victim", format="elf", game_id="gomoku"
    )
    store.create_match(
        "delete-user-active-match",
        bot["id"],
        bot["id"],
        owner_id=victim["id"],
        match_type="challenge",
        game_id="gomoku",
    )

    _, token = app.state.auth.authenticate("userdeladmin", "pw123456")
    response = TestClient(app).delete(
        f"/api/admin/users/{victim['id']}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 409, response.text
    assert store.get_user(victim["id"]) is not None
    assert store.get_bot(bot["id"]) is not None
    match = store.get_match("delete-user-active-match")
    assert match is not None and match["status"] == "pending"
    assert match["bot_a_id"] == bot["id"] and match["bot_b_id"] == bot["id"]
    store.update_match(
        "delete-user-active-match", status="completed", reason="five", winner=0,
        result={"rounds_played": 1, "deltas": [1, -1], "normalized_delta": 1},
    )
    historical = TestClient(app).delete(
        f"/api/admin/users/{victim['id']}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert historical.status_code == 409, historical.text
    assert "历史" in historical.json()["detail"]
    assert store.get_user(victim["id"]) is not None


@pytest.mark.parametrize("source", ["manual", "human"])
def test_delete_user_if_safe_rejects_queued_execution_identity(tmp_path, source):
    """claim 前尚无 Match，也不能删掉持久队列中的 Bot owner 或真人。"""
    s = _store(tmp_path)
    victim = s.create_user(
        f"queued-{source}", f"queued-{source}@example.com", "hash"
    )
    bot = s.create_bot(
        victim["id"], f"queued-{source}-bot", binary_path="/tmp/queued",
        format="elf", game_id="pencil",
    )
    if source == "human":
        values = (
            f"req-{source}", source, victim["id"], "human", bot["id"],
            bot["id"], victim["id"], 1, 1,
        )
    else:
        values = (
            f"req-{source}", source, victim["id"], "challenge", bot["id"],
            bot["id"], None, None, 2,
        )
    with s._tx() as c:
        c.execute(
            "INSERT INTO execution_jobs("
            "public_id,source,status,priority,owner_user_id,game_id,match_type,"
            "bot_a_id,bot_b_id,human_user_id,human_seat,match_config,rated,"
            "rating_reason,sandbox_units,created_at) "
            "VALUES(?,?,'queued',50,?,'pencil',?,?,?,?,?,'{}',0,'test',?,?)",
            (*values, "2026-08-11T00:00:00+00:00"),
        )

    result = s.delete_user_if_safe(victim["id"])
    assert result["deleted"] is False
    assert result["blockers"]["active_execution_jobs"] == 1
    assert s.get_user(victim["id"]) is not None
    assert s.get_bot(bot["id"]) is not None
    s.close()


def test_admin_delete_human_player_rejects_foreign_bot_match(tmp_path):
    """人类使用他人 Bot 时也按 owner_id/human_user_id 阻止删除参与者。"""
    from bzplat.backend.crypto import hash_password
    from bzplat.backend.main import create_app
    from fastapi.testclient import TestClient

    app = create_app(
        db_path=str(tmp_path / "delete-human-player.db"),
        upload_root=tmp_path / "uploads-human-player",
    )
    store = app.state.store
    admin = store.create_user(
        "humandeladmin", "humandeladmin@example.com", hash_password("pw123456"), role="admin"
    )
    human = store.create_user(
        "humanvictim", "humanvictim@example.com", hash_password("pw123456")
    )
    bot_owner = store.create_user(
        "foreignowner", "foreignowner@example.com", hash_password("pw123456")
    )
    store.update_user(admin["id"], email_verified=1)
    foreign_bot = store.create_bot(
        bot_owner["id"], "foreignbot", binary_path="/tmp/foreign", format="elf", game_id="gomoku"
    )
    store.create_match(
        "delete-human-active-match",
        foreign_bot["id"],
        foreign_bot["id"],
        owner_id=human["id"],
        human_user_id=human["id"],
        human_seat=1,
        match_type="human",
        game_id="gomoku",
    )
    store.update_match("delete-human-active-match", status="running")

    _, token = app.state.auth.authenticate("humandeladmin", "pw123456")
    response = TestClient(app).delete(
        f"/api/admin/users/{human['id']}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 409, response.text
    assert store.get_user(human["id"]) is not None
    match = store.get_match("delete-human-active-match")
    assert match is not None and match["status"] == "running"
    assert match["owner_id"] == human["id"]
    assert match["human_user_id"] == human["id"]


def test_admin_delete_bot_owner_rejects_mismatched_active_entry(tmp_path):
    """即使脏名册把 Bot 绑给另一用户，也不能级联破坏 published entry。"""
    from bzplat.backend.crypto import hash_password
    from bzplat.backend.main import create_app
    from fastapi.testclient import TestClient

    app = create_app(
        db_path=str(tmp_path / "delete-mismatched-entry.db"),
        upload_root=tmp_path / "uploads-mismatched-entry",
    )
    store = app.state.store
    admin = store.create_user(
        "entrydeladmin", "entrydeladmin@example.com", hash_password("pw123456"), role="admin"
    )
    organizer = store.create_user(
        "entrydelorg", "entrydelorg@example.com", hash_password("pw123456"), role="organizer"
    )
    bot_owner = store.create_user(
        "entrybotowner", "entrybotowner@example.com", hash_password("pw123456")
    )
    entrant = store.create_user(
        "differententrant", "differententrant@example.com", hash_password("pw123456")
    )
    store.update_user(admin["id"], email_verified=1)
    bot = store.create_bot(
        bot_owner["id"], "entryownedbot", binary_path="/tmp/entry", format="elf", game_id="holdem"
    )
    contest = store.create_contest(
        "mismatched entry guard", organizer_id=organizer["id"], game_id="holdem"
    )
    store.add_contest_entry(contest["id"], entrant["id"], bot["id"])
    store.update_contest(contest["id"], status="published")

    _, token = app.state.auth.authenticate("entrydeladmin", "pw123456")
    response = TestClient(app).delete(
        f"/api/admin/users/{bot_owner['id']}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 409, response.text
    assert store.get_user(bot_owner["id"]) is not None
    entry = store.get_entry(contest["id"], entrant["id"])
    assert entry is not None and entry["bot_id"] == bot["id"]


def test_admin_delete_user_rejects_contest_organizer_without_500(tmp_path):
    """contests.organizer_id 是 NO ACTION；端点应给 409 而不是泄漏 IntegrityError 500。"""
    from bzplat.backend.crypto import hash_password
    from bzplat.backend.main import create_app
    from fastapi.testclient import TestClient

    app = create_app(
        db_path=str(tmp_path / "delete-organizer.db"),
        upload_root=tmp_path / "uploads-organizer",
    )
    store = app.state.store
    admin = store.create_user(
        "orgdeladmin", "orgdeladmin@example.com", hash_password("pw123456"), role="admin"
    )
    organizer = store.create_user(
        "orgdelvictim",
        "orgdelvictim@example.com",
        hash_password("pw123456"),
        role="organizer",
    )
    store.update_user(admin["id"], email_verified=1)
    contest = store.create_contest(
        "organizer delete guard", organizer_id=organizer["id"], game_id="gomoku"
    )

    _, token = app.state.auth.authenticate("orgdeladmin", "pw123456")
    response = TestClient(app).delete(
        f"/api/admin/users/{organizer['id']}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 409, response.text
    assert store.get_user(organizer["id"]) is not None
    assert store.get_contest(contest["id"]) is not None


def test_admin_delete_user_purges_unreferenced_bot_files(tmp_path):
    """安全硬删仍可用，并清理级联删除 Bot 的磁盘目录。"""
    from bzplat.backend.crypto import hash_password
    from bzplat.backend.main import create_app
    from fastapi.testclient import TestClient

    upload_root = tmp_path / "uploads-safe"
    app = create_app(
        db_path=str(tmp_path / "delete-user-safe.db"),
        upload_root=upload_root,
    )
    store = app.state.store
    admin = store.create_user(
        "safedeladmin", "safedeladmin@example.com", hash_password("pw123456"), role="admin"
    )
    victim = store.create_user(
        "safedelvictim", "safedelvictim@example.com", hash_password("pw123456")
    )
    store.update_user(admin["id"], email_verified=1)
    bot = store.create_bot(
        victim["id"], "safedelbot", binary_path="/tmp/safe", format="elf", game_id="pencil"
    )
    bot_dir = upload_root / str(bot["id"]) / "v1"
    bot_dir.mkdir(parents=True)
    (bot_dir / "bot.bin").write_bytes(b"qa")

    _, token = app.state.auth.authenticate("safedeladmin", "pw123456")
    response = TestClient(app).delete(
        f"/api/admin/users/{victim['id']}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text
    assert store.get_user(victim["id"]) is None
    assert store.get_bot(bot["id"]) is None
    assert not (upload_root / str(bot["id"])).exists()
