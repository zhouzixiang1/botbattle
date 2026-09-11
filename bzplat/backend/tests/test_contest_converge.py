"""组织者/管理员强制收束（converge）测试。

converge = 裁量性收束：取消排队 job、未开打对阵标记“未进行”（voided_at）、
中止在途对局，然后按已完场成绩固化正式名次（rank 1..N 全名册）。与
finish（要求全部该打对局终态的恢复性收尾）语义并存且互不放松。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from bzplat.backend.contests.manager import ContestManager
from bzplat.backend.crypto import hash_password
from bzplat.backend.matches.orchestrator import MatchOrchestrator
from bzplat.backend.store import Store
from bzplat.backend.store.schema import (
    EXECUTION_SOURCE_CONTEST,
    TYPE_CONTEST,
)

_STAGE = {
    "key": "dup_rr",
    "type": "round_robin",
    "scoring": "poker_3_1_0",
    "duplicate": True,
    "games_per_pair": 1,
    "series_scoring": "independent_scoring_game_points_v1",
}


def _bind_completed(store: Store, contest_id: int, pairing: dict, match_id: str):
    store.create_match(
        match_id,
        pairing["bot_a_id"],
        pairing["bot_b_id"],
        owner_id=None,
        contest_id=contest_id,
        match_type="contest",
        game_id="holdem",
        match_config={
            "duplicate": True,
            "_bot_a_version_id": pairing["bot_a_version_id"],
            "_bot_b_version_id": pairing["bot_b_version_id"],
        },
    )
    store.bind_contest_pairing_match(
        contest_id, pairing["id"], match_id, require_execution_admission=False
    )
    store.update_match(
        match_id,
        status="completed",
        winner=None,
        result={
            "rounds_played": 140,
            "deltas": [20, -20],
            "legs": [
                {"winner": 0, "deltas": [10, -10], "rounds_played": 70},
                {"winner": 0, "deltas": [10, -10], "rounds_played": 70},
            ],
        },
        ended_at="2026-09-11T10:00:00",
    )
    assert store.complete_contest_pairing_for_match(contest_id, match_id)


def _setup_contest(tmp_path, *, people=4):
    store = Store(str(tmp_path / "converge.db"))
    organizer = store.create_user(
        "cvorg", "cvorg@e.com", hash_password("pw123456")
    )
    store.update_user(organizer["id"], role="organizer", email_verified=1)
    from bzplat.backend.tests.test_execution_queue import _bot

    actors = [_bot(store, f"cv{index}") for index in range(people)]
    users = [{"id": a["user_id"]} for a in actors]
    bots = [
        {"id": a["bot_id"], "version_id": a["version_id"]} for a in actors
    ]
    contest = store.create_contest(
        "converge 赛", organizer["id"], status="published",
        game_id="holdem", template_id="holdem_dup_rr",
        stages_json=json.dumps([_STAGE]),
    )
    entries = [
        store.add_contest_entry(contest["id"], u["id"], b["id"])
        for u, b in zip(users, bots)
    ]
    pairs = [(0, 1), (0, 2), (0, 3), (1, 2), (1, 3), (2, 3)]
    pairings = store.create_contest_stage_pairings(
        contest["id"], 0,
        [
            {
                "bot_a_id": bots[f]["id"], "bot_b_id": bots[s]["id"],
                "bot_a_version_id": bots[f]["version_id"],
                "bot_b_version_id": bots[s]["version_id"],
                "entry_a_id": entries[f]["id"], "entry_b_id": entries[s]["id"],
                "stage_idx": 0, "stage_key": "dup_rr", "round_num": n,
                "pairing_seed": 9000 + n, "published_at": "2026-09-11T09:00:00",
            }
            for n, (f, s) in enumerate(pairs, start=1)
        ],
        expected_current_stage_idx=0,
        expected_status="published",
        activate_running=True,
    )
    assert store.contest_stage_manifest_is_valid(contest["id"], 0)
    # 测试库无 dispatcher：直接开放接单（与其他 enqueue 型测试同法）
    with store._tx() as conn:
        conn.execute(
            "UPDATE execution_control SET dispatcher_state='running',"
            "accepting=1 WHERE singleton=1"
        )
    with store._tx() as conn:
        conn.execute(
            "UPDATE contests SET published_stage_pairing_count=?, "
            "sealed_pairing_topology_revision=pairing_topology_revision "
            "WHERE id=?",
            (len(pairings), contest["id"]),
        )
    return store, organizer, contest, bots, entries, pairings


def _mgr(store):
    return ContestManager(store, MatchOrchestrator(store))


def test_converge_voids_unplayed_and_finalizes_partial_results(tmp_path):
    store, organizer, contest, bots, entries, pairings = _setup_contest(tmp_path)
    cid = contest["id"]

    # 已完场 1 组（复式两场 bot0 双胜）；1 组在途；其余 4 组排队
    _bind_completed(store, cid, pairings[0], "cv-done-1")
    store.create_match(
        "cv-inflight", pairings[1]["bot_a_id"], pairings[1]["bot_b_id"],
        owner_id=None, contest_id=cid, match_type="contest",
        game_id="holdem",
        match_config={
            "duplicate": True,
            "_bot_a_version_id": pairings[1]["bot_a_version_id"],
            "_bot_b_version_id": pairings[1]["bot_b_version_id"],
        },
    )
    store.bind_contest_pairing_match(
        cid, pairings[1]["id"], "cv-inflight",
        require_execution_admission=False,
    )
    store.update_match("cv-inflight", status="running", started_at="2026-09-11T10:00:00")
    queued_jobs = [
        store.executions.enqueue(
            source=EXECUTION_SOURCE_CONTEST,
            owner_user_id=organizer["id"],
            game_id="holdem",
            match_type=TYPE_CONTEST,
            bot_a_id=p["bot_a_id"], bot_b_id=p["bot_b_id"],
            bot_a_version_id=p["bot_a_version_id"],
            bot_b_version_id=p["bot_b_version_id"],
            contest_id=cid, contest_pairing_id=p["id"],
        )
        for p in pairings[2:]
    ]
    assert len(queued_jobs) == 4

    result = asyncio.run(_mgr(store).converge(cid))

    # 终态 + 正式名次（全名册 rank 1..N）
    final = store.get_contest(cid)
    assert final["status"] == "finished"
    assert final["official_results_ready"] == 1
    official = store.list_official_results(cid)
    ranks = sorted(r["rank"] for r in official)
    assert ranks == list(range(1, len(entries) + 1))
    # 排队 job 全部取消且原因明确
    for job in queued_jobs:
        row = store.executions.get(job["public_id"])
        assert row["status"] == "cancelled"
        assert row["terminal_reason"] == "contest_converged"
    # 在途对局被中止，对应 pairing void
    assert store.get_match("cv-inflight")["status"] == "aborted"
    # 已完场行不动；其余全部 void；从未派发的 queued 请求方也可见取消
    rows = {
        p["id"]: p
        for p in store.list_contest_pairings(cid, stage_idx=0)
    }
    assert rows[pairings[0]["id"]]["voided_at"] is None
    assert rows[pairings[0]["id"]]["status"] == "completed"
    for idx in (1, 2, 3, 4, 5):
        assert rows[pairings[idx]["id"]]["voided_at"] is not None
    assert not store.contest_has_active_matches(cid)
    del result


def test_converge_rejects_terminal_showcase_and_contrast_with_finish(tmp_path):
    store, organizer, contest, bots, entries, pairings = _setup_contest(tmp_path)
    cid = contest["id"]

    # finish 在有未打对阵时依旧拒绝（语义不放松）
    with pytest.raises(ValueError, match="未完成对阵"):
        asyncio.run(_mgr(store).finish(cid))

    # showcase 只读拒绝
    store.freeze_contest_showcase(cid, "converge-showcase")
    with pytest.raises(ValueError, match="只读|演示"):
        asyncio.run(_mgr(store).converge(cid))

    # 已 finished 的赛事再 converge 拒绝（幂等重放不给静默成功）
    again = tmp_path / "again"
    again.mkdir()
    store2, _org2, contest2, *_ = _setup_contest(again)
    asyncio.run(_mgr(store2).converge(contest2["id"]))
    assert store2.get_contest(contest2["id"])["status"] == "finished"
    with pytest.raises(ValueError, match="仅运行中/休息中"):
        asyncio.run(_mgr(store2).converge(contest2["id"]))


def test_converge_endpoint_permissions_and_audit(tmp_path):
    from bzplat.backend.main import create_app
    import os
    os.environ["BZ_BOT_LOCAL"] = "1"
    os.environ["BZ_SKIP_CAPTCHA"] = "1"
    app = create_app(db_path=str(tmp_path / "converge-api.db"))
    store = app.state.store
    organizer = store.create_user("apiorg", "a@e.com", hash_password("pw123456"))
    store.update_user(organizer["id"], role="organizer", email_verified=1)
    stranger = store.create_user("stranger", "s@e.com", hash_password("pw123456"))
    store.update_user(stranger["id"], role="organizer", email_verified=1)
    contest = store.create_contest(
        "api赛", organizer["id"], status="running", game_id="holdem",
        template_id="holdem_dup_rr", stages_json=json.dumps([_STAGE]),
    )
    client = TestClient(app)
    # 未登录 401；他人组织者 403；不存在 404
    assert client.post(f"/api/contests/{contest['id']}/converge").status_code == 401

    def _tok(username):
        _, t = app.state.auth.authenticate(username, "pw123456")
        return {"Authorization": f"Bearer {t}"}

    assert client.post(
        f"/api/contests/{contest['id']}/converge", headers=_tok("stranger")
    ).status_code == 403
    assert client.post(
        "/api/contests/999999/converge", headers=_tok("apiorg")
    ).status_code == 404


def test_legacy_pairings_gain_voided_at_column_idempotently(tmp_path):
    """旧库缺 voided_at 列时重开自动补列；再次重开不重复迁移。"""
    db_path = str(tmp_path / "legacy.db")
    store = Store(db_path)
    store.close()
    conn = sqlite3.connect(db_path)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(contest_pairings)")]
    assert "voided_at" in cols
    conn.execute("ALTER TABLE contest_pairings DROP COLUMN voided_at")
    conn.commit()
    conn.close()

    store = Store(db_path)
    conn = sqlite3.connect(db_path)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(contest_pairings)")]
    assert "voided_at" in cols
    version = conn.execute("PRAGMA schema_version").fetchone()[0]
    conn.close()
    store.close()

    store = Store(db_path)
    conn = sqlite3.connect(db_path)
    assert conn.execute("PRAGMA schema_version").fetchone()[0] == version
    conn.close()
    store.close()
