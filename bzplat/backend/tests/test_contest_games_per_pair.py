"""Holdem per-pair physical Match series contracts."""
from __future__ import annotations

import asyncio
import itertools
import json
import sqlite3
from collections import defaultdict
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bzplat.backend.contests.manager import ContestManager
from bzplat.backend.contests.ranking import compute_official_ranking
from bzplat.backend.contests.stages import generate_stage_pairings
from bzplat.backend.contests.templates import list_templates
from bzplat.backend.contests.validation import validate_stage
from bzplat.backend.crypto import hash_password
from bzplat.backend.main import create_app
from bzplat.backend.store import Store
from bzplat.backend.store.schema import SCHEMA


class _NoopOrchestrator:
    max_concurrent = 2

    async def challenge(self, *_args, **_kwargs):  # pragma: no cover - future start guard
        raise AssertionError("future published contest must not dispatch")

    async def challenge_duplicate(self, *_args, **_kwargs):  # pragma: no cover
        raise AssertionError("future published contest must not dispatch")


def _fixture_people(
    store: Store,
    tmp_path: Path,
    count: int,
    *,
    prefix: str,
) -> tuple[list[dict], list[dict]]:
    users: list[dict] = []
    bots: list[dict] = []
    for index in range(count):
        user = store.create_user(
            f"{prefix}-u{index}",
            f"{prefix}-u{index}@example.com",
            "hash",
        )
        binary = tmp_path / f"{prefix}-bot-{index}"
        binary.write_bytes(b"test fixture")
        bot = store.create_bot(
            user["id"],
            f"{prefix}-b{index}",
            binary_path=str(binary),
            format="elf",
            game_id="holdem",
        )
        users.append(user)
        bots.append(bot)
    return users, bots


def _series_rows(
    entry_a: dict,
    entry_b: dict,
    bot_a: dict,
    bot_b: dict,
    size: int,
    *,
    seed_base: int = 10_000,
    stage_key: str = "rr",
) -> list[dict]:
    rows: list[dict] = []
    for index in range(1, size + 1):
        if index % 2:
            ea, eb, ba, bb = entry_a, entry_b, bot_a, bot_b
        else:
            ea, eb, ba, bb = entry_b, entry_a, bot_b, bot_a
        rows.append(
            {
                "entry_a_id": ea["id"],
                "entry_b_id": eb["id"],
                "bot_a_id": ba["id"],
                "bot_b_id": bb["id"],
                "round_num": index,
                "stage_key": stage_key,
                "status": "pending",
                "pairing_seed": seed_base + index,
                "series_index": index,
                "series_size": size,
            }
        )
    return rows


def test_template_capability_and_manager_creation_boundary(tmp_path):
    configurable = {
        template["id"]: template["games_per_pair_config"]
        for template in list_templates()
        if "games_per_pair_config" in template
    }
    assert configurable == {
        "holdem_dup_rr": {"default": 1, "min": 1, "max": 10},
        "holdem_rr": {"default": 1, "min": 1, "max": 10},
    }

    store = Store(str(tmp_path / "manager-boundary.db"))
    organizer = store.create_user("series-org", "series-org@example.com", "hash")
    manager = ContestManager(store, _NoopOrchestrator())

    defaulted = manager.create(
        organizer["id"], "default series", template_id="holdem_rr"
    )
    explicit = manager.create(
        organizer["id"],
        "three matches",
        template_id="holdem_dup_rr",
        games_per_pair=3,
    )
    assert json.loads(defaulted["stages_json"])[0]["games_per_pair"] == 1
    assert json.loads(explicit["stages_json"])[0]["games_per_pair"] == 3

    for value in (True, 1.5, 0, 11):
        with pytest.raises(ValueError, match="games_per_pair"):
            manager.create(
                organizer["id"],
                f"bad-{value!r}",
                template_id="holdem_rr",
                games_per_pair=value,  # type: ignore[arg-type]
            )
    with pytest.raises(ValueError, match="不支持 games_per_pair"):
        manager.create(
            organizer["id"],
            "swiss cannot opt in",
            template_id="holdem_swiss_ko",
            games_per_pair=2,
        )
    with pytest.raises(ValueError, match="不支持 games_per_pair"):
        manager.create(
            organizer["id"],
            "custom cannot opt in",
            stages=[{"type": "round_robin"}],
            games_per_pair=2,
        )
    with pytest.raises(ValueError, match="不支持 games_per_pair"):
        manager.create(
            organizer["id"],
            "custom cannot smuggle",
            template_id="holdem_rr",
            stages=[{"type": "round_robin", "games_per_pair": 2}],
        )
    with pytest.raises(ValueError, match="games_per_pair"):
        validate_stage(
            {"type": "group_round_robin", "games_per_pair": 2}, 0, "holdem"
        )
    store.close()


