"""赛事可见性测试（审计 P1-E）。

GET /api/contests 始终应对访客/普通用户排除 draft/cancelled，不能靠显式
status 绕过。组织者仅可见自己主办的隐藏赛事，admin 可见全部。
"""
from __future__ import annotations

import os
import sqlite3

import pytest
from fastapi.testclient import TestClient

from bzplat.backend.crypto import hash_password
from bzplat.backend.store import Store
from bzplat.backend.store.db import _paginate


def _app(tmp_path):
    from bzplat.backend.main import create_app
    os.environ["BZ_BOT_LOCAL"] = "1"
    os.environ["BZ_SKIP_CAPTCHA"] = "1"
    return create_app(db_path=str(tmp_path / "cv.db"))


def _setup(app):
    """建两个组织者 + admin + 普通用户及公开/隐藏赛事。"""
    store = app.state.store
    org = store.create_user("org", "org@e.com", hash_password("pw123456"))
    store.update_user(org["id"], role="organizer", email_verified=1)
    adm = store.create_user("adm", "a@e.com", hash_password("pw123456"))
    store.update_user(adm["id"], role="admin", email_verified=1)
    other_org = store.create_user(
        "otherorg", "otherorg@e.com", hash_password("pw123456")
    )
    store.update_user(other_org["id"], role="organizer", email_verified=1)
    usr = store.create_user("usr", "u@e.com", hash_password("pw123456"))
    store.update_user(usr["id"], email_verified=1)
    # 3 个赛事：draft（默认）/ open / cancelled
    c_draft = store.create_contest("草稿赛", org["id"], game_id="holdem", status="draft")
    c_open = store.create_contest("公开赛", org["id"], game_id="holdem", status="open")
    c_cancel = store.create_contest("已取消赛", org["id"], game_id="holdem", status="cancelled")
    c_other_draft = store.create_contest(
        "其他组织者草稿", other_org["id"], game_id="holdem", status="draft"
    )
    return store, {"org": org, "other_org": other_org, "adm": adm, "usr": usr,
                   "c_draft": c_draft, "c_open": c_open, "c_cancel": c_cancel,
                   "c_other_draft": c_other_draft}


def _tok(app, username):
    _, t = app.state.auth.authenticate(username, "pw123456")
    return {"Authorization": f"Bearer {t}"}


def test_visitor_does_not_see_draft_or_cancelled(tmp_path):
    """访客（未登录）GET /api/contests 不应见 draft/cancelled 赛事。"""
    app = _app(tmp_path)
    store, ctx = _setup(app)
    client = TestClient(app)
    r = client.get("/api/contests")
    assert r.status_code == 200
    titles = [c["title"] for c in r.json()["contests"]]
    assert "公开赛" in titles
    assert "草稿赛" not in titles, "访客不应见 draft 赛事"
    assert "已取消赛" not in titles, "访客不应见 cancelled 赛事"

    # contest_detail 也不能直读 draft（审计 P1-E：list 已守，detail 漏守）
    r2 = client.get(f"/api/contests/{ctx['c_draft']['id']}")
    assert r2.status_code == 404, "访客不应直读 draft 赛事 detail（id 枚举泄漏）"
    r3 = client.get(f"/api/contests/{ctx['c_cancel']['id']}")
    assert r3.status_code == 404, "访客不应直读 cancelled 赛事 detail"
    r3b = client.get(f"/api/contests/{ctx['c_draft']['id']}/bracket")
    assert r3b.status_code == 404, "访客不应绕过 detail 直读 draft bracket"
    # organizer 可读自己的 draft
    r4 = client.get(f"/api/contests/{ctx['c_draft']['id']}", headers=_tok(app, "org"))
    assert r4.status_code == 200
    assert r4.json()["contest"]["status"] == "draft"


def test_normal_user_does_not_see_draft(tmp_path):
    """普通用户登录后仍不见 draft/cancelled。"""
    app = _app(tmp_path)
    _, ctx = _setup(app)
    client = TestClient(app)
    r = client.get("/api/contests", headers=_tok(app, "usr"))
    assert r.status_code == 200
    titles = [c["title"] for c in r.json()["contests"]]
    assert "草稿赛" not in titles
    assert "已取消赛" not in titles
    assert "公开赛" in titles

    # 登录普通用户显式 status 与直达子资源同样不可绕过。
    assert client.get(
        "/api/contests?status=draft", headers=_tok(app, "usr")
    ).json()["contests"] == []
    assert client.get(
        f"/api/contests/{ctx['c_draft']['id']}/bracket",
        headers=_tok(app, "usr"),
    ).status_code == 404


