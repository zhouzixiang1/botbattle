"""预赛/决赛 P2：官方排名 + 破同分 + 导出测试。

验证：
1. compute_official_ranking：破同分链（points→buchholz_cut1→sonneborn→h2h→...）
2. 全员唯一连续 rank（1..N）
3. merge_replace_top：决赛合成榜（1..8 取 Top8，9..M 取 Stage1 未晋级）
4. 赛事 finished 后 official_results_ready=1 + list_official_results
5. /api/contests/{id}/official-results 导出 csv/json
"""
from __future__ import annotations

from bzplat.backend.contests import ranking
from bzplat.backend.store import Store


def _store(tmp_path):
    return Store(str(tmp_path / "p2.db"))


def test_compute_ranking_unique_continuous_ranks():
    """rank 唯一连续 1..N（无并列）。"""
    standings = [
        {"entry_id": 1, "bot_id": 10, "user_id": 100, "points": 6.0, "net_chips": 100, "seed": 1},
        {"entry_id": 2, "bot_id": 20, "user_id": 200, "points": 6.0, "net_chips": 50, "seed": 2},
        {"entry_id": 3, "bot_id": 30, "user_id": 300, "points": 3.0, "net_chips": 0, "seed": 3},
    ]
    rows = ranking.compute_official_ranking(standings, [], {})
    ranks = [r["rank"] for r in rows]
    assert ranks == [1, 2, 3], f"rank 应唯一连续 1..N，实际 {ranks}"


def test_tiebreak_buchholz_breaks_tie():
    """同分时按 buchholz 破同分（对手强者排前）。

    4 个 entry：e1/e2 同分=6（争冠），e3=3（中），e4=0（弱）。
    e1 只打了 e4（弱）→ buchholz 低；e2 打了 e3（中）→ buchholz 高。
    """
    standings = [
        {"entry_id": 1, "bot_id": 10, "user_id": 100, "points": 6.0, "net_chips": 0, "seed": 1},
        {"entry_id": 2, "bot_id": 20, "user_id": 200, "points": 6.0, "net_chips": 0, "seed": 2},
        {"entry_id": 3, "bot_id": 30, "user_id": 300, "points": 3.0, "net_chips": 0, "seed": 3},
        {"entry_id": 4, "bot_id": 40, "user_id": 400, "points": 0.0, "net_chips": 0, "seed": 4},
    ]
    # e1 只打 e4（弱）；e2 只打 e3（中）—— e2 对手分更高
    pairings = [
        {"entry_a_id": 1, "entry_b_id": 4, "match_id": "m1", "bot_a_id": 10, "bot_b_id": 40},
        {"entry_a_id": 2, "entry_b_id": 3, "match_id": "m2", "bot_a_id": 20, "bot_b_id": 30},
    ]
    matches = {
        "m1": {"status": "completed", "winner": 0, "result": {"deltas": [100, -100]}},
        "m2": {"status": "completed", "winner": 0, "result": {"deltas": [100, -100]}},
    }
    rows = ranking.compute_official_ranking(standings, pairings, matches)
    by_entry = {r["entry_id"]: r for r in rows}
    # e1 对手 e4(0) → buchholz=0；e2 对手 e3(3) → buchholz=3
    assert by_entry[2]["tiebreaks"]["buchholz"] > by_entry[1]["tiebreaks"]["buchholz"]
    assert by_entry[2]["rank"] == 1, "buchholz 高者应排前（同分时对手强者排前）"
    assert by_entry[1]["rank"] == 2