def test_create_api_strict_validation_and_frozen_projection(tmp_path, monkeypatch):
    monkeypatch.setenv("BZ_SKIP_CAPTCHA", "1")
    app = create_app(db_path=str(tmp_path / "series-api.db"))
    store = app.state.store
    organizer = store.create_user(
        "series-api-org",
        "series-api-org@example.com",
        hash_password("pw123456"),
        role="organizer",
    )
    store.update_user(organizer["id"], email_verified=1)
    _, token = app.state.auth.authenticate("series-api-org", "pw123456")
    headers = {"Authorization": f"Bearer {token}"}
    client = TestClient(app)

    templates = client.get("/api/contests/templates").json()["templates"]
    capabilities = {
        row["id"]: row.get("games_per_pair_config")
        for row in templates
        if row.get("games_per_pair_config") is not None
    }
    assert capabilities == {
        "holdem_dup_rr": {"default": 1, "min": 1, "max": 10},
        "holdem_rr": {"default": 1, "min": 1, "max": 10},
    }

    created = client.post(
        "/api/contests",
        headers=headers,
        json={
            "title": "API three",
            "template_id": "holdem_rr",
            "games_per_pair": 3,
        },
    )
    assert created.status_code == 200, created.text
    payload = created.json()["contest"]
    assert payload["games_per_pair"] == 3
    assert json.loads(payload["stages_json"])[0]["games_per_pair"] == 3

    for value in ("3", 3.0, True):
        rejected = client.post(
            "/api/contests",
            headers=headers,
            json={
                "title": f"wrong type {value!r}",
                "template_id": "holdem_rr",
                "games_per_pair": value,
            },
        )
        assert rejected.status_code == 422
    for value in (0, 11):
        rejected = client.post(
            "/api/contests",
            headers=headers,
            json={
                "title": f"wrong range {value}",
                "template_id": "holdem_rr",
                "games_per_pair": value,
            },
        )
        assert rejected.status_code == 400
    unsupported = client.post(
        "/api/contests",
        headers=headers,
        json={
            "title": "wrong template",
            "template_id": "holdem_swiss_ko",
            "games_per_pair": 2,
        },
    )
    assert unsupported.status_code == 400


