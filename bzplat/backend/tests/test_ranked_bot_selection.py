"""One ranked Bot per owner/game: storage, API, queue, and migration guards."""
from __future__ import annotations

import hashlib
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bzplat.backend.bots.manager import BotManager
from bzplat.backend.crypto import hash_password
from bzplat.backend.main import create_app
from bzplat.backend.runtime.config import RANKING_MIN_RATED_MATCHES
from bzplat.backend.store import RankedBotSelectionBusyError, Store
from bzplat.backend.store.schema import SCHEMA, game_rule_contract


ELF = Path(__file__).resolve().parents[3] / "samples" / "callbot_linux_amd64"
RANKED_INDEX = "idx_bots_one_ranked_per_owner_game"


def _user(store: Store, name: str) -> dict:
    return store.create_user(name, f"{name}@example.com", hash_password("pw123456"))


def _bot(
    store: Store,
    owner_id: int,
    name: str,
    *,
    game_id: str = "holdem",
    versioned: bool = False,
) -> dict:
    bot = store.create_bot(
        owner_id,
        name,
        binary_path=str(ELF),
        game_id=game_id,
        format="elf",
    )
    if versioned:
        raw = ELF.read_bytes()
        store.add_bot_version(
            bot["id"],
            binary_path=str(ELF),
            checksum=hashlib.sha256(raw).hexdigest(),
            size_bytes=len(raw),
            version=1,
        )
        bot = store.set_current_version(bot["id"], 1)
    return bot


def _select(store: Store, owner_id: int, bot_id: int) -> dict:
    return store.select_ranked_bot(owner_id, bot_id)["bot"]


def test_atomic_selection_unique_index_and_history_retention(tmp_path):
    store = Store(str(tmp_path / "ranked.db"))
    owner = _user(store, "rankowner")
    other = _user(store, "rankother")
    first = _bot(store, owner["id"], "first")
    second = _bot(store, owner["id"], "second")
    pencil = _bot(store, owner["id"], "pencil", game_id="pencil")
    rival = _bot(store, other["id"], "rival")

    assert not store.get_bot(first["id"])["is_ranked"]
    _select(store, owner["id"], first["id"])
    _select(store, owner["id"], pencil["id"])
    store.update_rating_row(
        first["id"], game_id="holdem", rating=1777, matches_played=12
    )
    before = store.get_rating(first["id"], game_id="holdem")

    switched = store.select_ranked_bot(owner["id"], second["id"])
    assert switched["previous_bot_id"] == first["id"]
    assert store.get_bot(first["id"])["is_ranked"] == 0
    assert store.get_bot(second["id"])["is_ranked"] == 1
    assert store.get_bot(pencil["id"])["is_ranked"] == 1
    assert store.get_rating(first["id"], game_id="holdem") == before

    with pytest.raises(sqlite3.IntegrityError):
        with store._tx() as conn:
            conn.execute(
                "UPDATE bots SET is_ranked=1 WHERE id=?", (first["id"],)
            )

    _select(store, other["id"], rival["id"])
    store.close()


