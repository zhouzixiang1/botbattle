"""Read-only lifecycle showcase snapshots and stage-history presentation."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bzplat.backend.contests.scheduler import ContestScheduler
from bzplat.backend.contests.showcase_seed import (
    ShowcaseSeedError,
    _recover_incomplete_showcases,
    rollback_showcases,
)
from bzplat.backend.crypto import hash_password
from bzplat.backend.main import create_app


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

    client = TestClient(app)
    visitor_list = client.get("/api/contests").json()["contests"]
    assert opened["id"] in {row["id"] for row in visitor_list}
    assert client.get(f"/api/contests/{draft['id']}").status_code == 404

    normal = _headers(app, "showcase_user")
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

    organizer = _headers(app, "showcase_org")
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

    admin = _headers(app, "showcase_admin")
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
    listed = next(
        row for row in client.get("/api/contests").json()["contests"]
        if row["id"] == contest["id"]
    )
    assert detail["description"] == "合成演示快照（只读，不代表真实活动赛事）。"
    assert listed["description"] == detail["description"]


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
