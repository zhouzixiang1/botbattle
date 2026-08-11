"""公开数值评分、排名资格与 Bot 可靠性投影回归。"""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from bzplat.backend.main import create_app
from bzplat.backend.runtime.config import RANKING_MIN_RATED_MATCHES
from bzplat.backend.store import Store


def _rated_bot(
    store: Store,
    *,
    username: str,
    name: str,
    game_id: str = "holdem",
    rating: float,
    rd: float = 80,
    matches: int,
) -> dict:
    user = store.create_user(username, f"{username}@example.com", "hash")
    bot = store.create_bot(
        user["id"], name, game_id=game_id, binary_path=f"/tmp/{name}", format="elf"
    )
    store.ensure_rating(bot["id"], game_id=game_id)
    store.update_rating_row(
        bot["id"],
        game_id=game_id,
        rating=rating,
        rd=rd,
        matches_played=matches,
        wins=matches,
        losses=0,
        draws=0,
    )
    return bot


def test_numeric_leaderboard_contract(tmp_path):
    app = create_app(db_path=str(tmp_path / "numeric.db"))
    store = app.state.store
    high = _rated_bot(
        store, username="high", name="high_bot", rating=1800, rd=100, matches=12
    )
    low = _rated_bot(
        store, username="low", name="low_bot", rating=1600, rd=50, matches=10
    )
    sample = _rated_bot(
        store, username="sample", name="sample_bot", rating=2300, rd=300, matches=9
    )
    store.upsert_pair_stats(high["id"], low["id"], a_wins_delta=1)
    store.upsert_pair_stats(high["id"], sample["id"], a_wins_delta=1)
    # 旧数据/人工维护可能留下反向 pair 行；“不同对手”必须按实体去重。
    store.upsert_pair_stats(low["id"], high["id"], draws_delta=1)

    with TestClient(app) as client:
        response = client.get("/api/leaderboard?game_id=holdem&page=1&per_page=20")
        assert response.status_code == 200
        body = response.json()

    assert body["ranking_min_matches"] == RANKING_MIN_RATED_MATCHES == 10
    assert body["summary"]["eligible"] == 2
    assert body["summary"]["sample"] == 1
    rows = body["leaderboard"]
    assert [row["bot_id"] for row in rows] == [high["id"], low["id"], sample["id"]]
    assert [row["rank"] for row in rows] == [1, 2, None]
    assert [row["rank_total"] for row in rows] == [2, 2, 2]
    assert [row["percentile"] for row in rows] == [100.0, 0.0, None]
    assert rows[-1]["ranking_progress"] == 0.9
    assert rows[-1]["ranking_eligible"] is False
    assert rows[0]["unique_opponents"] == 2
    assert rows[0]["confidence_low"] == 1604.0
    assert rows[0]["confidence_high"] == 1996.0

    assert all("matches_played" not in row for row in rows)


def test_leaderboard_rank_is_global_across_pages_and_ties_are_stable(tmp_path):
    app = create_app(db_path=str(tmp_path / "pages.db"))
    store = app.state.store
    bots = [
        _rated_bot(
            store,
            username=f"user_{index}",
            name=f"bot_{index}",
            rating=1700,
            matches=RANKING_MIN_RATED_MATCHES,
        )
        for index in range(12)
    ]
    with TestClient(app) as client:
        page_1 = client.get(
            "/api/leaderboard?game_id=holdem&page=1&per_page=5"
        ).json()["leaderboard"]
        page_2 = client.get(
            "/api/leaderboard?game_id=holdem&page=2&per_page=5"
        ).json()["leaderboard"]
        page_3 = client.get(
            "/api/leaderboard?game_id=holdem&page=3&per_page=5"
        ).json()["leaderboard"]

    rows = page_1 + page_2 + page_3
    assert [row["rank"] for row in rows] == list(range(1, 13))
    assert [row["bot_id"] for row in rows] == [bot["id"] for bot in bots]
    assert all(row["rank_total"] == 12 for row in rows)


def test_three_games_keep_independent_numeric_ranks(tmp_path):
    app = create_app(db_path=str(tmp_path / "games.db"))
    store = app.state.store
    bots = {
        game_id: _rated_bot(
            store,
            username=f"owner_{game_id}",
            name=f"bot_{game_id}",
            game_id=game_id,
            rating=1500 + offset,
            matches=RANKING_MIN_RATED_MATCHES,
        )
        for game_id, offset in (("holdem", 100), ("gomoku", 200), ("pencil", 300))
    }
    with TestClient(app) as client:
        for game_id, bot in bots.items():
            rows = client.get(f"/api/leaderboard?game_id={game_id}").json()["leaderboard"]
            assert len(rows) == 1
            assert rows[0]["bot_id"] == bot["id"]
            assert rows[0]["rank"] == 1
            assert rows[0]["rank_total"] == 1
            assert rows[0]["percentile"] == 100.0


def test_profile_exposes_deltas_rank_and_technical_failure_denominator(tmp_path):
    app = create_app(db_path=str(tmp_path / "profile.db"))
    store = app.state.store
    bot = _rated_bot(
        store, username="profile", name="profile_bot", rating=1700, rd=50, matches=10
    )
    opponent = _rated_bot(
        store, username="opponent", name="opponent_bot", rating=1500, matches=10
    )
    store.add_rating_history(bot["id"], 1500, 90, 0.06, 9, "old", game_id="holdem")
    baseline_at = (datetime.now() - timedelta(days=31)).isoformat(timespec="seconds")
    with store._tx() as conn:
        conn.execute(
            "UPDATE rating_history SET created_at=? "
            "WHERE bot_id=? AND reason='old'",
            (baseline_at, bot["id"]),
        )
    store.add_rating_history(bot["id"], 1700, 50, 0.06, 10, "current", game_id="holdem")
    store.upsert_pair_stats(bot["id"], opponent["id"], a_losses_delta=1)
    store.create_match(
        "technical-profile", bot["id"], opponent["id"], game_id="holdem"
    )
    store.update_match(
        "technical-profile",
        status="completed",
        winner=1,
        reason="technical_loss",
        technical_loss=1,
        result={"rounds_played": 0, "deltas": [-1, 1], "normalized_delta": -0.01},
    )
    store.mark_match_rating_settled("technical-profile")

    with TestClient(app) as client:
        profile = client.get(f"/api/bots/{bot['id']}/profile").json()["profile"]

    assert profile["rated_matches"] == 10
    assert profile["ranking_eligible"] is True
    assert profile["rank"] == 1
    assert profile["rank_total"] == 2
    assert profile["percentile"] == 100.0
    assert profile["rating_delta"] == 200.0
    assert profile["recent_delta_30d"] == 200.0
    assert profile["confidence_low"] == 1602.0
    assert profile["confidence_high"] == 1798.0
    assert profile["unique_opponents"] == 1
    assert profile["technical_failures"] == 1
    assert profile["normal_completion_rate"] == 0.9
    assert "matches_played" not in profile


def test_recent_delta_is_null_without_window_baseline(tmp_path):
    store = Store(str(tmp_path / "baseline.db"))
    bot = _rated_bot(
        store, username="recent", name="recent_bot", rating=1550, matches=10
    )
    store.add_rating_history(bot["id"], 1500, 80, 0.06, 9, "previous")
    store.add_rating_history(bot["id"], 1550, 80, 0.06, 10, "current")
    profile = store.bot_profile(bot["id"])
    assert profile is not None
    assert profile["rating_delta"] == 50.0
    assert profile["recent_delta_30d"] is None
    store.close()