def test_all_supported_sizes_have_pair_and_global_seat_balance():
    """Exhaust n=2..12 and K=1..10 against the frozen cohort orientation."""
    for entrant_count in range(2, 13):
        cohort = [10_000 + position * 37 for position in range(entrant_count)]
        position = {bot_id: index for index, bot_id in enumerate(cohort)}
        for games_per_pair in range(1, 11):
            pairings = generate_stage_pairings(
                {"type": "round_robin", "games_per_pair": games_per_pair},
                cohort,
            )
            assert len(pairings) == (
                entrant_count * (entrant_count - 1) // 2 * games_per_pair
            )
            per_pair: dict[frozenset[int], list[int]] = defaultdict(list)
            seat_debt = {bot_id: 0 for bot_id in cohort}
            coordinates: dict[frozenset[int], list[int]] = defaultdict(list)
            for pairing in pairings:
                assert pairing.bot_b_id is not None
                seat_zero = (
                    pairing.bot_a_id
                    if pairing.color_first == 0
                    else pairing.bot_b_id
                )
                seat_one = (
                    pairing.bot_b_id
                    if pairing.color_first == 0
                    else pairing.bot_a_id
                )
                key = frozenset((pairing.bot_a_id, pairing.bot_b_id))
                per_pair[key].append(seat_zero)
                coordinates[key].append(pairing.series_index)
                seat_debt[seat_zero] += 1
                seat_debt[seat_one] -= 1

            assert len(per_pair) == entrant_count * (entrant_count - 1) // 2
            for key, seat_zero_rows in per_pair.items():
                first, second = tuple(key)
                assert len(seat_zero_rows) == games_per_pair
                assert abs(
                    seat_zero_rows.count(first) - seat_zero_rows.count(second)
                ) == games_per_pair % 2
                assert sorted(coordinates[key]) == list(range(1, games_per_pair + 1))
            expected_global_debt = (
                0
                if games_per_pair % 2 == 0
                else 1 if entrant_count % 2 == 0 else 0
            )
            assert {abs(value) for value in seat_debt.values()} == {
                expected_global_debt
            }

            # Renumbering Bot ids while preserving frozen cohort positions must
            # not change the seat orientation in cohort coordinates.
            relabeled = [900_000 - index * 101 for index in range(entrant_count)]
            relabeled_position = {
                bot_id: index for index, bot_id in enumerate(relabeled)
            }
            relabeled_pairings = generate_stage_pairings(
                {"type": "round_robin", "games_per_pair": games_per_pair},
                relabeled,
            )

            def positional_shape(rows, positions):
                return sorted(
                    (
                        min(positions[row.bot_a_id], positions[row.bot_b_id]),
                        max(positions[row.bot_a_id], positions[row.bot_b_id]),
                        positions[
                            row.bot_a_id if row.color_first == 0 else row.bot_b_id
                        ],
                        row.series_index,
                        row.round_num,
                    )
                    for row in rows
                    if row.bot_b_id is not None
                )

            assert positional_shape(pairings, position) == positional_shape(
                relabeled_pairings, relabeled_position
            )


def test_legacy_schema_migrates_series_columns_with_safe_defaults(tmp_path):
    legacy_fragment = (
        "    series_index    INTEGER NOT NULL DEFAULT 1 CHECK(series_index>=1),\n"
        "    series_size     INTEGER NOT NULL DEFAULT 1 CHECK(series_size>=1),\n"
        "    CONSTRAINT chk_contest_pairing_series CHECK(series_index<=series_size)\n"
    )
    assert legacy_fragment in SCHEMA
    legacy_schema = SCHEMA.replace(
        "    color_first     INTEGER NOT NULL DEFAULT 0,\n" + legacy_fragment,
        "    color_first     INTEGER NOT NULL DEFAULT 0\n",
    )
    db_path = tmp_path / "legacy-series.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(legacy_schema)
    organizer_id = conn.execute(
        "INSERT INTO users(username,email,password_hash,created_at) "
        "VALUES(?,?,?,?)",
        ("legacy-series-org", "legacy-series@example.com", "hash", "2026-01-01"),
    ).lastrowid
    contest_id = conn.execute(
        "INSERT INTO contests(title,organizer_id,created_at,game_id) "
        "VALUES(?,?,?,?)",
        ("Legacy series", organizer_id, "2026-01-01", "holdem"),
    ).lastrowid
    conn.execute(
        "INSERT INTO contest_pairings("
        "contest_id,round_num,status,stage_idx,stage_key,color_first"
        ") VALUES(?,?,?,?,?,?)",
        (contest_id, 1, "pending", 0, "rr", 0),
    )
    conn.commit()
    conn.close()

    store = Store(str(db_path))
    with store._tx() as migrated:
        columns = {
            column[1]: column for column in migrated.execute(
                "PRAGMA table_info(contest_pairings)"
            )
        }
        legacy_row = migrated.execute(
            "SELECT series_index,series_size FROM contest_pairings"
        ).fetchone()
    assert columns["series_index"][4] == "1"
    assert columns["series_size"][4] == "1"
    assert tuple(legacy_row) == (1, 1)
    store.close()

    # Reopening an already-upgraded database must be idempotent and retain the
    # historical physical Match as a one-match series.
    reopened = Store(str(db_path))
    with reopened._tx() as migrated:
        legacy_row = migrated.execute(
            "SELECT series_index,series_size FROM contest_pairings"
        ).fetchone()
    assert tuple(legacy_row) == (1, 1)
    reopened.close()


