"""管理员批量指派参赛者+Bot 测试（测试期 admin 派遣，正式版用户自己报名）。

覆盖 POST /api/admin/contests/{id}/entries/bulk：
- 显式 entries 列表模式
- assign_all 便捷模式（按 game_id 全选）
- 重复报名跳过 / bot 不可用跳过 / 游戏不匹配跳过
- 非 admin 403 / 赛事不存在 404
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from bzplat.backend.crypto import hash_password, new_session_token, session_expires
from bzplat.backend.main import create_app
from bzplat.backend.store import Store


def _store(tmp_path) -> Store:
    return Store(str(tmp_path / "aa.db"))


def _setup(tmp_path, game: str = "holdem"):
    """建 app + admin + 若干用户（各有该游戏 Bot）+ 赛事（draft）。"""
    s = _store(tmp_path)
    app = create_app(db_path=s.path)
    st = app.state.store
    admin = st.create_user("adminusr", "a@ex.com", hash_password("pw"), role="admin")
    users = []
    for i in range(6):
        u = st.create_user(f"usr{i}", f"u{i}@ex.com", hash_password("pw"))
        b = st.create_bot(u["id"], f"bot{i}", binary_path="/tmp/b", format="elf",
                          is_public=1, is_active=1, game_id=game)
        st.ensure_rating(b["id"])
        users.append((u, b))
    cid = st.create_contest("Cup", organizer_id=admin["id"], game_id=game)["id"]
    # admin session token
    tok = new_session_token()
    st.add_session(tok, admin["id"], session_expires())
    c = TestClient(app)
    return s, st, admin, users, cid, tok, c


def test_admin_assign_explicit_entries(tmp_path):
    s, st, admin, users, cid, tok, c = _setup(tmp_path)
    # 显式指派 user0/user1 的 bot
    r = c.post(f"/api/admin/contests/{cid}/entries/bulk",
               headers={"Authorization": f"Bearer {tok}"},
               json={"entries": [{"user_id": users[0][0]["id"], "bot_id": users[0][1]["id"]},
                                 {"user_id": users[1][0]["id"], "bot_id": users[1][1]["id"]}]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["added"] == 2
    assert body["total_entries"] == 2
    ents = st.list_entries(cid)
    assert len(ents) == 2
    s.close()


def test_admin_assign_all_by_game(tmp_path):
    s, st, admin, users, cid, tok, c = _setup(tmp_path)
    # assign_all 模式：自动找所有 holdem bot
    r = c.post(f"/api/admin/contests/{cid}/entries/bulk",
               headers={"Authorization": f"Bearer {tok}"},
               json={"assign_all": True, "game_id": "holdem"})
    assert r.status_code == 200, r.text
    body = r.json()
    # 6 个用户各 1 个 bot → 6 条
    assert body["added"] == 6
    assert body["total_entries"] == 6
    s.close()


def test_admin_assign_skips_duplicates(tmp_path):
    s, st, admin, users, cid, tok, c = _setup(tmp_path)
    # 先指派 user0
    st.add_entry(cid, users[0][0]["id"], users[0][1]["id"])
    # 再批量指派含 user0 → 应跳过 user0
    r = c.post(f"/api/admin/contests/{cid}/entries/bulk",
               headers={"Authorization": f"Bearer {tok}"},
               json={"entries": [{"user_id": users[0][0]["id"], "bot_id": users[0][1]["id"]},
                                 {"user_id": users[1][0]["id"], "bot_id": users[1][1]["id"]}]})
    assert r.status_code == 200
    body = r.json()
    assert body["added"] == 1  # user0 已存在跳过
    assert any("已报名" in sk for sk in body["skipped"])
    s.close()


def test_admin_assign_skips_game_mismatch(tmp_path):
    s, st, admin, users, cid, tok, c = _setup(tmp_path, game="holdem")
    # 建一个 gomoku bot，指派进 holdem 赛事 → 应跳过
    ug = st.create_user("gomokuusr", "ug@ex.com", hash_password("pw"))
    bg = st.create_bot(ug["id"], "gobot", binary_path="/tmp/g", format="elf",
                       is_public=1, is_active=1, game_id="gomoku")
    r = c.post(f"/api/admin/contests/{cid}/entries/bulk",
               headers={"Authorization": f"Bearer {tok}"},
               json={"entries": [{"user_id": ug["id"], "bot_id": bg["id"]}]})
    assert r.status_code == 200
    body = r.json()
    assert body["added"] == 0
    assert any("游戏" in sk for sk in body["skipped"])
    s.close()


def test_admin_assign_requires_admin(tmp_path):
    s, st, admin, users, cid, _atok, c = _setup(tmp_path)
    # 普通用户 token → 403
    utok = new_session_token()
    st.add_session(utok, users[0][0]["id"], session_expires())
    r = c.post(f"/api/admin/contests/{cid}/entries/bulk",
               headers={"Authorization": f"Bearer {utok}"},
               json={"assign_all": True, "game_id": "holdem"})
    assert r.status_code == 403
    s.close()


def test_admin_assign_contest_not_found(tmp_path):
    s, st, admin, users, cid, tok, c = _setup(tmp_path)
    r = c.post("/api/admin/contests/99999/entries/bulk",
               headers={"Authorization": f"Bearer {tok}"},
               json={"assign_all": True, "game_id": "holdem"})
    assert r.status_code == 404
    s.close()