def test_concurrent_selection_leaves_exactly_one_ranked_bot(tmp_path):
    db = tmp_path / "concurrent.db"
    seed = Store(str(db))
    owner = _user(seed, "concurrentowner")
    first = _bot(seed, owner["id"], "concurrent_first")
    second = _bot(seed, owner["id"], "concurrent_second")
    seed.close()

    stores = (Store(str(db)), Store(str(db)))
    barrier = threading.Barrier(2)

    def select(index: int, bot_id: int) -> int:
        barrier.wait(timeout=5)
        result = stores[index].select_ranked_bot(owner["id"], bot_id)
        return int(result["selected_bot_id"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        selected = {
            future.result(timeout=10)
            for future in (
                pool.submit(select, 0, first["id"]),
                pool.submit(select, 1, second["id"]),
            )
        }
    assert selected == {first["id"], second["id"]}
    for store in stores:
        store.close()

    verified = Store(str(db))
    rows = verified._conn.execute(
        "SELECT id FROM bots WHERE owner_id=? AND game_id='holdem' AND is_ranked=1",
        (owner["id"],),
    ).fetchall()
    assert len(rows) == 1
    assert int(rows[0]["id"]) in {first["id"], second["id"]}
    assert verified.rating_projection_status()["ready"] is True
    verified.close()


def test_first_successful_upload_selects_without_later_upload_stealing(tmp_path):
    store = Store(str(tmp_path / "upload.db"))
    owner = _user(store, "uploader")
    manager = BotManager(store, upload_root=tmp_path / "uploads")
    raw = ELF.read_bytes()

    first = manager.create_from_upload(owner["id"], "upload_one", raw)
    second = manager.create_from_upload(owner["id"], "upload_two", raw)

    assert store.get_bot(first["id"])["is_ranked"] == 1
    assert store.get_bot(second["id"])["is_ranked"] == 0
    store.close()


def test_owner_ranking_api_selects_replaces_and_withdraws(tmp_path):
    app = create_app(db_path=str(tmp_path / "api.db"))
    store = app.state.store
    owner = _user(store, "apiowner")
    other = _user(store, "apiother")
    store.update_user(owner["id"], email_verified=1)
    store.update_user(other["id"], email_verified=1)
    first = _bot(store, owner["id"], "api_first")
    second = _bot(store, owner["id"], "api_second")
    _, owner_token = app.state.auth.authenticate("apiowner", "pw123456")
    _, other_token = app.state.auth.authenticate("apiother", "pw123456")
    owner_headers = {"Authorization": f"Bearer {owner_token}"}
    other_headers = {"Authorization": f"Bearer {other_token}"}

    with TestClient(app) as client:
        denied = client.put(
            f"/api/bots/{first['id']}/ranking", headers=other_headers
        )
        assert denied.status_code == 403

        selected = client.put(
            f"/api/bots/{first['id']}/ranking", headers=owner_headers
        )
        assert selected.status_code == 200
        assert selected.json()["bot"]["is_ranked"] == 1

        replaced = client.put(
            f"/api/bots/{second['id']}/ranking", headers=owner_headers
        )
        assert replaced.status_code == 200
        assert replaced.json()["previous_bot_id"] == first["id"]

        cleared = client.delete(
            f"/api/bots/{second['id']}/ranking", headers=owner_headers
        )
        assert cleared.status_code == 200
        assert cleared.json()["selected_bot_id"] is None

        store.update_bot(first["id"], is_active=0)
        unavailable = client.put(
            f"/api/bots/{first['id']}/ranking", headers=owner_headers
        )
        assert unavailable.status_code == 409
        assert unavailable.json()["detail"]["code"] == "ranking_unavailable"


def test_rating_policy_leaderboard_and_profile_follow_current_selection(tmp_path):
    store = Store(str(tmp_path / "policy.db"))
    owner_a = _user(store, "policya")
    owner_b = _user(store, "policyb")
    selected_a = _bot(store, owner_a["id"], "selected_a")
    reserve_a = _bot(store, owner_a["id"], "reserve_a")
    selected_b = _bot(store, owner_b["id"], "selected_b")
    _select(store, owner_a["id"], selected_a["id"])
    _select(store, owner_b["id"], selected_b["id"])

    spoofed = store.create_match(
        "spoofed-rating-config",
        selected_a["id"],
        selected_b["id"],
        game_id="holdem",
        match_config={
            "_rating_eligible": False,
            "_rating_reason": "ranked_bot_not_selected",
        },
    )
    spoofed_config = store.get_match("spoofed-rating-config")["match_config"]
    assert spoofed_config["_rating_eligible"] is True
    assert spoofed_config["_rating_reason"] == "eligible"
    assert store.match_rating_policy(spoofed) == {
        "rated": True,
        "rating_reason": "eligible",
    }
    store.update_match("spoofed-rating-config", status="aborted", reason="test cleanup")

    neutral = store.create_match(
        "reserve-pair", reserve_a["id"], selected_b["id"], game_id="holdem"
    )
    assert store.match_rating_policy(neutral) == {
        "rated": False,
        "rating_reason": "ranked_bot_not_selected",
    }
    store.update_match("reserve-pair", status="aborted", reason="test cleanup")
    rated = store.create_match(
        "ranked-pair", selected_a["id"], selected_b["id"], game_id="holdem"
    )
    assert store.match_rating_policy(rated) == {
        "rated": True,
        "rating_reason": "eligible",
    }
    store.update_match("ranked-pair", status="aborted", reason="test cleanup")

    store.update_rating_row(
        selected_a["id"], game_id="holdem", rating=1500, matches_played=10
    )
    store.update_rating_row(
        reserve_a["id"], game_id="holdem", rating=1900, matches_played=20
    )
    store.update_rating_row(
        selected_b["id"], game_id="holdem", rating=1600, matches_played=10
    )
    assert {
        row["bot_id"] for row in store.list_leaderboard(game_id="holdem")["items"]
    } == {selected_a["id"], selected_b["id"]}
    reserve_profile = store.bot_profile(reserve_a["id"])
    assert reserve_profile["rating"] == 1900
    assert reserve_profile["ranking_eligible"] is False
    assert reserve_profile["rank"] is None

    store.select_ranked_bot(owner_a["id"], reserve_a["id"])
    rows = store.list_leaderboard(game_id="holdem")["items"]
    assert [row["bot_id"] for row in rows] == [reserve_a["id"], selected_b["id"]]
    assert (
        store.get_rating(reserve_a["id"], game_id="holdem")["matches_played"]
        == 20
    )
    store.close()


def test_frozen_policy_allows_neutral_and_rated_lifecycles_to_coexist(tmp_path):
    store = Store(str(tmp_path / "frozen-overlap.db"))
    owner_a = _user(store, "overlapa")
    owner_b = _user(store, "overlapb")
    owner_c = _user(store, "overlapc")
    selected_a = _bot(store, owner_a["id"], "overlap_selected_a")
    reserve_a = _bot(store, owner_a["id"], "overlap_reserve_a")
    selected_b = _bot(store, owner_b["id"], "overlap_selected_b")
    selected_c = _bot(store, owner_c["id"], "overlap_selected_c")
    _select(store, owner_a["id"], selected_a["id"])
    _select(store, owner_b["id"], selected_b["id"])
    _select(store, owner_c["id"], selected_c["id"])

    neutral = store.create_match(
        "neutral-overlap", reserve_a["id"], selected_b["id"], game_id="holdem"
    )
    assert store.match_rating_policy(neutral) == {
        "rated": False,
        "rating_reason": "ranked_bot_not_selected",
    }

    # The neutral pending Match shares seat B with this truly rated Match.  It
    # must not be reclassified from current owners or block the rated lifecycle.
    rated = store.create_match(
        "rated-overlap", selected_a["id"], selected_b["id"], game_id="holdem"
    )
    assert store.match_rating_policy(rated) == {
        "rated": True,
        "rating_reason": "eligible",
    }
    store.update_match("neutral-overlap", status="running")

    # The SQLite boundary still rejects a second rated lifecycle even when a
    # caller bypasses Store.create_match.  Missing internal policy data also
    # fails closed as rated instead of bypassing the trigger.
    for match_id, config in (
        ("raw-rated-overlap", '{"_rating_eligible":true}'),
        ("raw-legacy-overlap", "{}"),
    ):
        with pytest.raises(sqlite3.IntegrityError, match="rated match lifecycle overlap"):
            with store._tx() as conn:
                conn.execute(
                    "INSERT INTO matches_holdem("
                    "id,bot_a_id,bot_b_id,match_type,status,game_id,match_config,created_at"
                    ") VALUES(?,?,?,'challenge','pending','holdem',?,datetime('now'))",
                    (match_id, selected_c["id"], selected_b["id"], config),
                )

    with pytest.raises(ValueError, match="正在其他计分对局中"):
        store.create_match(
            "second-rated-overlap",
            selected_c["id"],
            selected_b["id"],
            game_id="holdem",
        )

    store.update_match("rated-overlap", status="aborted", reason="test cleanup")
    switched = store.select_ranked_bot(owner_a["id"], reserve_a["id"])
    assert switched["previous_bot_id"] == selected_a["id"]
    assert store.match_rating_policy(store.get_match("neutral-overlap")) == {
        "rated": False,
        "rating_reason": "ranked_bot_not_selected",
    }
    assert store.match_rating_policy(store.get_match("rated-overlap")) == {
        "rated": True,
        "rating_reason": "eligible",
    }
    store.update_match("neutral-overlap", status="aborted", reason="test cleanup")
    store.close()


def test_missing_frozen_policy_fails_closed_for_every_overlap_gate(tmp_path):
    store = Store(str(tmp_path / "missing-policy-overlap.db"))
    owner_a = _user(store, "missingpolicya")
    owner_b = _user(store, "missingpolicyb")
    selected_a = _bot(store, owner_a["id"], "missing_policy_selected")
    reserve_a = _bot(store, owner_a["id"], "missing_policy_reserve")
    selected_b = _bot(store, owner_b["id"], "missing_policy_rival")
    _select(store, owner_a["id"], selected_a["id"])
    _select(store, owner_b["id"], selected_b["id"])

    # Simulate a crashed/legacy low-level writer that committed the physical
    # Match but never inserted its immutable policy row.
    with store._tx() as conn:
        conn.execute(
            "INSERT INTO matches_holdem("
            "id,bot_a_id,bot_b_id,match_type,status,game_id,match_config,created_at"
            ") VALUES(?,?,?,'challenge','pending','holdem',?,datetime('now'))",
            (
                "orphan-policy-match",
                selected_a["id"],
                selected_b["id"],
                '{"_rating_eligible":true}',
            ),
        )

    with pytest.raises(ValueError, match="正在其他计分对局中"):
        store.create_match(
            "product-after-orphan",
            selected_a["id"],
            selected_b["id"],
            game_id="holdem",
        )
    with pytest.raises(RankedBotSelectionBusyError):
        store.select_ranked_bot(owner_a["id"], reserve_a["id"])
    with pytest.raises(sqlite3.IntegrityError, match="rated match lifecycle overlap"):
        with store._tx() as conn:
            conn.execute(
                "INSERT INTO matches_holdem("
                "id,bot_a_id,bot_b_id,match_type,status,game_id,match_config,created_at"
                ") VALUES(?,?,?,'challenge','pending','holdem',?,datetime('now'))",
                (
                    "raw-after-orphan",
                    selected_a["id"],
                    selected_b["id"],
                    '{"_rating_eligible":true}',
                ),
            )
    store.close()


def test_neutral_physical_match_does_not_block_rated_execution_claim(tmp_path):
    store = Store(str(tmp_path / "neutral-claim.db"))
    owner_a = _user(store, "claimoverlapa")
    owner_b = _user(store, "claimoverlapb")
    selected_a = _bot(
        store, owner_a["id"], "claim_overlap_selected", versioned=True
    )
    reserve_a = _bot(
        store, owner_a["id"], "claim_overlap_reserve", versioned=True
    )
    selected_b = _bot(
        store, owner_b["id"], "claim_overlap_rival", versioned=True
    )
    _select(store, owner_a["id"], selected_a["id"])
    _select(store, owner_b["id"], selected_b["id"])
    neutral = store.create_match(
        "neutral-before-claim",
        reserve_a["id"],
        selected_b["id"],
        game_id="holdem",
    )
    assert store.match_rating_policy(neutral)["rated"] is False

    store.executions.resume()
    job = store.executions.enqueue(
        source="manual",
        owner_user_id=owner_a["id"],
        game_id="holdem",
        match_type="challenge",
        bot_a_id=selected_a["id"],
        bot_b_id=selected_b["id"],
        bot_a_version_id=store.get_current_bot_version(selected_a["id"])["id"],
        bot_b_version_id=store.get_current_bot_version(selected_b["id"])["id"],
    )
    assert job["rated"] == 1
    claimed = store.executions.claim_next(
        max_match_slots=1,
        max_sandbox_units=2,
        aging_seconds=0,
        user_active_limit=1,
        contest_share_slots=1,
    )
    assert claimed is not None
    assert claimed["public_id"] == job["public_id"]
    claimed_match = store.get_match(str(claimed["current_match_id"]))
    assert store.match_rating_policy(claimed_match) == {
        "rated": True,
        "rating_reason": "eligible",
    }
    # Updating the already-frozen neutral lifecycle must also coexist with the
    # newly claimed rated Match.
    store.update_match("neutral-before-claim", status="running")
    store.close()


def test_switch_cancels_queued_auto_and_busy_lifecycle_blocks_change(tmp_path):
    store = Store(str(tmp_path / "queue.db"))
    owner_a = _user(store, "queuea")
    owner_b = _user(store, "queueb")
    first = _bot(store, owner_a["id"], "queue_first", versioned=True)
    reserve = _bot(store, owner_a["id"], "queue_reserve", versioned=True)
    rival = _bot(store, owner_b["id"], "queue_rival", versioned=True)
    _select(store, owner_a["id"], first["id"])
    _select(store, owner_b["id"], rival["id"])

    refill = store.executions.refill_auto(
        target_queued=1,
        bootstrap_target_matches=RANKING_MIN_RATED_MATCHES,
    )
    assert refill["inserted"] == 1
    auto_job = store.executions.snapshot(
        max_match_slots=1,
        max_sandbox_units=2,
        aging_seconds=0,
    )["queued"][0]
    decision_id = int(auto_job["auto_decision_id"])

    switched = store.select_ranked_bot(owner_a["id"], reserve["id"])
    assert switched["cancelled_queued_jobs"] == 1
    assert store.executions.get(auto_job["public_id"])["terminal_reason"] == (
        "ranking_entry_changed"
    )
    decision = store._conn.execute(
        "SELECT lifecycle,terminal_reason FROM auto_match_decisions WHERE id=?",
        (decision_id,),
    ).fetchone()
    assert tuple(decision) == ("cancelled", "ranking_entry_changed")

    store.create_match(
        "direct-ranked", reserve["id"], rival["id"], game_id="holdem"
    )
    with pytest.raises(RankedBotSelectionBusyError):
        store.clear_ranked_bot(owner_a["id"], reserve["id"])
    store.update_match("direct-ranked", status="running")
    with pytest.raises(RankedBotSelectionBusyError):
        store.clear_ranked_bot(owner_a["id"], reserve["id"])
    store.update_match("direct-ranked", status="aborted", reason="test cleanup")
    assert store.clear_ranked_bot(owner_a["id"], reserve["id"])["changed"] is True
    _select(store, owner_a["id"], reserve["id"])

    store.create_match(
        "unsettled-ranked", reserve["id"], rival["id"], game_id="holdem"
    )
    store.update_match(
        "unsettled-ranked",
        status="completed",
        winner=0,
        result={"rounds_played": 1, "deltas": [1, -1], "normalized_delta": 1},
    )
    with pytest.raises(RankedBotSelectionBusyError):
        store.clear_ranked_bot(owner_a["id"], reserve["id"])
    assert store.get_bot(reserve["id"])["is_ranked"] == 1
    store.close()


def test_claim_cancels_frozen_rated_job_after_low_level_selection_drift(tmp_path):
    store = Store(str(tmp_path / "claim.db"))
    owner_a = _user(store, "claima")
    owner_b = _user(store, "claimb")
    first = _bot(store, owner_a["id"], "claim_first", versioned=True)
    reserve = _bot(store, owner_a["id"], "claim_reserve", versioned=True)
    rival = _bot(store, owner_b["id"], "claim_rival", versioned=True)
    _select(store, owner_a["id"], first["id"])
    _select(store, owner_b["id"], rival["id"])
    store.executions.resume()
    job = store.executions.enqueue(
        source="manual",
        owner_user_id=owner_a["id"],
        game_id="holdem",
        match_type="challenge",
        bot_a_id=first["id"],
        bot_b_id=rival["id"],
        bot_a_version_id=store.get_current_bot_version(first["id"])["id"],
        bot_b_version_id=store.get_current_bot_version(rival["id"])["id"],
    )
    assert job["rated"] == 1

    # Simulate a stale external writer between enqueue and claim.  The claim
    # gate must fail closed even when the frozen job still says rated=1.
    with store._tx() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE bots SET is_ranked=0 WHERE id=?", (first["id"],))
        conn.execute("UPDATE bots SET is_ranked=1 WHERE id=?", (reserve["id"],))
    claimed = store.executions.claim_next(
        max_match_slots=1,
        max_sandbox_units=2,
        aging_seconds=0,
        user_active_limit=1,
        contest_share_slots=1,
    )
    assert claimed is None
    stale = store.executions.get(job["public_id"])
    assert stale["status"] == "cancelled"
    assert stale["terminal_reason"] == "ranking_entry_changed"
    store.close()


def test_existing_database_backfill_is_deterministic_and_never_repeats(tmp_path):
    db = tmp_path / "legacy.db"
    legacy_schema = SCHEMA.replace(
        "    is_ranked       INTEGER NOT NULL DEFAULT 0,\n", ""
    ).replace(
        "    CONSTRAINT chk_bot_ranked CHECK (is_ranked IN (0,1)),\n", ""
    )
    conn = sqlite3.connect(db)
    conn.executescript(legacy_schema)
    now = "2026-08-18T00:00:00"
    contract = game_rule_contract("holdem", legacy=True)
    conn.execute(
        "INSERT INTO rating_pool_state(game_id,active_pool_id,ruleset_version,"
        "protocol_version,activated_at) VALUES(?,?,?,?,?)",
        (
            "holdem",
            contract["rating_pool_id"],
            contract["ruleset_version"],
            contract["protocol_version"],
            now,
        ),
    )
    conn.execute(
        "INSERT INTO users(id,username,email,password_hash,created_at) "
        "VALUES(1,'legacyuser','legacy@example.com','hash',?)",
        (now,),
    )
    for bot_id, name in ((1, "high_sample"), (2, "high_rating"), (3, "inactive")):
        conn.execute(
            "INSERT INTO bots(id,owner_id,name,display_name,binary_path,"
            "current_version,is_active,is_builtin,game_id,runtime_mode,"
            "protocol_version,created_at,updated_at) "
            "VALUES(?,?,?,?,?,1,?,?,?,'traditional',?,?,?)",
            (
                bot_id,
                1,
                name,
                name,
                str(ELF),
                0 if bot_id == 3 else 1,
                0,
                "holdem",
                contract["protocol_version"],
                now,
                now,
            ),
        )
        conn.execute(
            "INSERT INTO bot_versions(bot_id,version,binary_path,protocol_version,"
            "uploaded_at) VALUES(?,1,?,?,?)",
            (bot_id, str(ELF), contract["protocol_version"], now),
        )
    conn.execute(
        "INSERT INTO ratings(bot_id,game_id,rating,matches_played) "
        "VALUES(1,'holdem',1500,10),(2,'holdem',2200,9),(3,'holdem',3000,99)"
    )
    conn.execute(
        "UPDATE rating_projection_state SET policy_version='owner-neutral-v3' "
        "WHERE singleton=1"
    )
    conn.commit()
    conn.close()

    migrated = Store(str(db))
    assert migrated.get_bot(1)["is_ranked"] == 1
    assert migrated.get_bot(2)["is_ranked"] == 0
    assert migrated.get_bot(3)["is_ranked"] == 0
    assert migrated.rating_projection_status()["ready"] is False
    assert migrated.rating_projection_status()["state"]["policy_version"] == (
        "owner-neutral-v3"
    )
    migrated.clear_ranked_bot(1, 1)
    migrated.close()

    reopened = Store(str(db))
    assert reopened._conn.execute(
        "SELECT COUNT(*) FROM bots WHERE is_ranked=1"
    ).fetchone()[0] == 0
    reopened.close()


def test_missing_ranked_index_on_nonempty_migrated_database_fails_closed(tmp_path):
    db = tmp_path / "missing-index.db"
    store = Store(str(db))
    owner = _user(store, "missingindex")
    _bot(store, owner["id"], "missing_index_bot")
    store.close()
    with sqlite3.connect(db) as conn:
        conn.execute(f"DROP INDEX {RANKED_INDEX}")

    with pytest.raises(RuntimeError, match="index missing on non-empty"):
        Store(str(db))


def test_noncanonical_ranked_index_definition_fails_closed(tmp_path):
    db = tmp_path / "wrong-index.db"
    store = Store(str(db))
    store.close()
    with sqlite3.connect(db) as conn:
        conn.execute(f"DROP INDEX {RANKED_INDEX}")
        conn.execute(
            f"CREATE UNIQUE INDEX {RANKED_INDEX} ON bots(owner_id,game_id) "
            "WHERE is_ranked=0"
        )

    with pytest.raises(RuntimeError, match="index definition mismatch"):
        Store(str(db))


@pytest.mark.parametrize(
    "collision_sql",
    (
        f"CREATE TABLE {RANKED_INDEX}(id INTEGER)",
        f"CREATE VIEW {RANKED_INDEX} AS SELECT 1 AS id",
        f"CREATE TRIGGER {RANKED_INDEX} AFTER INSERT ON users BEGIN SELECT 1; END",
    ),
)
def test_ranked_index_schema_object_collision_fails_closed(
    tmp_path, collision_sql
):
    db = tmp_path / ("collision-" + collision_sql.split()[1].lower() + ".db")
    store = Store(str(db))
    store.close()
    with sqlite3.connect(db) as conn:
        conn.execute(f"DROP INDEX {RANKED_INDEX}")
        conn.execute(collision_sql)

    with pytest.raises(RuntimeError, match="schema object name collision"):
        Store(str(db))


def test_safe_hard_deletes_advance_projection_and_blocked_deletes_do_not(
    tmp_path,
):
    store = Store(str(tmp_path / "safe-delete.db"))

    bot_owner = _user(store, "deletebotowner")
    disposable_bot = _bot(store, bot_owner["id"], "delete_bot")
    _select(store, bot_owner["id"], disposable_bot["id"])
    before_bot = store.rating_projection_status()
    deleted_bot = store.delete_bot_if_safe(disposable_bot["id"])
    after_bot = store.rating_projection_status()
    assert deleted_bot["deleted"] is True
    assert after_bot["ready"] is True
    assert after_bot["state"]["mutation_revision"] > before_bot["state"][
        "mutation_revision"
    ]
    assert after_bot["state"]["mutation_revision"] == after_bot["state"][
        "trusted_mutation_revision"
    ]

    disposable_owner = _user(store, "deleteuserowner")
    disposable_owned_bot = _bot(store, disposable_owner["id"], "delete_user_bot")
    _select(store, disposable_owner["id"], disposable_owned_bot["id"])
    before_user = store.rating_projection_status()
    deleted_user = store.delete_user_if_safe(disposable_owner["id"])
    after_user = store.rating_projection_status()
    assert deleted_user["deleted"] is True
    assert after_user["ready"] is True
    assert after_user["state"]["mutation_revision"] > before_user["state"][
        "mutation_revision"
    ]
    assert after_user["state"]["mutation_revision"] == after_user["state"][
        "trusted_mutation_revision"
    ]

    blocked_owner = _user(store, "blockeddelete")
    blocked_bot = _bot(store, blocked_owner["id"], "blocked_delete_bot")
    _select(store, blocked_owner["id"], blocked_bot["id"])
    store.create_match(
        "blocked-delete-match",
        blocked_bot["id"],
        blocked_bot["id"],
        game_id="holdem",
    )
    before_blocked = store.rating_projection_status()
    assert store.delete_bot_if_safe(blocked_bot["id"])["deleted"] is False
    assert store.delete_user_if_safe(blocked_owner["id"])["deleted"] is False
    after_blocked = store.rating_projection_status()
    assert after_blocked["ready"] is True
    assert after_blocked["state"]["mutation_revision"] == before_blocked[
        "state"
    ]["mutation_revision"]
    assert after_blocked["state"]["trusted_mutation_revision"] == before_blocked[
        "state"
    ]["trusted_mutation_revision"]
    store.close()