def test_store_rejects_partial_series_bad_seeds_and_identity_mutation(tmp_path):
    store = Store(str(tmp_path / "series-invariants.db"))
    users, bots = _fixture_people(store, tmp_path, 2, prefix="invariant")
    contest = store.create_contest(
        "Invariant",
        users[0]["id"],
        status="published",
        game_id="holdem",
        template_id="holdem_rr",
        stages_json=json.dumps(
            [{"key": "rr", "type": "round_robin", "games_per_pair": 3}]
        ),
    )
    entries = [
        store.add_contest_entry(contest["id"], user["id"], bot["id"])
        for user, bot in zip(users, bots)
    ]
    valid = _series_rows(entries[0], entries[1], bots[0], bots[1], 3)

    with pytest.raises(ValueError, match="完整覆盖"):
        store.create_contest_stage_pairings(
            contest["id"], 0, valid[:2], expected_current_stage_idx=0
        )
    duplicate_index = [dict(row) for row in valid]
    duplicate_index[1]["series_index"] = 1
    with pytest.raises(ValueError, match="完整覆盖"):
        store.create_contest_stage_pairings(
            contest["id"], 0, duplicate_index, expected_current_stage_idx=0
        )
    duplicate_seed = [dict(row) for row in valid]
    duplicate_seed[1]["pairing_seed"] = duplicate_seed[0]["pairing_seed"]
    with pytest.raises(ValueError, match="互不重复"):
        store.create_contest_stage_pairings(
            contest["id"], 0, duplicate_seed, expected_current_stage_idx=0
        )
    for invalid_seed in (None, True, "100", [], 0):
        bad = [dict(row) for row in valid]
        bad[0]["pairing_seed"] = invalid_seed
        with pytest.raises(ValueError, match="pairing_seed"):
            store.create_contest_stage_pairings(
                contest["id"], 0, bad, expected_current_stage_idx=0
            )
    for index, size in ((0, 3), (4, 3), (1, 0), (True, 3)):
        bad = [dict(row) for row in valid]
        bad[0]["series_index"] = index
        bad[0]["series_size"] = size
        with pytest.raises(ValueError, match="1<=index<=size"):
            store.create_contest_stage_pairings(
                contest["id"], 0, bad, expected_current_stage_idx=0
            )
    with pytest.raises(ValueError, match="原子批次"):
        store.add_pairing(
            contest["id"],
            bots[0]["id"],
            bots[1]["id"],
            entry_a_id=entries[0]["id"],
            entry_b_id=entries[1]["id"],
            pairing_seed=1,
            series_index=1,
            series_size=2,
        )

    persisted = store.create_contest_stage_pairings(
        contest["id"], 0, valid, expected_current_stage_idx=0
    )
    for field, value in (
        ("pairing_seed", 99),
        ("series_index", 2),
        ("series_size", 4),
    ):
        with pytest.raises(ValueError, match="发布身份字段不可修改"):
            store.update_pairing(persisted[0]["id"], **{field: value})
    store.close()


def _published_series_fixture(tmp_path: Path, prefix: str, *, duplicate: bool = True):
    store = Store(str(tmp_path / f"{prefix}.db"))
    organizer = store.create_user(
        f"{prefix}-org", f"{prefix}-org@example.com", "hash"
    )
    users, bots = _fixture_people(store, tmp_path, 3, prefix=prefix)
    manager = ContestManager(store, _NoopOrchestrator())
    contest = manager.create(
        organizer["id"],
        f"{prefix} contest",
        template_id="holdem_dup_rr" if duplicate else "holdem_rr",
        games_per_pair=3,
        starts_at="2099-12-31T23:59:59",
    )
    for user, bot in zip(users, bots):
        store.add_contest_entry(contest["id"], user["id"], bot["id"])
    store.update_contest(contest["id"], status="open")
    return store, manager, contest["id"], bots