def test_hidden_detail_and_bracket_only_owner_or_admin(tmp_path):
    """其他 organizer 也不能读取不属于自己的隐藏赛事。"""
    app = _app(tmp_path)
    _, ctx = _setup(app)
    client = TestClient(app)
    did = ctx["c_draft"]["id"]
    for username in ("usr", "otherorg"):
        headers = _tok(app, username)
        assert client.get(f"/api/contests/{did}", headers=headers).status_code == 404
        assert client.get(
            f"/api/contests/{did}/bracket", headers=headers
        ).status_code == 404
    for username in ("org", "adm"):
        headers = _tok(app, username)
        assert client.get(f"/api/contests/{did}", headers=headers).status_code == 200
        assert client.get(
            f"/api/contests/{did}/bracket", headers=headers
        ).status_code == 200


def _seed_contest_entries(store, contest_id: int) -> list[dict]:
    entries = []
    for index in range(3):
        user = store.create_user(
            f"entry-user-{index}",
            f"entry-user-{index}@e.com",
            hash_password("pw123456"),
        )
        bot = store.create_bot(
            user["id"],
            f"entry-bot-{index}",
            display_name=f"分页 Bot {index}",
            game_id="holdem",
        )
        entries.append(store.add_entry(contest_id, user["id"], bot["id"]))
    return entries


def test_light_contest_entries_are_paged_allowlisted_and_pairing_free(
    tmp_path, monkeypatch
):
    """Roster pagination must not rebuild the full O(n²) contest projection."""
    app = _app(tmp_path)
    store, ctx = _setup(app)
    seeded = _seed_contest_entries(store, ctx["c_open"]["id"])

    def fail_full_projection(*_args, **_kwargs):
        raise AssertionError("light entries endpoint touched full pairing projection")

    monkeypatch.setattr(store, "contest_projection_snapshot", fail_full_projection)
    monkeypatch.setattr(store, "contest_bracket", fail_full_projection)
    monkeypatch.setattr(store, "_contest_bracket_tx", fail_full_projection)
    monkeypatch.setattr(store, "get_contest", fail_full_projection)
    monkeypatch.setattr(store, "contest_entries_named", fail_full_projection)
    client = TestClient(app)
    path = f"/api/contests/{ctx['c_open']['id']}/entries?page=2&per_page=1"
    expected_fields = {
        "id", "user_id", "bot_id", "registered_at", "group_id", "seed",
        "eliminated", "bot_name", "bot_display", "owner_name", "owner_display",
    }

    # The same visible-contest ACL applies to a guest, signed-in user, organizer,
    # and admin. No role gets an accidental full-detail/pairing read.
    callers = [None, "usr", "org", "adm"]
    for username in callers:
        response = client.get(path, headers=_tok(app, username) if username else {})
        assert response.status_code == 200
        payload = response.json()
        assert set(payload) == {"entries", "page", "per_page", "total"}
        assert (payload["page"], payload["per_page"], payload["total"]) == (2, 1, 3)
        assert len(payload["entries"]) == 1
        assert set(payload["entries"][0]) == expected_fields
        assert payload["entries"][0]["id"] == seeded[1]["id"]


def test_light_contest_entries_large_same_key_pages_are_stable_and_bounded(tmp_path):
    app = _app(tmp_path)
    store, ctx = _setup(app)
    contest_id = ctx["c_open"]["id"]
    registered_at = "2026-09-02T00:00:00+00:00"
    entry_count = 6_000
    with store._tx() as connection:
        connection.executemany(
            "INSERT INTO users(username,email,password_hash,created_at) "
            "VALUES(?,?,?,?)",
            (
                (
                    f"page-scale-user-{index}",
                    f"page-scale-user-{index}@example.test",
                    "hash",
                    registered_at,
                )
                for index in range(entry_count)
            ),
        )
        connection.execute(
            "INSERT INTO contest_entries(contest_id,user_id,registered_at,seed) "
            "SELECT ?,id,?,7 FROM users WHERE username LIKE 'page-scale-user-%'",
            (contest_id, registered_at),
        )
        expected_ids = [
            int(row[0])
            for row in connection.execute(
                "SELECT id FROM contest_entries WHERE contest_id=? ORDER BY id",
                (contest_id,),
            )
        ]

    traced_sql: list[str] = []
    progress_callbacks = 0

    def count_progress() -> int:
        nonlocal progress_callbacks
        progress_callbacks += 1
        return 0

    client = TestClient(app)
    store._conn.set_trace_callback(traced_sql.append)
    store._conn.set_progress_handler(count_progress, 100)
    try:
        first = client.get(
            f"/api/contests/{contest_id}/entries?page=1&per_page=100"
        )
    finally:
        store._conn.set_progress_handler(None, 0)
        store._conn.set_trace_callback(None)
    second = client.get(
        f"/api/contests/{contest_id}/entries?page=2&per_page=100"
    )
    repeated = client.get(
        f"/api/contests/{contest_id}/entries?page=1&per_page=100"
    )

    assert first.status_code == second.status_code == repeated.status_code == 200
    first_payload = first.json()
    second_payload = second.json()
    repeated_payload = repeated.json()
    assert first_payload["total"] == second_payload["total"] == entry_count
    assert [row["id"] for row in first_payload["entries"]] == expected_ids[:100]
    assert [row["id"] for row in second_payload["entries"]] == expected_ids[100:200]
    assert repeated_payload["entries"] == first_payload["entries"]

    normalized_sql = [" ".join(statement.split()) for statement in traced_sql]
    page_queries = [
        statement
        for statement in normalized_sql
        if statement.startswith("SELECT e.id,e.user_id,e.bot_id")
    ]
    count_queries = [
        statement
        for statement in normalized_sql
        if statement.upper().startswith("SELECT COUNT(*)")
    ]
    assert len(page_queries) == 1
    assert "ORDER BY e.seed,e.registered_at,e.id LIMIT 100 OFFSET 0" in (
        page_queries[0]
    )
    assert len(count_queries) == 1
    assert "FROM contest_entries WHERE contest_id=" in count_queries[0]
    assert " JOIN " not in count_queries[0].upper()
    # Counting the indexed contest range plus fetching the first 100 rows stays
    # proportional to the roster, without a second full joined scan and sort.
    assert progress_callbacks < 400


