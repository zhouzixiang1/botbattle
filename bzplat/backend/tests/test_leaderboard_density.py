"""单游戏高密度排行榜契约回归。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bzplat.backend.main import create_app
from bzplat.backend.store import Store


def _rated_bot(
    store: Store,
    *,
    username: str,
    name: str,
    rating: float,
    matches: int,
    wins: int,
    draws: int = 0,
) -> dict:
    user = store.create_user(username, f"{username}@example.com", "hash")
    bot = store.create_bot(
        user["id"], name, display_name=f"{name} display",
        binary_path=f"/tmp/{name}", format="elf", game_id="holdem"
    )
    store.select_ranked_bot(int(user["id"]), int(bot["id"]), if_empty=True)
    store.ensure_rating(bot["id"], game_id="holdem")
    store.update_rating_row(
        bot["id"],
        game_id="holdem",
        rating=rating,
        rd=80,
        matches_played=matches,
        wins=wins,
        draws=draws,
        losses=max(0, matches - wins - draws),
        last_played_at=f"2026-08-10T12:{bot['id']:02d}:00",
    )
    return bot


def test_store_leaderboard_requires_one_registered_game(tmp_path):
    store = Store(str(tmp_path / "required-game.db"))
    with pytest.raises(TypeError):
        store.list_leaderboard()  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="game_id 不可为空"):
        store.list_leaderboard(game_id="")
    with pytest.raises(ValueError, match="未知 game_id"):
        store.list_leaderboard(game_id="future-game")
    store.close()


def test_leaderboard_summary_rank_and_sample_are_server_authoritative(tmp_path):
    app = create_app(db_path=str(tmp_path / "ranked.db"))
    store = app.state.store
    formal_high = _rated_bot(
        store, username="formal_high", name="formal_high_bot",
        rating=1800, matches=12, wins=8,
    )
    formal_low = _rated_bot(
        store, username="formal_low", name="formal_low_bot",
        rating=1500, matches=10, wins=4, draws=1,
    )
    sample = _rated_bot(
        store, username="sample", name="sample_bot",
        rating=2300, matches=9, wins=9,
    )

    with TestClient(app) as client:
        missing = client.get("/api/leaderboard")
        assert missing.status_code == 422
        blank = client.get("/api/leaderboard?game_id=%20")
        assert blank.status_code == 400

        response = client.get(
            "/api/leaderboard?game_id=holdem&page=1&per_page=20"
        )
        assert response.status_code == 200
        body = response.json()

    assert body["game_id"] == "holdem"
    assert body["ranking_min_matches"] == 10
    assert body["summary"] == {
        "total": 3,
        "eligible": 2,
        "sample": 1,
        "last_rated_at": f"2026-08-10T12:{sample['id']:02d}:00",
    }
    rows = body["leaderboard"]
    assert [row["bot_id"] for row in rows] == [
        formal_high["id"], formal_low["id"], sample["id"],
    ]
    assert [row["rank"] for row in rows] == [1, 2, None]
    assert rows[-1]["ranking_eligible"] is False
    assert rows[-1]["ranking_progress"] == 0.9
    assert rows[-1]["rated_matches"] == 9

    # game_id 只在响应顶层出现；内部累计分差和平台 canonical tuple 不进榜单。
    forbidden = {"game_id", "delta_total", "net_chips", "format", "os", "arch", "vol"}
    assert all(forbidden.isdisjoint(row) for row in rows)


def test_rating_delta_and_recent_match_stay_in_same_validated_game(tmp_path):
    app = create_app(db_path=str(tmp_path / "recent.db"))
    store = app.state.store
    bot = _rated_bot(
        store, username="recent_owner", name="recent_bot",
        rating=1800, matches=14, wins=9,
    )
    opponent = _rated_bot(
        store, username="recent_opp", name="recent_opp_bot",
        rating=1500, matches=12, wins=5,
    )

    store.add_rating_history(
        bot["id"], 1400, 100, 0.06, 10, "non-match-history",
        game_id="holdem",
    )
    store.create_match(
        "valid-holdem-match", bot["id"], opponent["id"], game_id="holdem"
    )
    store.update_match(
        "valid-holdem-match", status="completed", winner=0,
        reason="completed", result={"rounds_played": 70, "deltas": [1, -1], "normalized_delta": 0.01},
    )
    store.add_rating_history(
        bot["id"], 1500, 90, 0.06, 11, "valid-holdem-match",
        game_id="holdem",
    )
    assert store.mark_match_rating_settled("valid-holdem-match")

    # 物理行仍在 holdem，但故意把索引漂移成 gomoku；最新链接必须跳过它。
    store.create_match(
        "drifted-index-match", bot["id"], opponent["id"], game_id="holdem"
    )
    store.update_match(
        "drifted-index-match", status="completed", winner=0,
        reason="completed", result={"rounds_played": 70, "deltas": [1, -1], "normalized_delta": 0.01},
    )
    assert store.mark_match_rating_settled("drifted-index-match")
    store._conn.execute(
        "UPDATE matches_index SET game_id='gomoku' WHERE id='drifted-index-match'"
    )
    store._conn.commit()
    store.add_rating_history(
        bot["id"], 1600, 80, 0.06, 12, "drifted-index-match",
        game_id="holdem",
    )

    # 索引与物理表都属于同一游戏，但目标 Bot 实际没坐在任何一侧。损坏的
    # rating_history.reason 不能因此把别人的对局显示成该 Bot 的最近对局。
    outsider = _rated_bot(
        store, username="recent_outsider", name="recent_outsider_bot",
        rating=1500, matches=12, wins=5,
    )
    store.create_match(
        "same-game-nonparticipant", opponent["id"], outsider["id"],
        game_id="holdem",
    )
    store.update_match(
        "same-game-nonparticipant", status="completed", winner=0,
        reason="completed",
        result={"rounds_played": 70, "deltas": [1, -1], "normalized_delta": 0.01},
    )
    store.add_rating_history(
        bot["id"], 1700, 80, 0.06, 13, "same-game-nonparticipant",
        game_id="holdem",
    )
    store.add_rating_history(
        bot["id"], 1800, 80, 0.06, 14, "non-match-current",
        game_id="holdem",
    )

    result = store.list_leaderboard(game_id="holdem", page=1, per_page=20)
    row = next(item for item in result["items"] if item["bot_id"] == bot["id"])
    assert row["rating_delta"] == 100.0
    assert row["last_match_id"] == "valid-holdem-match"
    assert row["last_match_at"]
    store.close()
