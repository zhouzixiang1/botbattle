"""Read-only lifecycle showcase snapshots and stage-history presentation."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bzplat.backend.contests import showcase_seed as showcase_seed_module
from bzplat.backend.contests.scheduler import ContestScheduler
from bzplat.backend.contests.showcase_seed import (
    SHOWCASE_PLAYER_PROFILES,
    SHOWCASE_PROFILE_SPECS,
    ShowcaseSeedError,
    _recover_incomplete_showcases,
    _verify_frozen_profile_versions,
    _verify_group_stage_distribution,
    _verify_match_replay_quality,
    _verify_pairing_identity_graph,
    rollback_showcases,
    validate_showcase_upload_namespace,
    validate_showcase_upload_target,
)
from bzplat.backend.crypto import hash_password
from bzplat.backend.main import create_app
from bzplat.backend.matches.runner import MatchRunner
from bzplat.backend.runtime.binary_runner import BinaryRunner


PASSWORD = "pw123456"


def _account(store, username: str, role: str = "user") -> dict:
    user = store.create_user(
        username,
        f"{username}@example.com",
        hash_password(PASSWORD),
        role=role,
    )
    return store.update_user(user["id"], email_verified=1)


def _headers(app, username: str) -> dict[str, str]:
    _user, token = app.state.auth.authenticate(username, PASSWORD)
    return {"Authorization": f"Bearer {token}"}


def _showcase_app(tmp_path):
    app = create_app(db_path=str(tmp_path / "showcase.db"), max_concurrent=1)
    store = app.state.store
    org = _account(store, "showcase_org", "organizer")
    admin = _account(store, "showcase_admin", "admin")
    user = _account(store, "showcase_user")
    other_org = _account(store, "showcase_other_org", "organizer")
    return app, store, org, admin, user, other_org


def test_fresh_showcase_shell_preserves_legacy_stages_after_template_reenabled(tmp_path):
    from bzplat.backend.contests.templates import get_template

    app, store, org, _admin, _user, _other_org = _showcase_app(tmp_path)
    template = get_template(showcase_seed_module.HISTORICAL_SHOWCASE_TEMPLATE_ID)
    assert template is not None
    assert template.get("creation_enabled", True) is True
    assert template["stages"][1]["tiebreak"] == "paired_swap_until_decided"

    contest = showcase_seed_module._create_historical_showcase_contest(
        app.state.contest_manager,
        "contest_lifecycle_draft",
        org["id"],
        starts_at=None,
    )
    stages = json.loads(contest["stages_json"])
    assert stages[0]["type"] == "group_double_round_robin"
    assert "round_stagger_minutes" not in stages[0]
    assert stages[1]["type"] == "single_elimination"
    assert "tiebreak" not in stages[1]
    store.close()


def _rollback_seed_fixture(tmp_path, *, with_match: bool = False):
    app = create_app(db_path=str(tmp_path / "rollback-fixture.db"), max_concurrent=1)
    store = app.state.store
    upload_root = tmp_path / "bot_uploads_showcase"
    validate_showcase_upload_namespace(store, upload_root, create=True)
    organizer = store.create_user(
        "showcase_organizer", "showcase-organizer@invalid.example",
        hash_password(PASSWORD), role="organizer",
    )
    players = []
    bots = []
    versions = []
    for index in (1, 2):
        player = store.create_user(
            f"showcase_player_{index:02d}",
            f"showcase-player-{index:02d}@invalid.example",
            hash_password(PASSWORD), role="user",
        )
        bot = store.create_bot(
            player["id"], f"showcase_gomoku_{index:02d}",
            description="合成演示 LongRunning 五子棋 Bot",
            binary_path="", format="elf", os="linux", arch="amd64",
            game_id="gomoku", runtime_mode="longrunning", is_active=1,
        )
        binary = upload_root / str(bot["id"]) / "v1" / "bot.bin"
        binary.parent.mkdir(parents=True)
        binary.write_bytes(f"rollback-bot-{index}".encode())
        version = store.add_bot_version(
            bot["id"], version=1, binary_path=str(binary),
            checksum=hashlib.sha256(binary.read_bytes()).hexdigest(),
            size_bytes=binary.stat().st_size, runtime_mode="longrunning",
        )
        store.update_bot(bot["id"], binary_path=str(binary), current_version=1)
        players.append(player)
        bots.append(bot)
        versions.append(version)
    key = "contest_lifecycle_draft"
    contest = store.create_contest(
        showcase_seed_module.TITLE[key], organizer["id"],
        description=f"{showcase_seed_module._marker(key)}\n合成演示快照",
        template_id="gomoku_group_drr_ko", game_id="gomoku",
    )
    match_id = None
    if with_match:
        entries = [
            store.add_contest_entry(contest["id"], player["id"], bot["id"])
            for player, bot in zip(players, bots)
        ]
        pairing = store.add_contest_pairing(
            contest["id"], bots[0]["id"], bots[1]["id"],
            entry_a_id=entries[0]["id"], entry_b_id=entries[1]["id"],
            bot_a_version_id=versions[0]["id"],
            bot_b_version_id=versions[1]["id"],
        )
        match_id = "rollback-interrupted-match"
        store.create_match(
            match_id, bots[0]["id"], bots[1]["id"],
            owner_id=organizer["id"], contest_id=contest["id"],
            match_type="contest", game_id="gomoku",
            match_config={
                "_bot_a_version_id": versions[0]["id"],
                "_bot_b_version_id": versions[1]["id"],
            },
        )
        store.update_match(
            match_id, status="completed", reason="five", winner=0,
        )
        store.update_contest_pairing(
            pairing["id"], status="completed", match_id=match_id,
        )
    # Showcase generation temporarily activates its private Bots while it
    # builds the roster/matches, then restores the frozen public-inactive state.
    for bot in bots:
        store.update_bot(bot["id"], is_active=0)
    return store, upload_root, organizer, players, bots, contest, match_id


def test_showcase_visibility_for_four_roles_and_write_conflicts(tmp_path):
    app, store, org, _admin, user, _other_org = _showcase_app(tmp_path)
    draft = store.create_contest(
        "合成演示：草稿阶段", org["id"], game_id="gomoku",
        template_id="gomoku_group_drr_ko",
    )
    opened = store.create_contest(
        "合成演示：报名阶段", org["id"], game_id="gomoku", status="open",
        template_id="gomoku_group_drr_ko",
    )
    store.freeze_contest_showcase(draft["id"], "contest_lifecycle_draft")
    store.freeze_contest_showcase(opened["id"], "contest_lifecycle_open")
    real = store.create_contest(
        "真实公开赛事", org["id"], game_id="gomoku", status="open",
        template_id="gomoku_group_drr_ko",
    )

    client = TestClient(app)
    normal = _headers(app, "showcase_user")
    organizer = _headers(app, "showcase_org")
    admin = _headers(app, "showcase_admin")
    # 真实赛事发现列表对四种身份使用同一数据骨架：showcase 在 SQL 的
    # COUNT/OFFSET 之前排除，不能占页、污染 total 或靠前端裁掉。
    for headers in (None, normal, organizer, admin):
        response = client.get(
            "/api/contests?page=1&per_page=1", headers=headers,
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["total"] == 1
        assert [row["id"] for row in payload["contests"]] == [real["id"]]
    assert client.get(f"/api/contests/{draft['id']}").status_code == 404

    assert client.get(f"/api/contests/{opened['id']}", headers=normal).status_code == 200
    assert client.get(f"/api/contests/{draft['id']}", headers=normal).status_code == 404
    # A normal participant reaches the immutable business guard before Bot
    # ownership/availability validation.
    register = client.post(
        f"/api/contests/{opened['id']}/register",
        json={"bot_id": 999999},
        headers=normal,
    )
    assert register.status_code == 409
    assert "演示快照" in register.json()["detail"]

    own_draft = client.get(f"/api/contests/{draft['id']}", headers=organizer)
    assert own_draft.status_code == 200
    assert own_draft.json()["contest"]["showcase_key"] == "contest_lifecycle_draft"
    assert client.post(
        f"/api/contests/{draft['id']}/open", headers=organizer
    ).status_code == 409

    other_organizer = _headers(app, "showcase_other_org")
    assert client.get(
        f"/api/contests/{draft['id']}", headers=other_organizer
    ).status_code == 404

    admin_detail = client.get(f"/api/contests/{draft['id']}", headers=admin)
    assert admin_detail.status_code == 200
    assert client.patch(
        f"/api/admin/contests/{draft['id']}",
        json={"status": "draft"},
        headers=admin,
    ).status_code == 409
    assert client.delete(
        f"/api/admin/contests/{draft['id']}", headers=admin
    ).status_code == 409


def test_showcase_seed_marker_is_not_exposed_by_public_api(tmp_path):
    app, store, org, *_ = _showcase_app(tmp_path)
    contest = store.create_contest(
        "【合成演示】公开文案", org["id"], game_id="gomoku", status="open",
        description=(
            "[contest-showcase-v1:contest_lifecycle_open]\n"
            "合成演示快照（只读，不代表真实活动赛事）。"
        ),
    )
    store.freeze_contest_showcase(contest["id"], "contest_lifecycle_open")
    client = TestClient(app)
    detail = client.get(f"/api/contests/{contest['id']}").json()["contest"]
    assert detail["description"] == "合成演示快照（只读，不代表真实活动赛事）。"
    assert contest["id"] not in {
        row["id"] for row in client.get("/api/contests").json()["contests"]
    }


def test_scheduler_reconcile_recovery_and_active_stats_skip_showcases(tmp_path):
    app, store, org, *_ = _showcase_app(tmp_path)
    draft = store.create_contest(
        "到点也不开放", org["id"], game_id="gomoku", status="draft",
        template_id="gomoku_group_drr_ko",
        registration_opens_at="2000-01-01T00:00:00",
    )
    running = store.create_contest(
        "冻结运行态", org["id"], game_id="gomoku", status="running",
        template_id="gomoku_group_drr_ko",
        stages_json=json.dumps([
            {"key": "group", "type": "group_double_round_robin", "group_count": 4,
             "advance_per_group": 2, "scoring": "ccgc_2_1_0"},
        ]),
    )
    store.freeze_contest_showcase(draft["id"], "contest_lifecycle_draft")
    store.freeze_contest_showcase(running["id"], "contest_lifecycle_running")

    scheduler = ContestScheduler(app.state.contest_manager, store)
    asyncio.run(scheduler._tick())
    assert store.get_contest(draft["id"])["status"] == "draft"
    assert asyncio.run(app.state.contest_manager.reconcile_running_contests()) == 0
    assert store.get_contest(running["id"])["status"] == "running"
    assert store.list_contests_by_status(["draft", "running"]) == []
    assert store.count_stats()["contests_running"] == 0


def test_detail_returns_persisted_per_stage_actual_participants_and_advancement(tmp_path):
    app, store, org, *_ = _showcase_app(tmp_path)
    contest = store.create_contest(
        "合成演示：完整赛事", org["id"], game_id="gomoku", status="finished",
        template_id="gomoku_group_drr_ko", current_stage_idx=1,
        stages_json=json.dumps([
            {"key": "group", "type": "group_double_round_robin", "group_count": 2,
             "advance_per_group": 1, "scoring": "ccgc_2_1_0"},
            {"key": "ko", "type": "single_elimination", "scoring": "ccgc_2_1_0"},
        ]),
    )
    entries = []
    bots = []
    for index in range(4):
        player = _account(store, f"stage_player_{index}")
        bot = store.create_bot(
            player["id"], f"stage_bot_{index}", display_name=f"阶段 Bot {index + 1}",
            binary_path=f"/tmp/stage-bot-{index}", format="elf", game_id="gomoku",
            is_active=1,
        )
        entry = store.add_contest_entry(contest["id"], player["id"], bot["id"])
        store.update_entry(
            contest["id"], player["id"], group_id="A" if index < 2 else "B",
        )
        entries.append(entry)
        bots.append(bot)

    # Stage 0 includes all four; stage 1 includes exactly the two group winners.
    for left, right, group in ((0, 1, "A"), (2, 3, "B")):
        store.add_contest_pairing(
            contest["id"], bots[left]["id"], bots[right]["id"],
            entry_a_id=entries[left]["id"], entry_b_id=entries[right]["id"],
            status="completed", stage_idx=0, stage_key="group", group_id=group,
        )
    store.add_contest_pairing(
        contest["id"], bots[0]["id"], bots[2]["id"],
        entry_a_id=entries[0]["id"], entry_b_id=entries[2]["id"],
        status="completed", stage_idx=1, stage_key="ko", bracket_slot=0,
    )
    for stage_idx in (0, 1):
        for index, (entry, bot) in enumerate(zip(entries, bots)):
            store.upsert_stage_result(
                contest["id"], stage_idx, entry["id"], bot_id=bot["id"],
                stage_key="group" if stage_idx == 0 else "ko",
                points=float(8 - index), wins=4 - index, losses=index,
                group_id="A" if index < 2 else "B",
            )
    replacement = store.create_bot(
        entries[0]["user_id"], "stage_bot_replacement",
        display_name="休息期替换 Bot", binary_path="/tmp/stage-bot-replacement",
        format="elf", game_id="gomoku", is_active=1,
    )
    store.update_entry(
        contest["id"], entries[0]["user_id"], bot_id=replacement["id"],
    )
    store.freeze_contest_showcase(contest["id"], "contest_lifecycle_finished")

    response = TestClient(app).get(f"/api/contests/{contest['id']}")
    assert response.status_code == 200
    payload = response.json()
    assert payload["contest"]["template_name"] == "五子棋：分组双循环 → 单败"
    stages = payload["stage_standings"]
    assert [len(stage["rows"]) for stage in stages] == [4, 2]
    assert {row["entry_id"] for row in stages[1]["rows"]} == {
        entries[0]["id"], entries[2]["id"],
    }
    stage0 = {row["entry_id"]: row for row in stages[0]["rows"]}
    assert stage0[entries[0]["id"]]["advancement"] == "advanced"
    assert stage0[entries[2]["id"]]["advancement"] == "advanced"
    assert stage0[entries[1]["id"]]["advancement"] == "eliminated"
    assert stages[0]["source"] == stages[1]["source"] == "persisted"
    assert stage0[entries[0]["id"]]["bot_id"] == bots[0]["id"]
    assert stage0[entries[0]["id"]]["bot_name"] == "阶段 Bot 1"

    store.delete_bot(bots[0]["id"])
    after_delete = TestClient(app).get(f"/api/contests/{contest['id']}").json()
    deleted_row = next(
        row for row in after_delete["stage_standings"][0]["rows"]
        if row["entry_id"] == entries[0]["id"]
    )
    assert deleted_row["bot_id"] is None
    assert deleted_row["bot_name"] == "历史 Bot（已删除）"


def test_showcase_key_is_nullable_unique(tmp_path):
    app, store, org, *_ = _showcase_app(tmp_path)
    first = store.create_contest("normal-1", org["id"], game_id="gomoku")
    second = store.create_contest("normal-2", org["id"], game_id="gomoku")
    assert first["showcase_key"] is None and second["showcase_key"] is None
    store.freeze_contest_showcase(first["id"], "contest_lifecycle_open")
    with pytest.raises(ValueError, match="已被其他赛事占用"):
        store.freeze_contest_showcase(second["id"], "contest_lifecycle_open")


def test_interrupted_seed_recovery_is_scoped_to_seed_namespace(tmp_path):
    app, store, org, *_ = _showcase_app(tmp_path)
    first = store.create_bot(
        org["id"], "scope_a", binary_path="/tmp/scope-a", format="elf",
        game_id="gomoku", is_active=1,
    )
    second = store.create_bot(
        org["id"], "scope_b", binary_path="/tmp/scope-b", format="elf",
        game_id="gomoku", is_active=1,
    )
    seed_contest = store.create_contest(
        "【合成演示】中断恢复", org["id"], game_id="gomoku", status="open",
        description="[contest-showcase-v1:contest_lifecycle_open]",
    )
    store.create_match(
        "seed-orphan", first["id"], second["id"], game_id="gomoku",
        match_type="contest", contest_id=seed_contest["id"],
    )
    store.update_match("seed-orphan", status="running")
    store.create_match(
        "unrelated-running", first["id"], second["id"], game_id="gomoku",
        match_type="challenge",
    )
    store.update_match("unrelated-running", status="running")

    recovered = asyncio.run(
        _recover_incomplete_showcases(
            app.state.contest_manager, org["id"], emit=lambda _message: None,
        )
    )
    assert recovered == 2  # abort the prepared ghost, then delete its exact row
    assert store.get_match("seed-orphan") is None
    assert store.get_match("unrelated-running")["status"] == "running"


def test_interrupted_bound_match_is_deleted_without_dispatching_future_pairing(tmp_path):
    app, store, org, *_ = _showcase_app(tmp_path)
    first = store.create_bot(
        org["id"], "bound_a", binary_path="/tmp/bound-a", format="elf",
        game_id="gomoku", is_active=1,
    )
    second = store.create_bot(
        org["id"], "bound_b", binary_path="/tmp/bound-b", format="elf",
        game_id="gomoku", is_active=1,
    )
    contest = store.create_contest(
        "【合成演示】中断排期", org["id"], game_id="gomoku", status="running",
        template_id="gomoku_group_drr_ko",
        description="[contest-showcase-v1:contest_lifecycle_running]",
        stages_json=json.dumps([
            {"key": "group", "type": "group_double_round_robin",
             "group_count": 1, "advance_per_group": 1, "scoring": "ccgc_2_1_0"},
        ]),
    )
    store.create_match(
        "bound-active", first["id"], second["id"], game_id="gomoku",
        match_type="contest", contest_id=contest["id"],
    )
    store.update_match("bound-active", status="running")
    pairing = store.add_contest_pairing(
        contest["id"], first["id"], second["id"], match_id="bound-active",
        status="running", scheduled_at="2999-01-01T00:00:00",
    )

    recovered = asyncio.run(
        _recover_incomplete_showcases(
            app.state.contest_manager, org["id"], emit=lambda _message: None,
        )
    )
    assert recovered == 3  # abort + exact unbind + physical match/index deletion
    assert store.get_match("bound-active") is None
    recovered_pairing = next(
        row for row in store.list_contest_pairings(contest["id"])
        if row["id"] == pairing["id"]
    )
    assert recovered_pairing["status"] == "pending"
    assert recovered_pairing["match_id"] is None
    assert store.list_matches(limit=100, contest_id=contest["id"]) == []


def test_admin_stats_exclude_showcase_contests_matches_identities_and_sessions(tmp_path):
    app = create_app(db_path=str(tmp_path / "stats.db"), max_concurrent=1)
    store = app.state.store
    demo_org = _account(store, "demo_stats_org", "organizer")
    demo_a = _account(store, "demo_stats_a")
    demo_b = _account(store, "demo_stats_b")
    normal = _account(store, "real_stats_user")
    demo_bot_a = store.create_bot(
        demo_a["id"], "demo_stats_a", binary_path="/tmp/demo-stats-a",
        format="elf", game_id="gomoku", is_active=1,
    )
    demo_bot_b = store.create_bot(
        demo_b["id"], "demo_stats_b", binary_path="/tmp/demo-stats-b",
        format="elf", game_id="gomoku", is_active=1,
    )
    normal_bot = store.create_bot(
        normal["id"], "real_stats_bot", binary_path="/tmp/real-stats",
        format="elf", game_id="gomoku", is_active=1,
    )
    showcase = store.create_contest(
        "showcase stats", demo_org["id"], game_id="gomoku", status="running",
    )
    entry_a = store.add_contest_entry(showcase["id"], demo_a["id"], demo_bot_a["id"])
    entry_b = store.add_contest_entry(showcase["id"], demo_b["id"], demo_bot_b["id"])
    store.add_contest_pairing(
        showcase["id"], demo_bot_a["id"], demo_bot_b["id"],
        entry_a_id=entry_a["id"], entry_b_id=entry_b["id"], status="completed",
    )
    store.create_match(
        "showcase-stats-match", demo_bot_a["id"], demo_bot_b["id"],
        game_id="gomoku", match_type="contest", contest_id=showcase["id"],
    )
    store.update_match(
        "showcase-stats-match", status="completed", reason="five", winner=0,
    )
    store.freeze_contest_showcase(showcase["id"], "contest_lifecycle_running")
    store.create_contest(
        "real contest", normal["id"], game_id="gomoku", status="running",
    )
    store.create_match(
        "real-stats-match", normal_bot["id"], normal_bot["id"],
        game_id="gomoku", match_type="challenge",
    )
    store.update_match("real-stats-match", status="completed", reason="five", winner=0)
    for token, user_id in (("demo-session", demo_a["id"]), ("real-session", normal["id"])):
        store.add_session(token, user_id, "2999-01-01T00:00:00")

    stats = store.count_stats()
    assert stats["users"] == stats["users_active"] == stats["users_verified"] == 1
    assert stats["bots"] == stats["bots_active"] == 1
    assert stats["contests"] == stats["contests_running"] == 1
    assert stats["matches"] == stats["matches_completed"] == 1
    assert stats["matches_aborted"] == stats["matches_running"] == stats["matches_pending"] == 0
    assert stats["matches_by_status"] == {"completed": 1}
    assert sum(row["count"] for row in stats["matches_recent_daily"]) == 1
    assert stats["active_sessions"] == 1
    assert [row["username"] for row in stats["recent_users"]] == ["real_stats_user"]


def test_inactive_showcase_bot_remains_publicly_readable_by_id(tmp_path):
    app = create_app(db_path=str(tmp_path / "inactive-bot.db"), max_concurrent=1)
    store = app.state.store
    owner = _account(store, "inactive_showcase_owner")
    bot = store.create_bot(
        owner["id"], "inactive_showcase_bot", binary_path="/tmp/inactive-showcase",
        format="elf", game_id="gomoku", is_active=0,
    )
    response = TestClient(app).get(f"/api/bots/{bot['id']}")
    assert response.status_code == 200
    assert response.json()["bot"]["id"] == bot["id"]


def test_showcase_upload_namespace_rejects_wrong_roots_content_and_symlinks(tmp_path):
    db_path = tmp_path / "copy.db"
    app = create_app(db_path=str(db_path), max_concurrent=1)
    store = app.state.store
    root = validate_showcase_upload_target(
        tmp_path / "bot_uploads_showcase", db_path=db_path,
        checkout_root=tmp_path / "checkout",
    )
    validate_showcase_upload_namespace(store, root, create=True)
    (root / "123").symlink_to(tmp_path)
    with pytest.raises(ShowcaseSeedError, match="符号链接"):
        validate_showcase_upload_namespace(store, root)
    nonmarker = tmp_path / "nonmarker" / "bot_uploads_showcase"
    nonmarker.mkdir(parents=True)
    (nonmarker / "unowned.bin").write_bytes(b"not a showcase namespace")
    with pytest.raises(ShowcaseSeedError, match="缺少 namespace marker"):
        validate_showcase_upload_namespace(store, nonmarker)
    with pytest.raises(ShowcaseSeedError, match="目录名必须固定"):
        validate_showcase_upload_target(
            tmp_path / "ordinary", db_path=db_path,
            checkout_root=tmp_path / "checkout",
        )
    nested = tmp_path / "bot_uploads" / "child" / "bot_uploads_showcase"
    with pytest.raises(ShowcaseSeedError, match="普通 Bot 上传目录"):
        validate_showcase_upload_target(
            nested, db_path=db_path, checkout_root=tmp_path / "checkout",
        )


def test_showcase_quality_rejects_fault_event_even_with_completed_row(tmp_path):
    app = create_app(db_path=str(tmp_path / "quality.db"), max_concurrent=1)
    store = app.state.store
    owner = _account(store, "quality_owner")
    first = store.create_bot(
        owner["id"], "quality_a", binary_path="/tmp/quality-a", format="elf",
        game_id="gomoku", is_active=1,
    )
    second = store.create_bot(
        owner["id"], "quality_b", binary_path="/tmp/quality-b", format="elf",
        game_id="gomoku", is_active=1,
    )
    store.create_match("quality-fault", first["id"], second["id"], game_id="gomoku")
    match = store.update_match(
        "quality-fault", status="completed", reason="five", winner=0,
        technical_loss=0,
    )
    store.upsert_replay("quality-fault", json.dumps([
        {"type": "match_start", "game_id": "gomoku"},
        {"type": "move", "player": 0, "x": 0, "y": 0},
        {"type": "technical_incident", "error": "bot_decide_error"},
        {"type": "match_end", "winner": 0, "reason": "five"},
    ]))
    with pytest.raises(ShowcaseSeedError, match="故障事件"):
        _verify_match_replay_quality(store, match)
    match = store.update_match(
        "quality-fault",
        result={"rounds_played": 1, "deltas": [1, -1], "normalized_delta": 1},
    )
    store.upsert_replay("quality-fault", json.dumps([
        {"type": "match_start", "game_id": "gomoku"},
        {"type": "move", "player": 0, "x": 0, "y": 0},
        {"type": "match_end", "winner": 0, "reason": "five", "deltas": [-1, 1]},
    ]))
    with pytest.raises(ShowcaseSeedError, match="终局事件与数据库不一致"):
        _verify_match_replay_quality(store, match)


def test_seed_deactivates_earlier_tracked_bot_when_later_identity_conflicts(
    tmp_path, monkeypatch,
):
    db_path = tmp_path / "tracked-cleanup.db"
    upload_root = tmp_path / "bot_uploads_showcase"
    upload_root.mkdir()
    profile_dir = Path(__file__).parents[3] / "samples" / "gomoku_showcase"
    raw = (profile_dir / SHOWCASE_PROFILE_SPECS["tactical"]["filename"]).read_bytes()
    store = create_app(db_path=str(db_path), max_concurrent=1).state.store
    players = []
    for index in (1, 2):
        player = store.create_user(
            f"showcase_player_{index:02d}",
            f"showcase-player-{index:02d}@invalid.example",
            hash_password(PASSWORD),
            role="user",
        )
        players.append(store.update_user(player["id"], email_verified=1))

    first = store.create_bot(
        players[0]["id"], "showcase_gomoku_01",
        display_name="演示棋手 01",
        description="合成演示 LongRunning 五子棋 Bot",
        binary_path="", format="elf", os="linux", arch="amd64",
        game_id="gomoku", runtime_mode="longrunning", is_active=0,
    )
    binary = upload_root / str(first["id"]) / "v1" / "bot.bin"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(raw)
    binary.chmod(0o755)
    store.add_bot_version(
        first["id"], version=1, binary_path=str(binary),
        checksum=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw),
        runtime_mode="longrunning",
    )
    store.create_bot(
        players[1]["id"], "showcase_gomoku_02",
        description="合成演示但元数据冲突",
        binary_path="/tmp/conflict", format="elf",
        game_id="holdem", runtime_mode="traditional", is_active=0,
    )
    store.close()

    # Model a conflict introduced after the namespace precheck.  The important
    # contract is that the first Bot was activated by this invocation before the
    # later identity fails, and finally cleans it without validating the whole graph.
    monkeypatch.setattr(
        showcase_seed_module,
        "validate_showcase_upload_namespace",
        lambda *_args, **_kwargs: {"bots": 0, "files": 0},
    )
    with pytest.raises(ShowcaseSeedError, match="专用演示 Bot 冲突"):
        asyncio.run(
            showcase_seed_module.seed_showcases(
                db_path, upload_root, profile_dir, emit=lambda _message: None,
            )
        )
    reopened = create_app(db_path=str(db_path), max_concurrent=1).state.store
    assert reopened.get_bot(first["id"])["is_active"] == 0
    reopened.close()


def test_showcase_profiles_are_checksum_pinned_and_deterministically_ranked():
    profile_dir = Path(__file__).parents[3] / "samples" / "gomoku_showcase"
    paths: dict[str, str] = {}
    for name, profile in SHOWCASE_PROFILE_SPECS.items():
        path = profile_dir / profile["filename"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == profile["checksum"]
        paths[name] = str(path)
    assert SHOWCASE_PLAYER_PROFILES == (
        "tactical", "tactical", "tactical", "tactical",
        "steady", "steady", "steady", "steady",
        "foundation", "foundation", "foundation", "foundation",
    )

    async def play(
        runner: MatchRunner, a: str, b: str,
    ) -> tuple[int | None, str, tuple[tuple[int, int, int], ...]]:
        result = await runner.run_binaries(
            paths[a], paths[b], game_id="gomoku",
            runtime_modes=("longrunning", "longrunning"), seed=20260810,
        )
        assert result.reason in {"five", "double_pass", "board_full"}
        assert not [
            event for event in result.events
            if event.get("type") in {
                "illegal", "forbidden", "technical_incident",
            }
            or event.get("reason") in {"crash", "timeout", "protocol_error"}
        ]
        assert result.events[-1]["type"] == "match_end"
        trajectory = tuple(
            (event["player"], event["x"], event["y"])
            for event in result.events if event.get("type") == "move"
        )
        return result.winner, result.reason, trajectory

    async def exercise(
        concurrency: int,
    ) -> tuple[
        list[tuple[int | None, str, tuple[tuple[int, int, int], ...]]],
        dict[str, int],
    ]:
        fixtures = [
            ("tactical", "steady", 0),
            ("steady", "tactical", 1),
            ("tactical", "foundation", 0),
            ("foundation", "tactical", 1),
            ("steady", "foundation", 0),
            ("foundation", "steady", 1),
        ]
        runner = MatchRunner(BinaryRunner(prefer_local=True))
        semaphore = asyncio.Semaphore(concurrency)

        async def bounded(a: str, b: str):
            async with semaphore:
                return await play(runner, a, b)

        work = [(a, b) for a, b, _winner in fixtures]
        work.extend((profile, profile) for profile in paths)
        outcomes = list(await asyncio.gather(*(bounded(a, b) for a, b in work)))
        points = {name: 0 for name in paths}
        for (a, b, expected_winner), outcome in zip(fixtures, outcomes):
            assert outcome[0] == expected_winner
            points[(a, b)[expected_winner]] += 2
        return outcomes, points

    first = asyncio.run(exercise(1))
    second = asyncio.run(exercise(2))
    assert first == second
    assert first[1] == {"tactical": 8, "steady": 4, "foundation": 0}


def test_showcase_group_quality_rejects_flat_points():
    class StageStore:
        @staticmethod
        def list_stage_results(_contest_id, *, stage_idx):
            assert stage_idx == 0
            return [
                {"group_id": group, "points": 4.0, "rank_in_group": rank}
                for group in ("G1", "G2", "G3", "G4")
                for rank in (1, 2, 3)
            ]

    with pytest.raises(ShowcaseSeedError, match="8/4/0"):
        _verify_group_stage_distribution(StageStore(), 1, label="finished")


def test_partial_rollback_allows_active_bot_and_missing_expected_file_but_not_unknown(
    tmp_path, monkeypatch,
):
    app = create_app(db_path=str(tmp_path / "rollback-scope.db"), max_concurrent=1)
    store = app.state.store
    upload_root = tmp_path / "bot_uploads_showcase"
    validate_showcase_upload_namespace(store, upload_root, create=True)
    organizer = store.create_user(
        "showcase_organizer", "showcase-organizer@invalid.example",
        hash_password(PASSWORD), role="organizer",
    )
    player = store.create_user(
        "showcase_player_01", "showcase-player-01@invalid.example",
        hash_password(PASSWORD), role="user",
    )
    binary = upload_root / "1" / "v1" / "bot.bin"
    bot = store.create_bot(
        player["id"], "showcase_gomoku_01",
        description="合成演示 LongRunning 五子棋 Bot",
        binary_path=str(binary), format="elf", os="linux", arch="amd64",
        game_id="gomoku", runtime_mode="longrunning", is_active=1,
    )
    binary = upload_root / str(bot["id"]) / "v1" / "bot.bin"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"expected file may be missing during recovery")
    store.add_bot_version(
        bot["id"], version=1, binary_path=str(binary),
        checksum=hashlib.sha256(binary.read_bytes()).hexdigest(),
        size_bytes=binary.stat().st_size, runtime_mode="longrunning",
    )
    store.update_bot(bot["id"], binary_path=str(binary), current_version=1)

    unknown = upload_root / "unknown.bin"
    unknown.write_bytes(b"must fail closed")
    with pytest.raises(ShowcaseSeedError, match="非白名单对象"):
        rollback_showcases(store, upload_root)
    unknown.unlink()
    binary.unlink()

    # Presentation quality is intentionally unusable here.  Rollback still
    # succeeds because its frozen scope has no active match or foreign object.
    monkeypatch.setattr(
        showcase_seed_module,
        "_verify_showcase_presentation_quality",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("rollback must not run presentation quality")
        ),
    )
    result = rollback_showcases(store, upload_root)
    assert result == {"contests": 0, "matches": 0, "bots": 1, "users": 2}
    assert store.get_user(int(organizer["id"])) is None
    assert not upload_root.exists()


def test_rollback_resumes_after_first_match_delete_committed(tmp_path, monkeypatch):
    store, upload_root, _organizer, _players, _bots, contest, match_id = (
        _rollback_seed_fixture(tmp_path, with_match=True)
    )
    original_delete = store.delete_match
    crashed = False

    def delete_then_interrupt(target: str) -> bool:
        nonlocal crashed
        deleted = original_delete(target)
        if not crashed:
            crashed = True
            raise RuntimeError("simulated rollback interruption")
        return deleted

    monkeypatch.setattr(store, "delete_match", delete_then_interrupt)
    with pytest.raises(RuntimeError, match="simulated rollback interruption"):
        rollback_showcases(store, upload_root)
    assert store.get_match(match_id) is None
    assert store.get_contest(contest["id"]) is not None

    monkeypatch.setattr(store, "delete_match", original_delete)
    result = rollback_showcases(store, upload_root)
    assert result == {"contests": 1, "matches": 0, "bots": 2, "users": 3}
    assert not upload_root.exists()


def test_rollback_rejects_foreign_player_and_source_contest_references(tmp_path):
    store, upload_root, _organizer, players, _bots, contest, _match_id = (
        _rollback_seed_fixture(tmp_path)
    )
    outsider = _account(store, "rollback_outsider", "organizer")
    first = store.create_bot(
        outsider["id"], "rollback_outside_a", binary_path="/tmp/outside-a",
        format="elf", game_id="gomoku", is_active=1,
    )
    second = store.create_bot(
        outsider["id"], "rollback_outside_b", binary_path="/tmp/outside-b",
        format="elf", game_id="gomoku", is_active=1,
    )
    store.create_match(
        "foreign-player-owner", first["id"], second["id"],
        owner_id=players[0]["id"], game_id="gomoku",
    )
    with pytest.raises(ShowcaseSeedError, match="对局身份引用"):
        rollback_showcases(store, upload_root)
    assert store.get_contest(contest["id"]) is not None
    store.delete_match("foreign-player-owner")

    store.create_match(
        "foreign-player-human", first["id"], second["id"],
        owner_id=outsider["id"], human_user_id=players[0]["id"],
        human_seat=1, match_type="human", game_id="gomoku",
    )
    with pytest.raises(ShowcaseSeedError, match="对局身份引用"):
        rollback_showcases(store, upload_root)
    assert store.get_contest(contest["id"]) is not None
    store.delete_match("foreign-player-human")

    store.create_contest(
        "foreign derived contest", outsider["id"], game_id="gomoku",
        source_contest_id=contest["id"],
    )
    with pytest.raises(ShowcaseSeedError, match="引用演示来源赛事"):
        rollback_showcases(store, upload_root)
    assert store.get_contest(contest["id"]) is not None


def test_strict_profile_rejects_pairing_frozen_to_old_version(tmp_path):
    app = create_app(db_path=str(tmp_path / "frozen-profile.db"), max_concurrent=1)
    store = app.state.store
    upload_root = tmp_path / "bot_uploads_showcase"
    profile = SHOWCASE_PROFILE_SPECS["tactical"]
    profile_raw = (
        Path(__file__).parents[3] / "samples" / "gomoku_showcase" / profile["filename"]
    ).read_bytes()
    old_raw = (Path(__file__).parents[3] / "samples" / "gomokubot_linux_amd64").read_bytes()
    owners = [_account(store, f"frozen_profile_{index}") for index in (1, 2)]
    bots = []
    versions = []
    for index, owner in enumerate(owners, 1):
        bot = store.create_bot(
            owner["id"], f"frozen_profile_bot_{index}", binary_path="",
            format="elf", os="linux", arch="amd64", game_id="gomoku",
            runtime_mode="longrunning", is_active=1,
        )
        bot_versions = []
        for version_number, raw in ((1, old_raw), (2, profile_raw)):
            binary = upload_root / str(bot["id"]) / f"v{version_number}" / "bot.bin"
            binary.parent.mkdir(parents=True, exist_ok=True)
            binary.write_bytes(raw)
            bot_versions.append(store.add_bot_version(
                bot["id"], version=version_number, binary_path=str(binary),
                checksum=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw),
                runtime_mode="longrunning",
            ))
        store.update_bot(
            bot["id"], binary_path=bot_versions[1]["binary_path"], current_version=2,
        )
        bots.append(bot)
        versions.append(bot_versions)
    contest = store.create_contest(
        "frozen profile", owners[0]["id"], game_id="gomoku",
    )
    entries = [
        store.add_contest_entry(contest["id"], owner["id"], bot["id"])
        for owner, bot in zip(owners, bots)
    ]
    store.add_contest_pairing(
        contest["id"], bots[0]["id"], bots[1]["id"],
        entry_a_id=entries[0]["id"], entry_b_id=entries[1]["id"],
        bot_a_version_id=versions[0][0]["id"],
        bot_b_version_id=versions[1][1]["id"],
    )
    for bot in bots:
        store.update_bot(bot["id"], is_active=0)
    with pytest.raises(ShowcaseSeedError, match="冻结版本不属于审核 manifest"):
        _verify_frozen_profile_versions(
            store,
            upload_root,
            {"showcases": {"fixture": {"contest_id": contest["id"]}}},
            {bot["id"]: profile for bot in bots},
        )


def test_showcase_pairing_rejects_cross_bound_dedicated_bot(tmp_path):
    app = create_app(db_path=str(tmp_path / "pairing-identity.db"), max_concurrent=1)
    store = app.state.store
    first_user = _account(store, "pairing_identity_a")
    second_user = _account(store, "pairing_identity_b")
    first = store.create_bot(
        first_user["id"], "pairing_identity_a", binary_path="/tmp/pair-a",
        format="elf", game_id="gomoku", is_active=1,
    )
    second = store.create_bot(
        second_user["id"], "pairing_identity_b", binary_path="/tmp/pair-b",
        format="elf", game_id="gomoku", is_active=1,
    )
    first_version = store.add_bot_version(first["id"], binary_path="/tmp/pair-a")
    second_version = store.add_bot_version(second["id"], binary_path="/tmp/pair-b")
    contest = store.create_contest(
        "pairing identity", first_user["id"], game_id="gomoku",
    )
    first_entry = store.add_contest_entry(contest["id"], first_user["id"], first["id"])
    second_entry = store.add_contest_entry(contest["id"], second_user["id"], second["id"])
    pairing = store.add_contest_pairing(
        contest["id"], second["id"], first["id"],
        entry_a_id=first_entry["id"], entry_b_id=second_entry["id"],
        bot_a_version_id=second_version["id"], bot_b_version_id=first_version["id"],
    )
    with pytest.raises(ShowcaseSeedError, match="entry/A Bot 错绑"):
        _verify_pairing_identity_graph(
            store,
            contest["id"],
            [first_entry, second_entry],
            [pairing],
            [],
            {first_user["id"]: first["id"], second_user["id"]: second["id"]},
        )

    store.update_contest_pairing(
        pairing["id"], bot_a_id=first["id"], bot_b_id=second["id"],
        bot_a_version_id=first_version["id"], bot_b_version_id=second_version["id"],
        match_id="pairing-seat-mismatch",
    )
    store.create_match(
        "pairing-seat-mismatch", second["id"], first["id"],
        game_id="gomoku", match_type="contest", contest_id=contest["id"],
        match_config={
            "_bot_a_version_id": second_version["id"],
            "_bot_b_version_id": first_version["id"],
        },
    )
    corrected_pairing = next(
        row for row in store.list_contest_pairings(contest["id"])
        if row["id"] == pairing["id"]
    )
    with pytest.raises(ShowcaseSeedError, match="pairing/match A 座位或版本错绑"):
        _verify_pairing_identity_graph(
            store,
            contest["id"],
            [first_entry, second_entry],
            [corrected_pairing],
            [store.get_match("pairing-seat-mismatch")],
            {first_user["id"]: first["id"], second_user["id"]: second["id"]},
        )


def test_showcase_rollback_rejects_reserved_identity_collision(tmp_path):
    app = create_app(db_path=str(tmp_path / "rollback-guard.db"), max_concurrent=1)
    store = app.state.store
    store.create_user(
        "showcase_organizer", "somebody@example.com", hash_password(PASSWORD),
        role="organizer",
    )
    with pytest.raises(ShowcaseSeedError, match="身份不匹配"):
        rollback_showcases(store, tmp_path / "uploads", emit=lambda _message: None)


def test_showcase_rest_ui_uses_stable_copy_without_countdown():
    source = (
        Path(__file__).parents[2]
        / "frontend"
        / "src"
        / "pages"
        / "ContestDetail.tsx"
    ).read_text(encoding="utf-8")
    assert "小组赛已结束，等待进入下一阶段；演示快照不会自动倒计时推进。" in source
    assert "contest.status === 'rest' && (isShowcase || contest.rest_ends_at)" in source