def test_light_contest_entries_preserve_hidden_acl(tmp_path, monkeypatch):
    app = _app(tmp_path)
    store, ctx = _setup(app)
    monkeypatch.setattr(
        store,
        "contest_projection_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("light entries endpoint touched full projection")
        ),
    )
    client = TestClient(app)
    path = f"/api/contests/{ctx['c_draft']['id']}/entries?page=1&per_page=20"

    for username in (None, "usr", "otherorg"):
        response = client.get(path, headers=_tok(app, username) if username else {})
        assert response.status_code == 404
    for username in ("org", "adm"):
        response = client.get(path, headers=_tok(app, username))
        assert response.status_code == 200
        assert response.json() == {
            "entries": [], "page": 1, "per_page": 20, "total": 0,
        }


def test_light_contest_entries_keep_real_name_identity_private(tmp_path):
    app = _app(tmp_path)
    store, ctx = _setup(app)
    entrant = store.create_user(
        "identity-entry",
        "identity-entry@e.com",
        hash_password("pw123456"),
        real_name="报名姓名",
        phone="13800138000",
        school="报名学校",
        student_id="ENTRY001",
    )
    bot = store.create_bot(entrant["id"], "identity-entry-bot", game_id="holdem")
    contest = store.create_contest(
        "实名分页赛事",
        ctx["org"]["id"],
        game_id="holdem",
        status="open",
        require_real_name=1,
    )
    store.add_entry(contest["id"], entrant["id"], bot["id"])
    client = TestClient(app)
    path = f"/api/contests/{contest['id']}/entries?page=1&per_page=20"

    ordinary = client.get(path, headers=_tok(app, "usr"))
    assert ordinary.status_code == 200
    assert not {
        "real_name", "phone", "school", "student_id", "identity_source",
        "identity_captured_at",
    }.intersection(ordinary.json()["entries"][0])
    assert ordinary.headers["Cache-Control"] == "private, no-store, max-age=0"
    assert ordinary.headers["Vary"] == "Authorization, Cookie"

    for username in ("org", "adm"):
        private = client.get(path, headers=_tok(app, username))
        assert private.status_code == 200
        entry = private.json()["entries"][0]
        assert entry["real_name"] == "报名姓名"
        assert entry["phone"] == "13800138000"
        assert entry["school"] == "报名学校"
        assert entry["student_id"] == "ENTRY001"
        assert entry["identity_source"] == "registration_profile"
        assert private.headers["Cache-Control"] == "private, no-store, max-age=0"
        assert private.headers["Vary"] == "Authorization, Cookie"