def test_publish_recovery_freezes_series_identity_and_is_idempotent(tmp_path):
    store, manager, contest_id, bots = _published_series_fixture(
        tmp_path, "publish-series"
    )
    asyncio.run(manager.publish(contest_id))
    first = store.list_contest_pairings(contest_id, stage_idx=0)
    assert len(first) == 9
    assert len({row["pairing_seed"] for row in first}) == 9
    assert all(row["pairing_seed"] is not None for row in first)
    grouped: dict[frozenset[int], list[dict]] = defaultdict(list)
    for row in first:
        grouped[frozenset((row["bot_a_id"], row["bot_b_id"]))].append(row)
    assert set(grouped) == {
        frozenset(pair) for pair in itertools.combinations([bot["id"] for bot in bots], 2)
    }
    for rows in grouped.values():
        assert sorted(row["series_index"] for row in rows) == [1, 2, 3]
        assert {row["series_size"] for row in rows} == {3}
        seat_zero_counts = {
            bot_id: 0
            for bot_id in (rows[0]["bot_a_id"], rows[0]["bot_b_id"])
        }
        for row in rows:
            seat_zero_counts[row["bot_a_id"]] += 1
        assert sorted(seat_zero_counts.values()) == [1, 2]

    first_ids = [row["id"] for row in first]
    asyncio.run(manager.ensure_published_pairings(contest_id, 0))
    assert [row["id"] for row in store.list_contest_pairings(contest_id)] == first_ids

    expected_signature = manager._pairing_batch_signature(first)
    same_pair = sorted(next(iter(grouped.values())), key=lambda row: row["series_index"])
    first_seed, second_seed = (
        same_pair[0]["pairing_seed"],
        same_pair[1]["pairing_seed"],
    )
    with store._tx() as conn:
        conn.execute(
            "UPDATE contest_pairings SET pairing_seed=CASE id "
            "WHEN ? THEN ? WHEN ? THEN ? ELSE pairing_seed END "
            "WHERE id IN (?,?)",
            (
                same_pair[0]["id"],
                second_seed,
                same_pair[1]["id"],
                first_seed,
                same_pair[0]["id"],
                same_pair[1]["id"],
            ),
        )
    asyncio.run(manager.ensure_published_pairings(contest_id, 0))
    seed_recovered = store.list_contest_pairings(contest_id, stage_idx=0)
    assert manager._pairing_batch_signature(seed_recovered) == expected_signature
    assert {row["id"] for row in seed_recovered}.isdisjoint(first_ids)

    # A coordinate swap retains the same aggregate 1..K index set, so this
    # specifically proves recovery compares each frozen row identity rather
    # than only batch counts/sets.
    regrouped: dict[frozenset[int], list[dict]] = defaultdict(list)
    for row in seed_recovered:
        regrouped[frozenset((row["bot_a_id"], row["bot_b_id"]))].append(row)
    same_pair = sorted(
        next(iter(regrouped.values())), key=lambda row: row["series_index"]
    )
    with store._tx() as conn:
        conn.execute(
            "UPDATE contest_pairings SET series_index=CASE id "
            "WHEN ? THEN 2 WHEN ? THEN 1 ELSE series_index END "
            "WHERE id IN (?,?)",
            (
                same_pair[0]["id"],
                same_pair[1]["id"],
                same_pair[0]["id"],
                same_pair[1]["id"],
            ),
        )
    asyncio.run(manager.ensure_published_pairings(contest_id, 0))
    coordinate_recovered = store.list_contest_pairings(contest_id, stage_idx=0)
    assert manager._pairing_batch_signature(coordinate_recovered) == expected_signature
    assert {row["id"] for row in coordinate_recovered}.isdisjoint(
        {row["id"] for row in seed_recovered}
    )

    expected_seeds = {row["pairing_seed"] for row in first}
    with store._tx() as conn:
        conn.execute(
            "DELETE FROM contest_pairings WHERE id=?",
            (coordinate_recovered[4]["id"],),
        )
    asyncio.run(manager.ensure_published_pairings(contest_id, 0))
    recovered = store.list_contest_pairings(contest_id, stage_idx=0)
    assert len(recovered) == 9
    assert {row["pairing_seed"] for row in recovered} == expected_seeds
    assert manager.estimate(contest_id)["estimated_matches"] == 9
    store.close()


