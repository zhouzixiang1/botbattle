"""预赛/决赛 P5：预赛/决赛模板 + 组织者名单 + 大 RR 旁路测试。

验证：
1. holdem_prelim_swiss / holdem_final_ranked 模板存在且 phase 正确
2. 组织者名单 API（POST/DELETE entries，权限校验，状态门）
3. allow_large_round_robin 旁路 FULL_RR_MAX_N
4. 决赛 replace_top 合成榜
"""
from __future__ import annotations

from bzplat.backend.contests.templates import DEFAULT_TEMPLATES
from bzplat.backend.crypto import hash_password
from bzplat.backend.games import registry
from fastapi.testclient import TestClient


def _app(tmp_path):
    from bzplat.backend.main import create_app

    return create_app(db_path=str(tmp_path / "p5.db"))


def test_prelim_final_templates_exist():
    """两个内置预赛/决赛模板存在且 phase 正确。"""
    ids = {t["id"]: t for t in registry.get("holdem").templates}
    assert "holdem_prelim_swiss" in ids
    assert ids["holdem_prelim_swiss"]["phase"] == "preliminary"
    assert "holdem_final_ranked" in ids
    assert ids["holdem_final_ranked"]["phase"] == "final"
    # 决赛 stage1 有 allow_large_round_robin
    final = ids["holdem_final_ranked"]
    assert final["stages"][0].get("allow_large_round_robin") is True
    assert final["stages"][1].get("ranking_mode") == "replace_top"


def test_create_contest_with_prelim_template_sets_phase(tmp_path):
    """用 holdem_prelim_swiss 建赛 → phase=preliminary（从模板派生）。"""
    app = _app(tmp_path)
    store = app.state.store
    o = store.create_user("org", "o@ex.com", hash_password("pw123456"), role="organizer")
    store.update_user(o["id"], email_verified=1)
    client = TestClient(app)
    _, tok = app.state.auth.authenticate("org", "pw123456")
    h = {"Authorization": f"Bearer {tok}"}
    r = client.post(
        "/api/contests",
        json={"title": "预赛测试", "template_id": "holdem_prelim_swiss"},
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert r.json()["contest"]["phase"] == "preliminary"


def test_organizer_entries_api_permissions(tmp_path):
    """组织者名单 API：组织者可加/删，非组织者拒，开赛后拒改名册。"""
    app = _app(tmp_path)
    store = app.state.store
    o = store.create_user("org", "o@ex.com", hash_password("pw123456"), role="organizer")
    store.update_user(o["id"], email_verified=1)
    other = store.create_user("other", "ot@ex.com", hash_password("pw123456"), role="organizer")
    store.update_user(other["id"], email_verified=1)
    u1 = store.create_user("usr1", "usr1@ex.com", hash_password("pw123456"))
    store.update_user(u1["id"], email_verified=1)
    b1 = store.create_bot(u1["id"], "usrb1", binary_path="/tmp", format="elf", game_id="holdem")
    c = store.create_contest("P5名单", organizer_id=o["id"], game_id="holdem")["id"]
    client = TestClient(app)
    _, tok = app.state.auth.authenticate("org", "pw123456")
    _, otok = app.state.auth.authenticate("other", "pw123456")
    h = {"Authorization": f"Bearer {tok}"}
    oh = {"Authorization": f"Bearer {otok}"}
    # 非组织者拒
    r = client.post(
        f"/api/contests/{c}/entries",
        json={"user_id": u1["id"], "bot_id": b1["id"]}, headers=oh,
    )
    assert r.status_code == 403
    # 组织者可加
    r = client.post(
        f"/api/contests/{c}/entries",
        json={"user_id": u1["id"], "bot_id": b1["id"]}, headers=h,
    )
    assert r.status_code == 200, r.text
    # 开赛后拒
    store.update_contest(c, status="running")
    r = client.post(
        f"/api/contests/{c}/entries",
        json={"user_id": u1["id"], "bot_id": b1["id"]}, headers=h,
    )
    assert r.status_code == 400


def test_organizer_bulk_and_delete(tmp_path):
    """组织者 bulk assign + delete entry。"""
    app = _app(tmp_path)
    store = app.state.store
    o = store.create_user("org", "o@ex.com", hash_password("pw123456"), role="organizer")
    store.update_user(o["id"], email_verified=1)
    u1 = store.create_user("u1b", "u1b@ex.com", hash_password("pw123456"))
    store.update_user(u1["id"], email_verified=1)
    b1 = store.create_bot(u1["id"], "usrb1b", binary_path="/tmp", format="elf", game_id="holdem")
    c = store.create_contest("P5bulk", organizer_id=o["id"], game_id="holdem")["id"]
    client = TestClient(app)
    _, tok = app.state.auth.authenticate("org", "pw123456")
    h = {"Authorization": f"Bearer {tok}"}
    # bulk
    r = client.post(
        f"/api/contests/{c}/entries/bulk",
        json={"entries": [{"user_id": u1["id"], "bot_id": b1["id"]}]}, headers=h,
    )
    assert r.status_code == 200
    assert r.json()["added"] == 1
    # delete
    r = client.delete(f"/api/contests/{c}/entries/{u1['id']}", headers=h)
    assert r.status_code == 200


def test_allow_large_round_robin_bypasses_guard(tmp_path):
    """allow_large_round_robin=True 旁路 FULL_RR_MAX_N（仅白名单模板）。
    建 holdem_final_ranked（stage1 allow_large_rr）+ 20 人报名 → 不被 _guard_full_rr 拒。
    """
    import asyncio

    from bzplat.backend.contests.manager import ContestManager
    from bzplat.backend.matches.orchestrator import MatchOrchestrator
    from bzplat.backend.matches.runner import MatchRunner
    from bzplat.backend.runtime.binary_runner import BinaryRunner

    app = _app(tmp_path)
    store = app.state.store
    o = store.create_user("org2", "o2@ex.com", hash_password("pw123456"), role="organizer")
    store.update_user(o["id"], email_verified=1)
    # 建 13 人（> FULL_RR_MAX_N=12，普通 RR 会被拒）
    users = []
    for i in range(13):
        u = store.create_user(f"lr{i}", f"lr{i}@ex.com", hash_password("pw123456"))["id"]
        store.update_user(u, email_verified=1)
        b = store.create_bot(u, f"lrb{i}", binary_path="/tmp", format="elf", game_id="holdem")["id"]
        users.append(u)
    c = store.create_contest(
        "P5大RR", organizer_id=o["id"], game_id="holdem",
        stages_json='[{"key":"q","type":"round_robin","allow_large_round_robin":true,"advance_count":2,"scoring":"poker_3_1_0"}]',
    )["id"]
    for uid in users:
        store.add_contest_entry(c, uid, store.get_bot_by_owner_name(uid, f"lrb{users.index(uid)}")["id"])
    orch = MatchOrchestrator(store, runner=MatchRunner(BinaryRunner(prefer_local=True)), max_concurrent=1)
    cm = ContestManager(store, orch)
    store.update_contest(c, status="open")
    # start 应不抛 _guard_full_rr 错（allow_large_round_robin 旁路）
    asyncio.run(cm.start(c))
    assert store.get_contest(c)["status"] == "running"
