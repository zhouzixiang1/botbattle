"""服务端分页测试（PR fix/audit-bugs-pagination）。

为原先返回无界列表的端点加上 ``page``/``per_page`` 分页：
响应契约（提供 page 时）= ``{<key>:[...], page, per_page, total}``；
不提供 page 时保持旧行为（仅返回 ``{<key>:[...]}``）。

覆盖端点：
- GET /api/bots/mine
- GET /api/bots/public
- GET /api/users/{username}/bots
- GET /api/leaderboard
- GET /api/comments
- GET /api/notifications
- GET /api/bots/{id}/matches
- GET /api/bots/{id}/opponents
- GET /api/contests/{id}（entries 子分页）
- GET /api/admin/users
- GET /api/admin/bots（已支持，回归校验）
- GET /api/admin/contests（已支持，回归校验）

通用断言：page=2 返回不同行；per_page 上限钳到 200；省略 page 时旧契约不变。
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from bzplat.backend.crypto import hash_password
from bzplat.backend.main import create_app


# ── 公共 fixture ───────────────────────────────────────────────

def _new_app(tmp_path, db_name="pg.db"):
    return create_app(db_path=str(tmp_path / db_name))


def _admin_and_token(app, name="pgadmin", pw="pw123456"):
    store = app.state.store
    a = store.create_user(name, f"{name}@ex.com", hash_password(pw), role="admin")
    store.update_user(a["id"], email_verified=1)
    _, tok = app.state.auth.authenticate(name, pw)
    return a, {"Authorization": f"Bearer {tok}"}


def _login(app, name, pw="pw123456"):
    _, tok = app.state.auth.authenticate(name, pw)
    return {"Authorization": f"Bearer {tok}"}


def _seed_bots(app, count=20, owner=None, game_id="holdem", prefix="usr"):
    """建 count 个 bot（各自有 owner），返回 (users, bots)。"""
    store = app.state.store
    users, bots = [], []
    owner = owner or store.create_user(
        f"{prefix}_owner", f"{prefix}_o@ex.com", hash_password("pw123456")
    )
    store.update_user(owner["id"], email_verified=1)
    users.append(owner)
    for i in range(count):
        u = store.create_user(f"{prefix}{i:02d}", f"{prefix}{i}@ex.com", hash_password("pw123456"))
        store.update_user(u["id"], email_verified=1)
        b = store.create_bot(
            u["id"], f"{prefix}bot{i:02d}", binary_path="/tmp", format="elf", game_id=game_id,
        )
        users.append(u)
        bots.append(b)
    return users, bots


# ── /api/bots/mine ─────────────────────────────────────────────

def test_my_bots_pagination(tmp_path):
    app = _new_app(tmp_path)
    store = app.state.store
    owner = store.create_user("mine", "m@ex.com", hash_password("pw123456"))
    store.update_user(owner["id"], email_verified=1)
    for i in range(12):
        store.create_bot(owner["id"], f"mb{i}", binary_path="/tmp", format="elf", game_id="holdem")
    h = _login(app, "mine")
    c = TestClient(app)

    # 分页契约
    p1 = c.get("/api/bots/mine?page=1&per_page=5", headers=h).json()
    assert p1["total"] == 12
    assert p1["page"] == 1
    assert p1["per_page"] == 5
    assert len(p1["bots"]) == 5

    # page=2 不同行
    p2 = c.get("/api/bots/mine?page=2&per_page=5", headers=h).json()
    assert len(p2["bots"]) == 5
    names1 = {b["name"] for b in p1["bots"]}
    names2 = {b["name"] for b in p2["bots"]}
    assert names1.isdisjoint(names2)

    # 末页
    p3 = c.get("/api/bots/mine?page=3&per_page=5", headers=h).json()
    assert len(p3["bots"]) == 2  # 12 - 10

    # 向后兼容：不传 page → 旧形状
    old = c.get("/api/bots/mine", headers=h).json()
    assert "page" not in old
    assert len(old["bots"]) == 12


# ── /api/bots/public ───────────────────────────────────────────

def test_public_bots_pagination(tmp_path):
    app = _new_app(tmp_path)
    _seed_bots(app, count=20)
    c = TestClient(app)

    p1 = c.get("/api/bots/public?page=1&per_page=8").json()
    assert p1["total"] >= 20
    assert len(p1["bots"]) == 8
    assert p1["page"] == 1 and p1["per_page"] == 8
    # owner_name 富化仍作用于分页后的行
    assert all("owner_name" in b for b in p1["bots"])

    p3 = c.get("/api/bots/public?page=3&per_page=8").json()
    ids1 = {b["id"] for b in p1["bots"]}
    ids3 = {b["id"] for b in p3["bots"]}
    assert ids1.isdisjoint(ids3)

    # 旧契约
    old = c.get("/api/bots/public").json()
    assert "page" not in old
    assert len(old["bots"]) >= 20


# ── /api/users/{username}/bots ────────────────────────────────

def test_user_bots_pagination(tmp_path):
    app = _new_app(tmp_path)
    store = app.state.store
    owner = store.create_user("creator", "c@ex.com", hash_password("pw123456"))
    store.update_user(owner["id"], email_verified=1)
    for i in range(15):
        store.create_bot(owner["id"], f"cb{i}", binary_path="/tmp", format="elf", game_id="holdem")
    c = TestClient(app)

    p1 = c.get("/api/users/creator/bots?page=1&per_page=10").json()
    assert p1["total"] == 15
    assert len(p1["bots"]) == 10
    p2 = c.get("/api/users/creator/bots?page=2&per_page=10").json()
    assert len(p2["bots"]) == 5
    assert {b["id"] for b in p1["bots"]}.isdisjoint({b["id"] for b in p2["bots"]})

    old = c.get("/api/users/creator/bots").json()
    assert "page" not in old and len(old["bots"]) == 15


# ── /api/leaderboard ──────────────────────────────────────────

def _seed_ratings(app, count=12, game_id="holdem", prefix="usr"):
    """建 count 个有评分的 active bot。"""
    store = app.state.store
    _, bots = _seed_bots(app, count=count, game_id=game_id, prefix=prefix)
    for i, b in enumerate(bots):
        store.select_ranked_bot(int(b["owner_id"]), int(b["id"]), if_empty=True)
        store.ensure_rating(b["id"])
        store.upsert_rating(
            b["id"], rating=1500.0 + i, rd=200.0, vol=0.06,
            matches_played=10,
        )


def test_leaderboard_pagination(tmp_path):
    app = _new_app(tmp_path)
    _seed_ratings(app, count=12)
    c = TestClient(app)

    p1 = c.get("/api/leaderboard?game_id=holdem&page=1&per_page=5").json()
    assert p1["total"] == 12
    assert len(p1["leaderboard"]) == 5
    assert p1["page"] == 1 and p1["per_page"] == 5
    assert [row["rank"] for row in p1["leaderboard"]] == [1, 2, 3, 4, 5]
    # 数值评分投影仍作用于分页行
    assert "confidence_low" in p1["leaderboard"][0]
    assert "rank_total" in p1["leaderboard"][0]
    assert "rating_delta" in p1["leaderboard"][0]

    p2 = c.get("/api/leaderboard?game_id=holdem&page=2&per_page=5").json()
    assert {r["bot_id"] for r in p1["leaderboard"]}.isdisjoint(
        {r["bot_id"] for r in p2["leaderboard"]}
    )
    assert [row["rank"] for row in p2["leaderboard"]] == [6, 7, 8, 9, 10]

    # 旧契约：limit 生效
    old = c.get("/api/leaderboard?game_id=holdem&limit=200").json()
    assert "page" not in old
    assert len(old["leaderboard"]) == 12


def test_leaderboard_pagination_game_filter(tmp_path):
    app = _new_app(tmp_path)
    _seed_ratings(app, count=6, game_id="holdem", prefix="hld")
    _seed_ratings(app, count=4, game_id="gomoku", prefix="gmk")
    c = TestClient(app)

    holdem = c.get("/api/leaderboard?game_id=holdem&page=1&per_page=50").json()
    assert holdem["total"] == 6
    assert holdem["game_id"] == "holdem"
    assert all("game_id" not in r for r in holdem["leaderboard"])
    gomoku = c.get("/api/leaderboard?game_id=gomoku&page=1&per_page=50").json()
    assert gomoku["total"] == 4


# ── /api/comments ─────────────────────────────────────────────

def test_comments_pagination(tmp_path):
    app = _new_app(tmp_path)
    store = app.state.store
    u = store.create_user("cmt", "c@ex.com", hash_password("pw123456"))
    store.update_user(u["id"], email_verified=1)
    bot = store.create_bot(
        u["id"], "cmtbot", binary_path="/tmp/cmt", format="elf", game_id="holdem"
    )
    store.create_match("mX", bot["id"], bot["id"], game_id="holdem")
    for i in range(18):
        store.add_comment(u["id"], "match", "mX", f"comment {i}")

    c = TestClient(app)
    p1 = c.get("/api/comments?target_type=match&target_id=mX&page=1&per_page=10").json()
    assert p1["total"] == 18
    assert len(p1["comments"]) == 10
    assert p1["page"] == 1 and p1["per_page"] == 10
    # count 字段保留（旧契约里有）
    assert p1["count"] == 18

    p2 = c.get("/api/comments?target_type=match&target_id=mX&page=2&per_page=10").json()
    assert len(p2["comments"]) == 8
    assert {cmt["id"] for cmt in p1["comments"]}.isdisjoint(
        {cmt["id"] for cmt in p2["comments"]}
    )

    # 向后兼容：limit 生效，无 page
    old = c.get("/api/comments?target_type=match&target_id=mX&limit=500").json()
    assert "page" not in old
    assert len(old["comments"]) == 18


# ── /api/notifications ────────────────────────────────────────

def test_notifications_pagination(tmp_path):
    app = _new_app(tmp_path)
    store = app.state.store
    u = store.create_user("notif", "n@ex.com", hash_password("pw123456"))
    store.update_user(u["id"], email_verified=1)
    for i in range(14):
        store.add_notification(u["id"], title=f"n{i}")

    c = TestClient(app)
    h = _login(app, "notif")
    p1 = c.get("/api/notifications?page=1&per_page=5", headers=h).json()
    assert p1["total"] == 14
    assert len(p1["notifications"]) == 5
    assert p1["page"] == 1 and p1["per_page"] == 5
    # unread_count 保留
    assert p1["unread_count"] == 14

    p3 = c.get("/api/notifications?page=3&per_page=5", headers=h).json()
    assert len(p3["notifications"]) == 4  # 14 - 10
    assert {n["id"] for n in p1["notifications"]}.isdisjoint(
        {n["id"] for n in p3["notifications"]}
    )

    # 向后兼容：limit/offset 生效
    old = c.get("/api/notifications?limit=200&offset=0", headers=h).json()
    assert "page" not in old
    assert len(old["notifications"]) == 14


# ── /api/bots/{id}/matches ────────────────────────────────────

def test_bot_matches_pagination(tmp_path):
    app = _new_app(tmp_path)
    store = app.state.store
    owner = store.create_user("bmatch", "bm@ex.com", hash_password("pw123456"))
    store.update_user(owner["id"], email_verified=1)
    b1 = store.create_bot(owner["id"], "bma", binary_path="/tmp", format="elf", game_id="holdem")
    b2 = store.create_bot(owner["id"], "bmb", binary_path="/tmp", format="elf", game_id="holdem")
    for i in range(11):
        mid = f"bmh{i}"
        store.create_match(mid, bot_a_id=b1["id"], bot_b_id=b2["id"], owner_id=owner["id"], game_id="holdem")
        store.update_match(mid, status="completed", winner=0)

    c = TestClient(app)
    p1 = c.get(f"/api/bots/{b1['id']}/matches?page=1&per_page=5").json()
    assert p1["total"] == 11
    assert len(p1["matches"]) == 5
    assert p1["page"] == 1 and p1["per_page"] == 5

    p3 = c.get(f"/api/bots/{b1['id']}/matches?page=3&per_page=5").json()
    assert len(p3["matches"]) == 1  # 11 - 10
    assert {m["id"] for m in p1["matches"]}.isdisjoint({m["id"] for m in p3["matches"]})

    # 向后兼容：limit/offset 仍工作
    old = c.get(f"/api/bots/{b1['id']}/matches?limit=100&offset=0").json()
    assert "page" not in old
    assert len(old["matches"]) == 11


# ── /api/bots/{id}/opponents ──────────────────────────────────

def test_bot_opponents_pagination_is_complete_stable_and_public(tmp_path):
    app = _new_app(tmp_path, "opponents.db")
    store = app.state.store
    owner = store.create_user(
        "oppowner", "oppowner@ex.com", hash_password("pw123456")
    )
    store.update_user(owner["id"], email_verified=1)

    # 先建 3 个较小 id 的对手，再建目标和 20 个较大 id 的对手，确保目标
    # 同时出现在 pair_stats 的 a/b 两侧，分页不能破坏胜负视角。
    lower = [
        store.create_bot(
            owner["id"], f"lower{i}", binary_path="/tmp", format="elf",
            game_id="holdem",
        )
        for i in range(3)
    ]
    target = store.create_bot(
        owner["id"], "target", binary_path="/tmp", format="elf",
        game_id="holdem",
    )
    higher = [
        store.create_bot(
            owner["id"], f"higher{i:02d}", binary_path="/tmp", format="elf",
            game_id="holdem",
        )
        for i in range(20)
    ]
    opponents = lower + higher
    for index, opponent in enumerate(opponents):
        lo, hi = sorted((target["id"], opponent["id"]))
        if index == 0:
            # target 在 b 位，a 胜即 target 负。
            store.upsert_pair_stats(lo, hi, a_wins_delta=1)
        elif target["id"] == lo:
            store.upsert_pair_stats(lo, hi, a_wins_delta=1)
        else:
            # target 在 b 位，a 负即 target 胜。
            store.upsert_pair_stats(lo, hi, a_losses_delta=1)

    # 固定时间后，全部一场的行只靠规范化 pair id 稳定破同分；对于固定目标
    # 该顺序等价于 opponent_id 升序。
    with store._tx() as conn:
        conn.execute(
            "UPDATE pair_stats SET last_played_at='2026-08-20T00:00:00' "
            "WHERE bot_a_id=? OR bot_b_id=?",
            (target["id"], target["id"]),
        )
        pair_count = int(conn.execute(
            "SELECT COUNT(*) FROM pair_stats "
            "WHERE bot_a_id=? OR bot_b_id=?",
            (target["id"], target["id"]),
        ).fetchone()[0])

    client = TestClient(app)  # 无认证：端点是公开读接口。
    page_1_response = client.get(
        f"/api/bots/{target['id']}/opponents?page=1"
    )
    assert page_1_response.status_code == 200
    page_1 = page_1_response.json()
    assert page_1.keys() == {"opponents", "page", "per_page", "total"}
    assert page_1["page"] == 1
    assert page_1["per_page"] == 20
    assert page_1["total"] == pair_count == len(opponents) == 23
    assert len(page_1["opponents"]) == 20

    page_2 = client.get(
        f"/api/bots/{target['id']}/opponents?page=2&per_page=20"
    ).json()
    assert page_2["total"] == 23
    assert len(page_2["opponents"]) == 3
    first_ids = [row["opponent_id"] for row in page_1["opponents"]]
    second_ids = [row["opponent_id"] for row in page_2["opponents"]]
    expected_ids = sorted(opponent["id"] for opponent in opponents)
    assert first_ids + second_ids == expected_ids
    assert set(first_ids).isdisjoint(second_ids)

    # target 在 b 位的两种结果都按 target 视角还原；target 在 a 位亦保持正向。
    rows_by_id = {
        row["opponent_id"]: row
        for row in page_1["opponents"] + page_2["opponents"]
    }
    assert rows_by_id[lower[0]["id"]]["losses"] == 1
    assert rows_by_id[lower[0]["id"]]["wins"] == 0
    assert rows_by_id[lower[1]["id"]]["wins"] == 1
    assert rows_by_id[higher[0]["id"]]["wins"] == 1

    # 参数钳制与超尾页都保留权威 total；超尾页不重复最后一页。
    low_clamp = client.get(
        f"/api/bots/{target['id']}/opponents?page=0&per_page=0"
    ).json()
    assert low_clamp["page"] == 1
    assert low_clamp["per_page"] == 1
    assert low_clamp["total"] == 23
    assert len(low_clamp["opponents"]) == 1

    high_clamp = client.get(
        f"/api/bots/{target['id']}/opponents?page=1&per_page=999"
    ).json()
    assert high_clamp["per_page"] == 200
    assert high_clamp["total"] == 23
    assert len(high_clamp["opponents"]) == 23

    beyond = client.get(
        f"/api/bots/{target['id']}/opponents?page=99&per_page=20"
    ).json()
    assert beyond["page"] == 99
    assert beyond["total"] == 23
    assert beyond["opponents"] == []

    # 不带 page 时保持旧响应形状和 limit 行为。
    legacy = client.get(
        f"/api/bots/{target['id']}/opponents?limit=5"
    ).json()
    assert legacy.keys() == {"opponents"}
    assert len(legacy["opponents"]) == 5


def test_bot_opponents_pagination_returns_404_for_unknown_bot(tmp_path):
    client = TestClient(_new_app(tmp_path, "opponents-404.db"))
    response = client.get("/api/bots/999999/opponents?page=1&per_page=20")
    assert response.status_code == 404
    assert response.json()["detail"] == "bot 不存在"


# ── /api/contests/{id}（entries 子分页）──────────────────────

def test_contest_detail_entries_pagination(tmp_path):
    app = _new_app(tmp_path)
    store = app.state.store
    org = store.create_user("org", "o@ex.com", hash_password("pw123456"), role="organizer")
    store.update_user(org["id"], email_verified=1)
    cid = store.create_contest("BigCup", organizer_id=org["id"], game_id="holdem", status="open")["id"]
    # 8 名参赛者各一 bot，全部报名
    for i in range(8):
        u = store.create_user(f"ply{i}", f"p{i}@ex.com", hash_password("pw123456"))
        store.update_user(u["id"], email_verified=1)
        b = store.create_bot(u["id"], f"pb{i}", binary_path="/tmp", format="elf", game_id="holdem")
        store.add_entry(cid, u["id"], b["id"])

    c = TestClient(app)
    # 不分页：旧契约（无 entries_page/entries_total）
    old = c.get(f"/api/contests/{cid}").json()
    assert "entries_page" not in old
    assert len(old["entries"]) == 8

    # 分页 entries
    p1 = c.get(f"/api/contests/{cid}?entries_page=1&entries_per_page=3").json()
    assert p1["entries_total"] == 8
    assert p1["entries_page"] == 1 and p1["entries_per_page"] == 3
    assert len(p1["entries"]) == 3
    # pairings/standings 仍在
    assert "pairings" in p1 and "standings" in p1

    p3 = c.get(f"/api/contests/{cid}?entries_page=3&entries_per_page=3").json()
    assert len(p3["entries"]) == 2  # 8 - 6
    e1 = {e["id"] for e in p1["entries"]}
    e3 = {e["id"] for e in p3["entries"]}
    assert e1.isdisjoint(e3)


# ── /api/admin/users ──────────────────────────────────────────

def test_admin_users_pagination(tmp_path):
    app = _new_app(tmp_path)
    store = app.state.store
    # 已有 admin + 种子用户；再造一批
    for i in range(20):
        store.create_user(f"au{i}", f"au{i}@ex.com", hash_password("pw123456"))
    _, h = _admin_and_token(app)
    c = TestClient(app)

    p1 = c.get("/api/admin/users?page=1&per_page=10", headers=h).json()
    assert p1["total"] >= 20
    assert len(p1["users"]) == 10
    assert p1["page"] == 1 and p1["per_page"] == 10

    p2 = c.get("/api/admin/users?page=2&per_page=10", headers=h).json()
    assert {u["id"] for u in p1["users"]}.isdisjoint({u["id"] for u in p2["users"]})

    # 搜索/实名筛选必须在数据库分页前执行；不能只筛当前页而漏掉后页用户。
    target = store.get_user_by_username("au19")
    store.update_user(
        target["id"], real_name="QA", phone="13800138000",
        school="Test", student_id="S19",
    )
    searched = c.get(
        "/api/admin/users?page=1&per_page=10&q=au19&real_name=true", headers=h,
    ).json()
    assert searched["total"] == 1
    assert [u["username"] for u in searched["users"]] == ["au19"]
    missing = c.get(
        "/api/admin/users?page=1&per_page=10&q=au19&real_name=false", headers=h,
    ).json()
    assert missing["total"] == 0 and missing["users"] == []

    # 旧契约
    old = c.get("/api/admin/users", headers=h).json()
    assert "page" not in old
    assert len(old["users"]) >= 20


# ── /api/admin/bots（已支持，回归校验）───────────────────────

def test_admin_bots_pagination_regression(tmp_path):
    app = _new_app(tmp_path)
    _seed_bots(app, count=20)
    _, h = _admin_and_token(app)
    c = TestClient(app)

    p1 = c.get("/api/admin/bots?page=1&per_page=5", headers=h).json()
    assert len(p1["bots"]) == 5
    assert p1["total"] >= 20
    assert p1["page"] == 1 and p1["per_page"] == 5
    assert all(b.get("owner_name") for b in p1["bots"])

    old = c.get("/api/admin/bots", headers=h).json()
    assert "page" not in old


# ── /api/admin/contests（已支持，回归校验）───────────────────

def test_admin_contests_pagination_regression(tmp_path):
    app = _new_app(tmp_path)
    store = app.state.store
    admin, h = _admin_and_token(app, name="acadmin")
    for i in range(15):
        store.create_contest(f"Cup{i}", organizer_id=admin["id"], game_id="holdem")
    c = TestClient(app)

    p1 = c.get("/api/admin/contests?page=1&per_page=5", headers=h).json()
    assert len(p1["contests"]) == 5
    assert p1["total"] >= 15
    assert p1["page"] == 1 and p1["per_page"] == 5


# ── per_page 上限钳到 200 ────────────────────────────────────

def test_per_page_clamp_to_200(tmp_path):
    app = _new_app(tmp_path)
    _seed_bots(app, count=5)
    c = TestClient(app)
    # per_page=10000 应被钳到 200（不报错；实际行数=数据量）
    r = c.get("/api/bots/public?page=1&per_page=10000").json()
    assert r["per_page"] == 200
    assert len(r["bots"]) == 5  # 数据不足 200