def test_concurrent_series_publish_creates_one_complete_batch(tmp_path):
    store, manager, contest_id, _bots = _published_series_fixture(
        tmp_path, "concurrent-series", duplicate=False
    )

    async def exercise():
        return await asyncio.gather(
            manager.publish(contest_id),
            manager.publish(contest_id),
            return_exceptions=True,
        )

    results = asyncio.run(exercise())
    assert sum(isinstance(result, dict) for result in results) == 1
    errors = [result for result in results if isinstance(result, Exception)]
    assert len(errors) == 1 and isinstance(errors[0], ValueError)
    pairings = store.list_contest_pairings(contest_id, stage_idx=0)
    assert len(pairings) == 9
    assert len({row["pairing_seed"] for row in pairings}) == 9
    store.close()


def _cyclic_scoring_fixture(
    tmp_path: Path,
    *,
    games_per_pair: int,
    duplicate: bool,
) -> tuple[Store, ContestManager, int]:
    suffix = f"score-{games_per_pair}-{'dup' if duplicate else 'normal'}"
    store = Store(str(tmp_path / f"{suffix}.db"))
    users, bots = _fixture_people(store, tmp_path, 3, prefix=suffix)
    stage = {
        "key": "rr",
        "type": "round_robin",
        "scoring": "poker_3_1_0",
        "games_per_pair": games_per_pair,
        "duplicate": duplicate,
    }
    contest = store.create_contest(
        suffix,
        users[0]["id"],
        status="published",
        game_id="holdem",
        template_id="holdem_dup_rr" if duplicate else "holdem_rr",
        stages_json=json.dumps([stage]),
    )
    entries = [
        store.add_contest_entry(contest["id"], user["id"], bot["id"])
        for user, bot in zip(users, bots)
    ]
    rows: list[dict] = []
    seed = 100_000
    for first, second in itertools.combinations(range(3), 2):
        rows.extend(
            _series_rows(
                entries[first],
                entries[second],
                bots[first],
                bots[second],
                games_per_pair,
                seed_base=seed,
            )
        )
        seed += 100
    persisted = store.create_contest_stage_pairings(
        contest["id"], 0, rows, expected_current_stage_idx=0
    )

    # Rock-paper-scissors cycle: entry0 beats entry1, entry1 beats entry2,
    # entry2 beats entry0.  Every entrant therefore has identical totals.
    winner_by_pair = {
        frozenset((entries[0]["id"], entries[1]["id"])): entries[0]["id"],
        frozenset((entries[1]["id"], entries[2]["id"])): entries[1]["id"],
        frozenset((entries[0]["id"], entries[2]["id"])): entries[2]["id"],
    }
    for ordinal, pairing in enumerate(persisted, start=1):
        match_id = f"{suffix}-{ordinal}"
        winner_entry = winner_by_pair[
            frozenset((pairing["entry_a_id"], pairing["entry_b_id"]))
        ]
        winner = 0 if pairing["entry_a_id"] == winner_entry else 1
        deltas = [10, -10] if winner == 0 else [-10, 10]
        result = (
            {
                "deltas": [deltas[0] * 2, deltas[1] * 2],
                "legs": [
                    {"winner": winner, "deltas": list(deltas)},
                    {"winner": winner, "deltas": list(deltas)},
                ],
            }
            if duplicate
            else {"deltas": deltas}
        )
        store.create_match(
            match_id,
            pairing["bot_a_id"],
            pairing["bot_b_id"],
            owner_id=users[0]["id"],
            contest_id=contest["id"],
            match_type="contest",
            game_id="holdem",
        )
        store.bind_contest_pairing_match(
            contest["id"],
            pairing["id"],
            match_id,
            require_execution_admission=False,
        )
        store.update_match(
            match_id,
            status="completed",
            winner=None if duplicate else winner,
            result=result,
            ended_at=f"2026-08-27T00:00:{ordinal:02d}",
        )
        store.complete_contest_pairing_for_match(contest["id"], match_id)
    return store, ContestManager(store, _NoopOrchestrator()), contest["id"]


