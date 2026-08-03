"""组织者单条加人校验测试（审计 P1：organizer_add_entry 缺校验→500）。

覆盖 POST /api/contests/{id}/entries：
- 缺 user_id/bot_id → 400（原 int(None) → TypeError → 500）
- 非整数 → 400
- user 不存在 → 400
- 正常加人 → 200
- 重复报名 → 400
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from bzplat.backend.crypto import hash_password
from bzplat.backend.main import create_app
from bzplat.backend.store import Store


def _app(tmp_path):
    db = str(tmp_path / "oe.db")
    app = create_app(db_path=db)
    st = app.state.store
    org = st.create_user("organizer", "o@ex.com", hash_password("pw"), role="organizer")
    st.update_user(org["id"], email_verified=1)
    u = st.create_user("player1", "p@ex.com", hash_password("pw"))
    st.update_user(u["id"], email_verified=1)
    b = st.create_bot(u["id"], "p1bot", binary_path="/tmp/b", format="elf", game_id="holdem")
    cid = st.create_contest("Cup", organizer_id=org["id"], game_id="holdem")["id"]
    _, tok = app.state.auth.authenticate("organizer", "pw")
    c = TestClient(app)
    return c, cid, u, b, tok


def test_missing_user_id_returns_400(tmp_path):
    c, cid, u, b, tok = _app(tmp_path)
    r = c.post(
        f"/api/contests/{cid}/entries",
        json={"bot_id": b["id"]},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 400


def test_missing_bot_id_returns_400(tmp_path):
    c, cid, u, b, tok = _app(tmp_path)
    r = c.post(
        f"/api/contests/{cid}/entries",
        json={"user_id": u["id"]},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 400


def test_non_integer_ids_returns_400(tmp_path):
    c, cid, u, b, tok = _app(tmp_path)
    r = c.post(
        f"/api/contests/{cid}/entries",
        json={"user_id": "abc", "bot_id": b["id"]},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 400


def test_nonexistent_user_returns_400(tmp_path):
    c, cid, u, b, tok = _app(tmp_path)
    r = c.post(
        f"/api/contests/{cid}/entries",
        json={"user_id": 99999, "bot_id": b["id"]},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 400


def test_valid_entry_succeeds(tmp_path):
    c, cid, u, b, tok = _app(tmp_path)
    r = c.post(
        f"/api/contests/{cid}/entries",
        json={"user_id": u["id"], "bot_id": b["id"]},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_duplicate_entry_returns_400(tmp_path):
    c, cid, u, b, tok = _app(tmp_path)
    # 第一次成功
    c.post(
        f"/api/contests/{cid}/entries",
        json={"user_id": u["id"], "bot_id": b["id"]},
        headers={"Authorization": f"Bearer {tok}"},
    )
    # 重复 → 400
    r = c.post(
        f"/api/contests/{cid}/entries",
        json={"user_id": u["id"], "bot_id": b["id"]},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 400