def test_light_contest_entries_acl_and_page_share_one_sqlite_snapshot(tmp_path):
    app = _app(tmp_path)
    store, ctx = _setup(app)
    entrant = store.create_user(
        "snapshot-entry",
        "snapshot-entry@example.test",
        hash_password("pw123456"),
        real_name="PRE-SNAPSHOT-NAME",
        phone="13800138000",
        school="Snapshot School",
        student_id="SNAPSHOT-1",
    )
    bot = store.create_bot(entrant["id"], "snapshot-entry-bot", game_id="holdem")
    contest = store.create_contest(
        "名册快照并发赛事",
        ctx["org"]["id"],
        game_id="holdem",
        status="open",
        require_real_name=1,
    )
    first_entry = store.add_entry(contest["id"], entrant["id"], bot["id"])
    registered_at = "2026-09-02T00:00:00"
    filler_count = 6_000
    with store._tx() as connection:
        connection.executemany(
            "INSERT INTO users(username,email,password_hash,created_at) "
            "VALUES(?,?,?,?)",
            (
                (
                    f"snapshot-filler-{index}",
                    f"snapshot-filler-{index}@example.test",
                    "hash",
                    registered_at,
                )
                for index in range(filler_count)
            ),
        )
        connection.execute(
            "INSERT INTO contest_entries(contest_id,user_id,registered_at,seed) "
            "SELECT ?,id,?,1 FROM users WHERE username LIKE 'snapshot-filler-%'",
            (contest["id"], registered_at),
        )
    writer = type(store)(store.path)
    store._conn.execute("PRAGMA journal_mode=WAL")
    writer._conn.execute("PRAGMA journal_mode=WAL")
    inside_count = False
    injected = False

    def trace(statement: str) -> None:
        nonlocal inside_count
        normalized = " ".join(statement.split())
        if normalized.startswith(
            "SELECT COUNT(*) FROM contest_entries WHERE contest_id="
        ):
            inside_count = True

    def inject_revocation() -> int:
        nonlocal injected
        if inside_count and not injected:
            injected = True
            with writer._tx() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "UPDATE contests SET status='draft',organizer_id=? WHERE id=?",
                    (ctx["other_org"]["id"], contest["id"]),
                )
                connection.execute(
                    "UPDATE contest_entries SET real_name_snapshot=? WHERE id=?",
                    ("POST-REVOCATION-SECRET", first_entry["id"]),
                )
        return 0

    token = _tok(app, "org")
    store._conn.set_trace_callback(trace)
    store._conn.set_progress_handler(inject_revocation, 100)
    try:
        response = TestClient(app).get(
            f"/api/contests/{contest['id']}/entries?page=1&per_page=20",
            headers=token,
        )
    finally:
        store._conn.set_progress_handler(None, 0)
        store._conn.set_trace_callback(None)
        writer.close()
    assert injected is True
    assert response.status_code == 200
    assert response.json()["total"] == filler_count + 1
    serialized = response.text
    assert "PRE-SNAPSHOT-NAME" in serialized
    assert "POST-REVOCATION-SECRET" not in serialized
    revoked = TestClient(app).get(
        f"/api/contests/{contest['id']}/entries?page=1&per_page=20",
        headers=token,
    )
    assert revoked.status_code == 404
    assert revoked.headers["Cache-Control"] == "private, no-store, max-age=0"
    assert revoked.headers["Vary"] == "Authorization, Cookie"


@pytest.mark.parametrize(
    "query",
    [
        "page=0",
        "page=10001",
        "page=bad",
        "per_page=0",
        "per_page=201",
        "per_page=bad",
    ],
)
def test_light_contest_entries_reject_invalid_pagination(tmp_path, query):
    app = _app(tmp_path)
    _, ctx = _setup(app)
    response = TestClient(app).get(
        f"/api/contests/{ctx['c_open']['id']}/entries?{query}"
    )
    assert response.status_code == 422


def test_organizer_sees_only_own_hidden_while_admin_sees_all(tmp_path):
    """组织者只额外看到自己的 hidden；admin 才能看到所有 hidden。"""
    app = _app(tmp_path)
    _setup(app)
    client = TestClient(app)
    org_titles = [
        c["title"]
        for c in client.get("/api/contests", headers=_tok(app, "org")).json()["contests"]
    ]
    assert "草稿赛" in org_titles
    assert "已取消赛" in org_titles
    assert "公开赛" in org_titles
    assert "其他组织者草稿" not in org_titles

    admin_titles = [
        c["title"]
        for c in client.get("/api/contests", headers=_tok(app, "adm")).json()["contests"]
    ]
    assert {"草稿赛", "已取消赛", "公开赛", "其他组织者草稿"} <= set(admin_titles)

    # ACL 必须在 SQL 分页/COUNT 之前生效，不能先取一页再在 Python 裁剪。
    anon_page = client.get("/api/contests?page=1&per_page=1").json()
    org_page = client.get(
        "/api/contests?page=1&per_page=1", headers=_tok(app, "org")
    ).json()
    admin_page = client.get(
        "/api/contests?page=1&per_page=1", headers=_tok(app, "adm")
    ).json()
    assert anon_page["total"] == 1
    assert org_page["total"] == 3
    assert admin_page["total"] == 4


def test_explicit_hidden_status_cannot_bypass_visibility(tmp_path):
    """显式 status 仍受隐藏状态 ACL；owner/admin 仅看各自授权集合。"""
    app = _app(tmp_path)
    _setup(app)
    client = TestClient(app)
    # 访客显式查 draft 也只能得到空集。
    r = client.get("/api/contests?status=draft")
    assert r.status_code == 200
    assert r.json()["contests"] == []

    owner_titles = [
        c["title"]
        for c in client.get(
            "/api/contests?status=draft", headers=_tok(app, "org")
        ).json()["contests"]
    ]
    assert owner_titles == ["草稿赛"]

    admin_titles = {
        c["title"]
        for c in client.get(
            "/api/contests?status=draft", headers=_tok(app, "adm")
        ).json()["contests"]
    }
    assert admin_titles == {"草稿赛", "其他组织者草稿"}


