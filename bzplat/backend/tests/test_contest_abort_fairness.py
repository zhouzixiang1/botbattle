"""赛事中止与 Bot 缺失的公平性回归。"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bzplat.backend.contests.manager import ContestManager
from bzplat.backend.crypto import hash_password, new_session_token, session_expires
from bzplat.backend.main import create_app
from bzplat.backend.store import Store


class _NeverChallenge:
    async def challenge(self, *_args, **_kwargs):  # pragma: no cover - 失败时才调
        raise AssertionError("Bot 不可用时不应启动真实 runner")


def _user_and_bot(store: Store, suffix: str, *, role: str = "user") -> tuple[dict, dict]:
    fixture_dir = Path(store.path).resolve().parent / "bot-fixtures"
    fixture_dir.mkdir(parents=True, exist_ok=True)
    binary_path = fixture_dir / f"fair-{suffix}"
    binary_path.write_bytes(b"test fixture")
    user = store.create_user(
        f"fair{suffix}",
        f"fair{suffix}@example.com",
        hash_password("pw123456"),
        role=role,
    )
    store.update_user(user["id"], email_verified=1)
    bot = store.create_bot(
        user["id"],
        f"fairbot{suffix}",
        binary_path=str(binary_path),
        format="elf",
        is_active=1,
        game_id="holdem",
    )
    return user, bot


def _ko_contest(store: Store, organizer: dict, players: list[tuple[dict, dict]]) -> tuple[int, dict]:
    contest = store.create_contest(
        "公平中止赛",
        organizer["id"],
        status="running",
        game_id="holdem",
        stages_json=json.dumps([{"key": "ko", "type": "single_elimination"}]),
    )
    entries = [
        store.add_contest_entry(contest["id"], user["id"], bot["id"])
        for user, bot in players
    ]
    pairing = store.add_contest_pairing(
        contest["id"],
        players[0][1]["id"],
        players[1][1]["id"],
        status="pending",
        stage_idx=0,
        stage_key="ko",
        entry_a_id=entries[0]["id"],
        entry_b_id=entries[1]["id"],
    )
    return contest["id"], pairing


def test_admin_abort_keeps_history_and_redispatches_without_ko_advance(tmp_path):
    """2 人 KO 中止不能固定晋级座位 0；旧局保留，pairing 安全重派。"""
    app = create_app(db_path=str(tmp_path / "admin-abort-ko.db"))
    store = app.state.store
    admin, bot_a = _user_and_bot(store, "admin", role="admin")
    user_b, bot_b = _user_and_bot(store, "player")
    token = new_session_token()
    store.add_session(token, admin["id"], session_expires())
    contest_id, pairing = _ko_contest(
        store, admin, [(admin, bot_a), (user_b, bot_b)]
    )
    old_match_id = "admin-abort-ko-old"
    store.create_match(
        old_match_id,
        bot_a["id"],
        bot_b["id"],
        owner_id=admin["id"],
        contest_id=contest_id,
        match_type="contest",
        game_id="holdem",
    )
    store.bind_contest_pairing_match(contest_id, pairing["id"], old_match_id)

    # 重派只验证 prepare + bind；不在单测里真启动二进制 Bot。
    app.state.orch.start_prepared_match = lambda _mid: None
    response = TestClient(app).patch(
        f"/api/admin/matches/{old_match_id}",
        json={"status": "aborted"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text
    old_match = store.get_match(old_match_id)
    assert old_match and old_match["status"] == "aborted"
    assert old_match["winner"] is None
    refreshed = store.list_contest_pairings(contest_id, stage_idx=0)
    assert len(refreshed) == 1, "中止不得生成下一轮/决赛对阵"
    assert refreshed[0]["id"] == pairing["id"]
    assert refreshed[0]["status"] == "running"
    assert refreshed[0]["match_id"] not in (None, old_match_id)
    replacement = store.get_match(refreshed[0]["match_id"])
    assert replacement and replacement["status"] == "pending"
    assert store.get_contest(contest_id)["status"] == "running"
    assert store.list_official_results(contest_id) == []


def test_platform_error_aborted_match_is_not_immediately_redispatched(tmp_path):
    """平台故障不得在 on_match_done 回调栈高速循环创建对局。"""
    store = Store(str(tmp_path / "platform-error-no-spin.db"))
    organizer, bot_a = _user_and_bot(store, "platforma", role="organizer")
    user_b, bot_b = _user_and_bot(store, "platformb")
    contest_id, pairing = _ko_contest(
        store, organizer, [(organizer, bot_a), (user_b, bot_b)]
    )
    failed_id = "platform-error-once"
    store.create_match(
        failed_id,
        bot_a["id"],
        bot_b["id"],
        owner_id=organizer["id"],
        contest_id=contest_id,
        match_type="contest",
        game_id="holdem",
    )
    store.bind_contest_pairing_match(contest_id, pairing["id"], failed_id)
    store.update_match(
        failed_id,
        status="aborted",
        reason="platform_error",
        winner=None,
    )
    manager = ContestManager(store, _NeverChallenge())  # type: ignore[arg-type]

    asyncio.run(manager.handle_match_done(failed_id, contest_id))

    failed = store.get_match(failed_id)
    assert failed and failed["status"] == "aborted" and failed["winner"] is None
    refreshed = store.list_contest_pairings(contest_id)[0]
    assert refreshed["status"] == "pending"
    assert refreshed["match_id"] is None
    assert refreshed["scheduled_at"], "平台故障重试必须有最小退避"
    assert [row["id"] for row in store.list_matches(contest_id=contest_id)] == [
        failed_id
    ]
    assert store.get_contest(contest_id)["status"] == "running"
    standings = manager.standings(contest_id)
    assert all(row["points"] == 0 and row["wins"] == 0 for row in standings)
    assert store.list_official_results(contest_id) == []


@pytest.mark.parametrize("endpoint,initial_status", [("publish", "open"), ("start", "draft")])
def test_publish_and_start_reject_unavailable_roster_without_state_change(
    tmp_path, endpoint: str, initial_status: str
):
    """发布/开赛必须在锁内复核 active+binary Bot，400 时不产生副作用。"""
    app = create_app(db_path=str(tmp_path / f"reject-{endpoint}.db"))
    store = app.state.store
    organizer, bot_a = _user_and_bot(store, f"org{endpoint}", role="organizer")
    user_b, bot_b = _user_and_bot(store, f"p{endpoint}")
    token = new_session_token()
    store.add_session(token, organizer["id"], session_expires())
    contest = store.create_contest(
        f"不可用-{endpoint}",
        organizer["id"],
        status=initial_status,
        game_id="holdem",
        stages_json=json.dumps([{"key": "ko", "type": "single_elimination"}]),
    )
    store.add_contest_entry(contest["id"], organizer["id"], bot_a["id"])
    store.add_contest_entry(contest["id"], user_b["id"], bot_b["id"])
    store.update_bot(bot_b["id"], is_active=0)
    before = store.get_contest(contest["id"])

    response = TestClient(app).post(
        f"/api/contests/{contest['id']}/{endpoint}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 400, response.text
    assert "可用 Bot" in response.text
    after = store.get_contest(contest["id"])
    assert after["status"] == initial_status
    assert after.get("registration_closes_at") == before.get("registration_closes_at")
    assert after.get("starts_at") == before.get("starts_at")
    assert store.list_contest_pairings(contest["id"]) == []


def test_mid_contest_single_unavailable_bot_is_completed_technical_loss(tmp_path):
    """中途单侧 Bot 不可用有明确 winner，可合法推进 KO。"""
    store = Store(str(tmp_path / "single-unavailable.db"))
    organizer, bot_a = _user_and_bot(store, "singlea", role="organizer")
    user_b, bot_b = _user_and_bot(store, "singleb")
    contest_id, pairing = _ko_contest(
        store, organizer, [(organizer, bot_a), (user_b, bot_b)]
    )
    store.update_bot(bot_b["id"], is_active=0)
    manager = ContestManager(store, _NeverChallenge())  # type: ignore[arg-type]

    asyncio.run(manager._dispatch_pending(contest_id, 0))

    refreshed = store.list_contest_pairings(contest_id)[0]
    match = store.get_match(refreshed["match_id"])
    assert match and match["status"] == "completed"
    assert match["winner"] == 0
    assert int(match["technical_loss"]) == 1
    assert match["bot_a_id"] == bot_a["id"]
    assert match["bot_b_id"] == bot_b["id"]
    assert store.get_contest(contest_id)["status"] == "finished"


def test_mid_contest_both_unavailable_bots_block_without_fake_match(
    tmp_path, caplog: pytest.LogCaptureFixture
):
    """双方都不可用时无法公平裁决：保留 pending，无 id=0 伪 match。"""
    store = Store(str(tmp_path / "both-unavailable.db"))
    organizer, bot_a = _user_and_bot(store, "botha", role="organizer")
    user_b, bot_b = _user_and_bot(store, "bothb")
    contest_id, pairing = _ko_contest(
        store, organizer, [(organizer, bot_a), (user_b, bot_b)]
    )
    store.update_bot(bot_a["id"], is_active=0)
    store.update_bot(bot_b["id"], is_active=0)
    manager = ContestManager(store, _NeverChallenge())  # type: ignore[arg-type]

    with caplog.at_level(logging.ERROR):
        asyncio.run(manager._dispatch_pending(contest_id, 0))

    refreshed = store.list_contest_pairings(contest_id)[0]
    assert refreshed["id"] == pairing["id"]
    assert refreshed["status"] == "pending"
    assert refreshed["match_id"] is None
    assert store.list_matches(contest_id=contest_id) == []
    assert store.get_contest(contest_id)["status"] == "running"
    assert "both bots unavailable" in caplog.text