@pytest.mark.parametrize(
    ("games_per_pair", "duplicate", "points", "wins", "buchholz", "cut1"),
    [
        (1, False, 3, 1, 6, 3),
        (3, False, 9, 3, 54, 45),
        (1, True, 6, 2, 24, 18),
        (3, True, 18, 6, 216, 198),
    ],
)
def test_series_scoring_and_record_weighted_cut1(
    tmp_path,
    games_per_pair,
    duplicate,
    points,
    wins,
    buchholz,
    cut1,
):
    store, manager, contest_id = _cyclic_scoring_fixture(
        tmp_path,
        games_per_pair=games_per_pair,
        duplicate=duplicate,
    )
    standings = manager.standings(contest_id)
    assert len(standings) == 3
    assert {row["points"] for row in standings} == {points}
    assert {row["wins"] for row in standings} == {wins}
    assert {row["losses"] for row in standings} == {wins}
    assert manager._stage_done(contest_id, 0) is True
    assert len(store.list_contest_pairings(contest_id, stage_idx=0)) == (
        3 * games_per_pair
    )

    pairings = store.list_contest_pairings(contest_id, stage_idx=0)
    matches = {
        pairing["match_id"]: store.get_match(pairing["match_id"])
        for pairing in pairings
    }
    ranked = compute_official_ranking(standings, pairings, matches)
    assert {row["tiebreaks"]["buchholz"] for row in ranked} == {buchholz}
    # Cut1 removes one highest-opponent-score scoring record, not one unique
    # opponent and not the entire repeated series against that opponent.
    assert {row["tiebreaks"]["buchholz_cut1"] for row in ranked} == {cut1}
    expected_records = 2 * games_per_pair * (2 if duplicate else 1)
    assert buchholz == expected_records * points
    assert cut1 == buchholz - points
    store.close()


def test_duplicate_technical_terminal_without_legs_is_one_scoring_record(tmp_path):
    """A physical duplicate Match fault must not invent its unplayed second leg."""
    store = Store(str(tmp_path / "duplicate-technical-terminal.db"))
    users, bots = _fixture_people(store, tmp_path, 2, prefix="dup-technical")
    contest = store.create_contest(
        "Duplicate technical terminal",
        users[0]["id"],
        status="published",
        game_id="holdem",
        template_id="holdem_dup_rr",
        stages_json=json.dumps(
            [
                {
                    "key": "dup_rr",
                    "type": "round_robin",
                    "scoring": "poker_3_1_0",
                    "duplicate": True,
                    "games_per_pair": 1,
                }
            ]
        ),
    )
    entries = [
        store.add_contest_entry(contest["id"], user["id"], bot["id"])
        for user, bot in zip(users, bots)
    ]
    pairing = store.create_contest_stage_pairings(
        contest["id"],
        0,
        _series_rows(entries[0], entries[1], bots[0], bots[1], 1),
        expected_current_stage_idx=0,
    )[0]
    match_id = "duplicate-technical-no-legs"
    store.create_match(
        match_id,
        pairing["bot_a_id"],
        pairing["bot_b_id"],
        owner_id=users[0]["id"],
        contest_id=contest["id"],
        match_type="contest",
        game_id="holdem",
    )
    store.bind_contest_pairing_match(
        contest["id"],
        pairing["id"],
        match_id,
        require_execution_admission=False,
    )
    store.update_match(
        match_id,
        status="completed",
        winner=0,
        reason="technical_loss",
        result={"deltas": [1, -1]},
        technical_loss=1,
        ended_at="2026-08-27T12:00:00+08:00",
    )
    store.complete_contest_pairing_for_match(contest["id"], match_id)

    manager = ContestManager(store, _NoopOrchestrator())
    standings = manager.standings(contest["id"])
    by_entry = {row["entry_id"]: row for row in standings}
    assert by_entry[entries[0]["id"]]["points"] == 3
    assert by_entry[entries[0]["id"]]["wins"] == 1
    assert by_entry[entries[1]["id"]]["points"] == 0
    assert by_entry[entries[1]["id"]]["losses"] == 1

    pairings = store.list_contest_pairings(contest["id"], stage_idx=0)
    ranked = compute_official_ranking(
        standings,
        pairings,
        {match_id: store.get_match(match_id)},
    )
    ranked_by_entry = {row["entry_id"]: row for row in ranked}
    loser_tiebreaks = ranked_by_entry[entries[1]["id"]]["tiebreaks"]
    assert loser_tiebreaks["buchholz"] == 3
    assert loser_tiebreaks["buchholz_cut1"] == 0
    assert loser_tiebreaks["head_to_head"] == 0
    assert loser_tiebreaks["technical_losses"] == 1
    assert manager._stage_done(contest["id"], 0) is True
    store.close()