def _finished_gomoku_source(store, organizer_id: int, title: str) -> dict:
    contest = store.create_contest(
        title,
        organizer_id,
        game_id="gomoku",
        template_id="gomoku_drr",
        status="finished",
    )
    store.update_contest(contest["id"], official_results_ready=1)
    return store.get_contest(contest["id"])


def test_source_candidates_are_bounded_authorized_and_minimal(tmp_path):
    app = _app(tmp_path)
    store, ctx = _setup(app)
    client = TestClient(app)

    candidates = [
        _finished_gomoku_source(store, ctx["org"]["id"], f"合格模拟赛 {index:02d}")
        for index in range(4)
    ]
    not_ready = store.create_contest(
        "拒绝未就绪",
        ctx["org"]["id"],
        game_id="gomoku",
        status="finished",
    )
    wrong_game = store.create_contest(
        "拒绝异游戏",
        ctx["org"]["id"],
        game_id="pencil",
        status="finished",
    )
    store.update_contest(wrong_game["id"], official_results_ready=1)
    showcase = _finished_gomoku_source(
        store, ctx["org"]["id"], "拒绝演示快照"
    )
    store.freeze_contest_showcase(showcase["id"], "source-candidate-test")
    malformed_ready = store.create_contest(
        "拒绝损坏标记",
        ctx["org"]["id"],
        game_id="gomoku",
        status="finished",
    )
    with store._tx() as conn:
        conn.execute(
            "UPDATE contests SET official_results_ready='broken' WHERE id=?",
            (malformed_ready["id"],),
        )

    path = "/api/contests/source-candidates?game_id=gomoku&limit=2"
    assert client.get(path).status_code == 401
    assert client.get(path, headers=_tok(app, "usr")).status_code == 403

    for username in ("org", "adm"):
        response = client.get(path, headers=_tok(app, username))
        assert response.status_code == 200
        payload = response.json()
        assert set(payload) == {"candidates", "has_more"}
        assert payload["has_more"] is True
        assert len(payload["candidates"]) == 2
        assert all(set(row) == {"id", "title"} for row in payload["candidates"])
        returned_ids = {row["id"] for row in payload["candidates"]}
        assert returned_ids <= {contest["id"] for contest in candidates}
        assert not returned_ids.intersection(
            {not_ready["id"], wrong_game["id"], showcase["id"], malformed_ready["id"]}
        )
        assert all(
            not {"organizer_id", "source_contest_id", "description"}.intersection(row)
            for row in payload["candidates"]
        )


def test_source_candidates_support_exact_id_trim_and_literal_like_search(tmp_path):
    app = _app(tmp_path)
    store, ctx = _setup(app)
    client = TestClient(app)
    literal = _finished_gomoku_source(
        store, ctx["org"]["id"], r"百分比 100%_完成\\复盘"
    )
    other = _finished_gomoku_source(
        store, ctx["org"]["id"], "百分比 100XY完成复盘"
    )
    headers = _tok(app, "org")

    exact = client.get(
        "/api/contests/source-candidates",
        params={"game_id": "gomoku", "query": f"  {literal['id']}  "},
        headers=headers,
    )
    assert exact.status_code == 200
    assert exact.json() == {
        "candidates": [{"id": literal["id"], "title": literal["title"]}],
        "has_more": False,
    }

    escaped = client.get(
        "/api/contests/source-candidates",
        params={"game_id": "gomoku", "query": r"100%_完成\\"},
        headers=headers,
    )
    assert escaped.status_code == 200
    assert escaped.json() == {
        "candidates": [{"id": literal["id"], "title": literal["title"]}],
        "has_more": False,
    }
    assert other["id"] != literal["id"]

    short = _finished_gomoku_source(store, ctx["org"]["id"], "中文决赛 ABC 🚀")
    for query in ("文", "决赛", "文决赛", "abc", "🚀"):
        response = client.get(
            "/api/contests/source-candidates",
            params={"game_id": "gomoku", "query": query},
            headers=headers,
        )
        assert response.status_code == 200
        assert response.json() == {
            "candidates": [{"id": short["id"], "title": short["title"]}],
            "has_more": False,
        }


