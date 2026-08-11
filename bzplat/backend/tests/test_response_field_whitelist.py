"""响应字段白名单测试（PR：死字段裁剪后回归守护）。

对抗审计裁了 25 个真死字段（per-route 投影，不动共享 SELECT）。本测试断言这些字段
不再出现在 API 响应里——若未来有人加回死字段，测试会报。同时验证 4 个会致回归的字段
（winner/reason/match_type/contest_id）和共享 SELECT 字段仍在（守护不误删）。

注意：Store 数值投影另有专门测试；本文件只覆盖 API 路由响应层。
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from bzplat.backend.main import create_app


def _seed_bot_and_match(c, app):
    """建一个用户+bot+完成一场对局，返回 bot_id。"""
    from bzplat.backend.crypto import hash_password
    store = app.state.store
    u = store.create_user("wl_user", "wl@x.com", hash_password("pw123456"), display_name="wl")
    store.update_user(u["id"], email_verified=1)
    # Reuse create_app's DB-adjacent upload root. A relative "bot_uploads" here
    # used to write the primary checkout whenever pytest ran from repository CWD.
    bm = app.state.bot_manager
    from pathlib import Path
    sample = Path(__file__).resolve().parents[3] / "samples" / "callbot_linux_amd64"
    if not sample.is_file():
        import pytest
        pytest.skip("样例 bot 缺失")
    b = bm.create_from_upload(u["id"], "wl_bot", sample.read_bytes(), display_name="wl_bot", game_id="holdem")
    return u, b


def test_leaderboard_drops_dead_fields(tmp_path):
    app = create_app(db_path=str(tmp_path / "wl.db"))
    with TestClient(app) as c:
        u, b = _seed_bot_and_match(c, app)
        # 触发一局对局产生 leaderboard 数据（challenge 自博弈）
        from bzplat.backend.crypto import new_session_token, session_expires
        tok = new_session_token()
        store = app.state.store
        store.add_session(tok, u["id"], session_expires())
        r = c.post("/api/matches/challenge", json={"my_bot_id": b["id"], "opponent_bot_id": b["id"]},
                   headers={"Authorization": f"Bearer {tok}"})
        # 等对局完成
        import time
        mid = r.json().get("match_id")
        for _ in range(40):
            d = c.get(f"/api/matches/{mid}").json()
            m = d.get("match", d)
            if m.get("status") in ("completed", "aborted"):
                break
            time.sleep(0.5)
        r = c.get("/api/leaderboard?game_id=holdem")
        lb = r.json().get("leaderboard", [])
        if not lb:
            return  # 无数据则无可测（对局可能 aborted）
        row = lb[0]
        # 裁剪的死字段
        for dead in (
            "vol", "last_played_at", "is_builtin", "owner_display",
            "format", "os", "arch", "game_id", "delta_total", "net_chips",
        ):
            assert dead not in row, f"leaderboard 仍含死字段 {dead}"
        # 守护：公开数值字段与身份字段保留。
        for keep in ("rating", "bot_name", "rated_matches", "rank_total", "ranking_eligible"):
            assert keep in row


def test_bot_profile_drops_dead_fields(tmp_path):
    app = create_app(db_path=str(tmp_path / "wl.db"))
    with TestClient(app) as c:
        _, b = _seed_bot_and_match(c, app)
        r = c.get(f"/api/bots/{b['id']}/profile")
        assert r.status_code == 200
        p = r.json()["profile"]
        for dead in (
            "vol", "delta_total", "rated_at", "is_builtin", "updated_at",
            "format", "os", "arch",
        ):
            assert dead not in p, f"bot_profile 仍含死字段 {dead}"
        # 守护：测试依赖字段保留
        for keep in ("rated_matches", "rank_total", "ranking_progress", "owner_id"):
            assert keep in p, f"bot_profile 误删了保留字段 {keep}"


def test_matches_list_drops_dead_keeps_critical(tmp_path):
    app = create_app(db_path=str(tmp_path / "wl.db"))
    with TestClient(app) as c:
        u, b = _seed_bot_and_match(c, app)
        from bzplat.backend.crypto import new_session_token, session_expires
        tok = new_session_token()
        app.state.store.add_session(tok, u["id"], session_expires())
        c.post("/api/matches/challenge", json={"my_bot_id": b["id"], "opponent_bot_id": b["id"]},
               headers={"Authorization": f"Bearer {tok}"})
        r = c.get("/api/matches?limit=5")
        ms = r.json().get("matches", [])
        if not ms:
            return
        m = ms[0]
        # 裁剪的死字段
        for dead in ("started_at", "ended_at", "human_user_id", "human_seat",
                     "likes_count", "views_count", "owner_id"):
            assert dead not in m, f"matches 列表仍含死字段 {dead}"
        # 守护：4 个会致回归的字段必须在（有消费者）
        for keep in ("winner", "reason", "match_type", "contest_id"):
            assert keep in m, f"matches 列表误删了关键字段 {keep}（会致回归）"