def test_merge_replace_top():
    """决赛合成榜：1..scope 取 stage2，scope+1..N 取 stage1 未晋级。"""
    stage1 = [
        {"entry_id": i, "rank": i, "bot_id": i * 10} for i in range(1, 9)  # 8 人
    ]
    stage2 = [
        {"entry_id": 1, "rank": 1, "bot_id": 10},
        {"entry_id": 3, "rank": 2, "bot_id": 30},
        {"entry_id": 5, "rank": 3, "bot_id": 50},
        {"entry_id": 7, "rank": 4, "bot_id": 70},
        {"entry_id": 2, "rank": 5, "bot_id": 20},
        {"entry_id": 4, "rank": 6, "bot_id": 40},
        {"entry_id": 6, "rank": 7, "bot_id": 60},
        {"entry_id": 8, "rank": 8, "bot_id": 80},
    ]
    merged = ranking.merge_replace_top(stage1, stage2, scope=8)
    # 全是 Top8，scope=8 → stage2 全取，stage1 全晋级故 rest 空
    assert len(merged) == 8
    ranks = [r["rank"] for r in merged]
    assert ranks == [1, 2, 3, 4, 5, 6, 7, 8]
    # stage2 的第 1 名（entry1）应在 merged 第 1
    assert merged[0]["entry_id"] == 1


def test_persist_and_list_official_results(tmp_path):
    """落库 + list_official_results 按 rank 升序。"""
    s = _store(tmp_path)
    u = s.create_user("org", "o@e.com", "x", role="organizer")["id"]
    b1 = s.create_bot(u, "rb1", binary_path="/tmp", format="elf", game_id="holdem")["id"]
    c = s.create_contest("P2持久", organizer_id=u, game_id="holdem")["id"]
    ranking.persist_official_results(
        s, c,
        [
            {"entry_id": 1, "rank": 2, "bot_id": b1, "user_id": u,
             "tiebreaks": {"points": 3}},
            {"entry_id": 2, "rank": 1, "bot_id": b1, "user_id": u,
             "tiebreaks": {"points": 6}},
        ],
    )
    rows = s.list_official_results(c)
    assert [r["rank"] for r in rows] == [1, 2], "应按 rank 升序"
    assert int(s.get_contest(c)["official_results_ready"]) == 1
    s.close()


def test_official_results_endpoint_csv_json(tmp_path):
    """/api/contests/{id}/official-results 导出 csv + json。"""
    from bzplat.backend.crypto import hash_password
    from fastapi.testclient import TestClient

    from bzplat.backend.main import create_app

    app = create_app(db_path=str(tmp_path / "app.db"))
    store = app.state.store
    o = store.create_user("org2", "o2@ex.com", hash_password("pw123456"), role="organizer")
    store.update_user(o["id"], email_verified=1)
    b = store.create_bot(o["id"], "rb", binary_path="/tmp", format="elf", game_id="holdem")["id"]
    c = store.create_contest("P2导出", organizer_id=o["id"], game_id="holdem")["id"]
    ranking.persist_official_results(
        store, c,
        [{"entry_id": 1, "rank": 1, "bot_id": b, "user_id": o["id"],
          "tiebreaks": {"points": 6, "buchholz_cut1": 3, "sonneborn_berger": 3}}],
    )
    client = TestClient(app)
    r = client.get(f"/api/contests/{c}/official-results?format=json")
    assert r.status_code == 200
    data = r.json()
    assert data["ready"] is True
    assert len(data["results"]) == 1
    r2 = client.get(f"/api/contests/{c}/official-results?format=csv")
    assert r2.status_code == 200
    assert "text/csv" in r2.headers.get("content-type", "")
    assert "rank" in r2.text


def test_official_results_not_ready_returns_409(tmp_path):
    """赛事未 finished/未落库 → 409。"""
    from bzplat.backend.crypto import hash_password
    from fastapi.testclient import TestClient

    from bzplat.backend.main import create_app

    app = create_app(db_path=str(tmp_path / "app2.db"))
    store = app.state.store
    o = store.create_user("org3", "o3@ex.com", hash_password("pw123456"), role="organizer")
    store.update_user(o["id"], email_verified=1)
    c = store.create_contest("P2未就绪", organizer_id=o["id"], game_id="holdem")["id"]
    client = TestClient(app)
    r = client.get(f"/api/contests/{c}/official-results")
    assert r.status_code == 409