def test_source_candidate_default_and_title_queries_use_canonical_indexes(tmp_path):
    app = _app(tmp_path)
    store, ctx = _setup(app)
    target = _finished_gomoku_source(
        store, ctx["org"]["id"], "唯一锚点 source-needle"
    )
    created_at = "2026-09-02T00:00:00"
    decoys = 10_000
    with store._tx() as connection:
        connection.executemany(
            "INSERT INTO contests(title,description,organizer_id,status,created_at,"
            "game_id,official_results_ready) VALUES(?,'',?,'open',?,'gomoku',0)",
            (
                (f"普通候选 {index:05d}", ctx["other_org"]["id"], created_at)
                for index in range(decoys)
            ),
        )

    statements: list[str] = []
    callbacks = 0

    def progress() -> int:
        nonlocal callbacks
        callbacks += 1
        return 0

    store._conn.set_trace_callback(statements.append)
    store._conn.set_progress_handler(progress, 100)
    try:
        default = store.list_contest_source_candidates(game_id="gomoku", limit=5)
        title = store.list_contest_source_candidates(
            game_id="gomoku", query="source-needle", limit=5
        )
    finally:
        store._conn.set_progress_handler(None, 0)
        store._conn.set_trace_callback(None)
    assert default["items"] == [{"id": target["id"], "title": target["title"]}]
    assert title["items"] == default["items"]
    assert callbacks < 50

    selects = [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith("SELECT")
    ]
    plans = [
        " ".join(str(row[3]) for row in store._conn.execute("EXPLAIN QUERY PLAN " + sql))
        for sql in selects
    ]
    assert any("idx_contests_source_protected" in plan for plan in plans)
    assert any(
        "SEARCH grams USING COVERING INDEX idx_contests_source_protected" in plan
        for plan in plans
    )
    assert all("TEMP B-TREE" not in plan for plan in plans)
    assert all("SCAN contests" not in plan for plan in plans)

    # A common one-character term still follows the scope-ordered index and
    # stops at limit+1; it must not collect and temp-sort the whole hit set.
    with store._tx() as connection:
        connection.executemany(
            "INSERT INTO contests(title,description,organizer_id,status,created_at,"
            "game_id,official_results_ready) VALUES(?,'',?,'finished',?,'gomoku',1)",
            (
                (f"常见赛词 {index:05d}", ctx["other_org"]["id"], created_at)
                for index in range(2_000)
            ),
        )
        connection.executemany(
            "INSERT INTO contests(title,description,organizer_id,status,created_at,"
            "game_id,official_results_ready) VALUES(?,'',?,'draft',?,'pencil',0)",
            (
                (f"外部草稿 {index:05d}", ctx["other_org"]["id"], created_at)
                for index in range(5_000)
            ),
        )
    callbacks = 0
    store._conn.set_progress_handler(progress, 100)
    try:
        common = store.list_contest_source_candidates(
            game_id="gomoku", query="赛", limit=5
        )
        absent_long = store.list_contest_source_candidates(
            game_id="gomoku", query="常见赛词-绝对不存在", limit=5
        )
        hidden = store.list_contest_source_candidates(
            game_id="pencil",
            query="草",
            limit=5,
            source_kind="navigation",
            hidden_owner_id=ctx["org"]["id"],
        )
    finally:
        store._conn.set_progress_handler(None, 0)
    assert len(common["items"]) == 5
    assert common["has_more"] is True
    assert absent_long == {"items": [], "has_more": False}
    assert hidden == {"items": [], "has_more": False}
    assert callbacks < 100

    reverse_plan = " ".join(
        str(row[3])
        for row in store._conn.execute(
            "EXPLAIN QUERY PLAN DELETE FROM contest_source_search_grams "
            "WHERE contest_id=?",
            (target["id"],),
        )
    )
    assert "SEARCH contest_source_search_grams USING PRIMARY KEY (contest_id=?)" in reverse_plan
    callbacks = 0
    store._conn.set_progress_handler(progress, 100)
    try:
        store.update_contest(target["id"], title="唯一锚点 source-needle updated")
    finally:
        store._conn.set_progress_handler(None, 0)
    assert callbacks < 250

    # The old `%substring%` shape still walks the whole eligible contest range;
    # a hard progress cap proves the new title path did not merely add LIMIT to
    # the response while retaining that scan.
    interrupted = 0

    def stop_legacy_scan() -> int:
        nonlocal interrupted
        interrupted += 1
        return int(interrupted > 50)

    store._conn.set_progress_handler(stop_legacy_scan, 100)
    try:
        with pytest.raises(sqlite3.OperationalError, match="interrupted"):
            store._conn.execute(
                "SELECT id FROM contests WHERE game_id='gomoku' "
                "AND title LIKE '%does-not-exist%' ORDER BY created_at DESC,id DESC "
                "LIMIT 6"
            ).fetchall()
    finally:
        store._conn.set_progress_handler(None, 0)


def test_pencil_navigation_candidates_need_no_results_and_preserve_hidden_acl(tmp_path):
    app = _app(tmp_path)
    store, ctx = _setup(app)
    client = TestClient(app)
    owner_draft = store.create_contest(
        "点格棋自有草稿预赛",
        ctx["org"]["id"],
        game_id="pencil",
        template_id="pencil_drr",
        status="draft",
    )
    public_open = store.create_contest(
        "点格棋公开决赛",
        ctx["other_org"]["id"],
        game_id="pencil",
        template_id="pencil_group_drr",
        status="open",
    )
    foreign_draft = store.create_contest(
        "点格棋他人草稿",
        ctx["other_org"]["id"],
        game_id="pencil",
        template_id="pencil_drr",
        status="draft",
    )

    organizer_payload = client.get(
        "/api/contests/source-candidates",
        params={"game_id": "pencil"},
        headers=_tok(app, "org"),
    ).json()
    organizer_ids = {row["id"] for row in organizer_payload["candidates"]}
    assert owner_draft["id"] in organizer_ids
    assert public_open["id"] in organizer_ids
    assert foreign_draft["id"] not in organizer_ids
    assert organizer_payload["has_more"] is False

    admin_payload = client.get(
        "/api/contests/source-candidates",
        params={"game_id": "pencil", "query": str(foreign_draft["id"])},
        headers=_tok(app, "adm"),
    ).json()
    assert admin_payload == {
        "candidates": [
            {"id": foreign_draft["id"], "title": foreign_draft["title"]}
        ],
        "has_more": False,
    }


def test_organizer_navigation_candidate_branches_share_one_sqlite_snapshot(tmp_path):
    app = _app(tmp_path)
    store, ctx = _setup(app)
    contest = store.create_contest(
        "跨分支快照赛事",
        ctx["org"]["id"],
        game_id="pencil",
        status="open",
    )
    writer = type(store)(store.path)
    store._conn.execute("PRAGMA journal_mode=WAL")
    writer._conn.execute("PRAGMA journal_mode=WAL")
    injected = False

    def move_public_contest_to_owner_branch(statement: str) -> None:
        nonlocal injected
        if (
            not injected
            and "INDEXED BY idx_contests_source_default_navigation_owner"
            in " ".join(statement.split())
        ):
            injected = True
            with writer._tx() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "UPDATE contests SET status='draft' WHERE id=?",
                    (contest["id"],),
                )

    store._conn.set_trace_callback(move_public_contest_to_owner_branch)
    try:
        payload = store.list_contest_source_candidates(
            game_id="pencil",
            limit=1,
            source_kind="navigation",
            hidden_owner_id=ctx["org"]["id"],
        )
    finally:
        store._conn.set_trace_callback(None)
        writer.close()

    assert injected is True
    assert payload == {
        "items": [{"id": contest["id"], "title": contest["title"]}],
        "has_more": False,
    }
    assert store.get_contest(contest["id"])["status"] == "draft"


@pytest.mark.parametrize(
    "params",
    [
        {"game_id": "holdem"},
        {"game_id": "unknown"},
        {"game_id": "GOMOKU"},
        {"game_id": " gomoku"},
        {"game_id": "gomoku", "limit": "0"},
        {"game_id": "gomoku", "limit": "51"},
        {"game_id": "gomoku", "limit": "bad"},
        {"game_id": "gomoku", "query": "bad\nquery"},
    ],
)
def test_source_candidate_query_rejects_invalid_inputs(tmp_path, params):
    app = _app(tmp_path)
    _setup(app)
    response = TestClient(app).get(
        "/api/contests/source-candidates",
        params=params,
        headers=_tok(app, "org"),
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    "query",
    [
        "page=0",
        "page=-1",
        "page=10001",
        "page=9999999999999999999999999999999999999999",
        "page=bad",
        "per_page=0",
        "per_page=201",
        "per_page=bad",
    ],
)
def test_public_contest_pagination_rejects_invalid_values(tmp_path, query):
    app = _app(tmp_path)
    _setup(app)
    response = TestClient(app).get(f"/api/contests?{query}")
    assert response.status_code == 422


def test_public_contest_list_is_bounded_even_when_page_is_omitted(tmp_path):
    app = _app(tmp_path)
    store, ctx = _setup(app)
    for index in range(25):
        store.create_contest(
            f"公开有界赛事 {index:02d}",
            ctx["org"]["id"],
            game_id="holdem",
            status="open",
        )
    payload = TestClient(app).get("/api/contests").json()
    assert payload["page"] == 1
    assert payload["per_page"] == 20
    assert payload["total"] == 26
    assert len(payload["contests"]) == 20


def test_store_contest_pagination_and_source_search_fail_closed(tmp_path):
    app = _app(tmp_path)
    store, ctx = _setup(app)

    assert store.list_contests(page=1, per_page=1)["page"] == 1
    invalid_pages = (True, 0, -1, 1.0, "1", 2**63 + 1)
    for page in invalid_pages:
        with pytest.raises(ValueError):
            store.list_contests(page=page, per_page=1)
    for per_page in (True, 0, 201, 1.0, "1"):
        with pytest.raises(ValueError):
            store.list_contests(page=1, per_page=per_page)

    # Store 保持跨游戏通用；默认模式仍是正式保护种子来源，Pencil
    # 导航候选由 API 显式切到 navigation 模式并套用隐藏赛事 ACL。
    pencil_source = store.create_contest(
        "点格棋通用来源",
        ctx["org"]["id"],
        game_id="pencil",
        status="finished",
    )
    store.update_contest(pencil_source["id"], official_results_ready=1)
    assert store.list_contest_source_candidates(game_id="pencil") == {
        "items": [{"id": pencil_source["id"], "title": pencil_source["title"]}],
        "has_more": False,
    }
    for game_id in (None, "", " gomoku", "GOMOKU", "unknown", True):
        with pytest.raises(ValueError):
            store.list_contest_source_candidates(game_id=game_id)
    with pytest.raises(ValueError):
        store.list_contest_source_candidates(game_id="gomoku", limit=51)
    with pytest.raises(ValueError):
        store.list_contest_source_candidates(game_id="gomoku", query="bad\x00query")
    with pytest.raises(ValueError):
        store.list_contest_source_candidates(game_id="gomoku", query=" " * 101)

    with store._tx() as conn:
        for page, per_page in (
            (True, 1),
            (1.0, 1),
            (1, True),
            (1, 201),
            (2**63 + 1, 1),
        ):
            with pytest.raises(ValueError):
                _paginate(
                    conn,
                    "SELECT id FROM contests ORDER BY id",
                    (),
                    page=page,
                    per_page=per_page,
                )


@pytest.mark.parametrize(
    "title",
    [
        "",
        "   ",
        " leading",
        "trailing ",
        "\u00a0nonbreaking",
        "figure\u2007",
        "\u202fnarrow",
        "line\nbreak",
        "c1\x85control",
        "x" * 121,
    ],
)
def test_contest_title_api_manager_and_store_reject_invalid_values(tmp_path, title):
    app = _app(tmp_path)
    store, ctx = _setup(app)
    client = TestClient(app)
    assert client.post(
        "/api/contests",
        json={"title": title},
        headers=_tok(app, "org"),
    ).status_code == 422
    with pytest.raises(ValueError, match="赛事标题"):
        app.state.contest_manager.create(ctx["org"]["id"], title)
    with pytest.raises(ValueError, match="赛事标题"):
        store.create_contest(title, ctx["org"]["id"])


def test_contest_title_boundary_and_raw_sql_guards(tmp_path):
    app = _app(tmp_path)
    store, ctx = _setup(app)
    valid = "界" * 120
    contest = store.create_contest(valid, ctx["org"]["id"], game_id="gomoku")
    assert contest["title"] == valid
    with pytest.raises(ValueError, match="赛事标题"):
        store.update_contest(contest["id"], title="x" * 121)
    with pytest.raises(sqlite3.IntegrityError, match="contest title invalid"):
        with store._tx() as connection:
            connection.execute(
                "UPDATE contests SET title=? WHERE id=?", ("bad\nraw", contest["id"])
            )
    with pytest.raises(sqlite3.IntegrityError, match="contest title invalid"):
        with store._tx() as connection:
            connection.execute(
                "INSERT INTO contests(title,organizer_id,created_at,game_id) "
                "VALUES(?,?,?,?)",
                ("x" * 121, ctx["org"]["id"], "2026-09-02", "gomoku"),
            )
    for whitespace in ("\u00a0", "\u2007", "\u202f"):
        for invalid in (whitespace + "raw", "raw" + whitespace):
            with pytest.raises(sqlite3.IntegrityError, match="contest title invalid"):
                with store._tx() as connection:
                    connection.execute(
                        "UPDATE contests SET title=? WHERE id=?",
                        (invalid, contest["id"]),
                    )
            with pytest.raises(sqlite3.IntegrityError, match="contest title invalid"):
                with store._tx() as connection:
                    connection.execute(
                        "INSERT INTO contests(title,organizer_id,created_at,game_id) "
                        "VALUES(?,?,?,?)",
                        (invalid, ctx["org"]["id"], "2026-09-02", "gomoku"),
                    )


@pytest.mark.parametrize("whitespace", ("\u00a0", "\u2007", "\u202f"))
def test_contest_title_legacy_corruption_fails_closed_on_reopen(
    tmp_path, whitespace
):
    app = _app(tmp_path)
    store, ctx = _setup(app)
    contest = store.create_contest("legacy title", ctx["org"]["id"])
    path = store.path
    store.close()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER trg_contest_title_guard_insert")
        connection.execute("DROP TRIGGER trg_contest_title_guard_update")
        connection.execute(
            "UPDATE contests SET title=? WHERE id=?",
            (whitespace + "损坏标题", contest["id"]),
        )
    with pytest.raises(RuntimeError, match="legacy contest title"):
        Store(path)
