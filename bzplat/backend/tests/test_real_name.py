"""实名信息重构测试。

验证：
1. users 表有 real_name/phone/school/student_id 列
2. contests 表有 require_real_name 列
3. create_user 带实名 / 不带实名
4. update_user 白名单含实名字段（补填生效）
5. PUT /api/auth/profile 补填实名 + 手机号格式校验
6. POST /api/auth/register 注册时选填实名
7. 建赛 require_real_name + 报名校验（要求时拒未填/接受已填）
8. 公开主页 user_profile 不返回实名字段（隐私）
9. 实名开关、资料更新与报名快照在双 Store 下线性化
"""
from __future__ import annotations

import asyncio
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import bzplat.backend.store.db as store_db_module
from bzplat.backend.crypto import hash_password
from bzplat.backend.store import Store
from fastapi.testclient import TestClient


def _store(tmp_path):
    return Store(str(tmp_path / "rn.db"))


def _app(tmp_path):
    from bzplat.backend.main import create_app

    return create_app(db_path=str(tmp_path / "app.db"))


_ENTRY_WRITERS = ("add_entry", "add_contest_entry_once", "roster")


def _write_entry(
    store: Store,
    method: str,
    contest_id: int,
    user_id: int,
    bot_id: int,
) -> dict:
    if method == "add_entry":
        return store.add_entry(contest_id, user_id, bot_id)
    if method == "add_contest_entry_once":
        return store.add_contest_entry_once(contest_id, user_id, bot_id)
    assert method == "roster"
    added, skipped = store.add_contest_roster_entries(
        contest_id,
        [(user_id, bot_id)],
        allow_real_name_override=True,
    )
    assert skipped == []
    assert len(added) == 1
    return added[0]


def _race_registration_against_writer(
    first: Store,
    second: Store,
    monkeypatch: pytest.MonkeyPatch,
    *,
    method: str,
    contest_id: int,
    user_id: int,
    bot_id: int,
    mutate,
) -> tuple[dict, dict[str, object]]:
    """Pause after identity read and prove another Store cannot write yet.

    The second connection uses a short busy timeout for a deterministic lock
    result instead of a scheduling-sensitive sleep.  After the registration
    commits, the exact same mutation is retried against the final state.
    """
    original_identity = store_db_module._registration_identity_tx
    aligned = threading.Barrier(2)
    release_registration = threading.Event()
    retry_writer = threading.Event()
    first_attempt_finished = threading.Event()
    outcome: dict[str, object] = {}

    def paused_identity(conn, cid, uid, *, captured_at):
        identity = original_identity(
            conn, cid, uid, captured_at=captured_at
        )
        aligned.wait(timeout=5)
        if not release_registration.wait(timeout=5):
            raise TimeoutError("registration release timed out")
        return identity

    monkeypatch.setattr(
        store_db_module, "_registration_identity_tx", paused_identity
    )
    second._conn.execute("PRAGMA busy_timeout=50")

    def register() -> dict:
        return _write_entry(first, method, contest_id, user_id, bot_id)

    def concurrent_write() -> None:
        aligned.wait(timeout=5)
        try:
            outcome["first_result"] = mutate(second)
        except sqlite3.OperationalError as exc:
            outcome["first_error"] = exc
            first_attempt_finished.set()
            if not retry_writer.wait(timeout=5):
                raise TimeoutError("writer retry timed out")
            second._conn.execute("PRAGMA busy_timeout=5000")
            try:
                outcome["retry_result"] = mutate(second)
            except Exception as retry_exc:  # asserted by each caller
                outcome["retry_error"] = retry_exc
            return
        first_attempt_finished.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        registration_future = pool.submit(register)
        writer_future = pool.submit(concurrent_write)
        try:
            assert first_attempt_finished.wait(timeout=5)
            first_error = outcome.get("first_error")
            assert isinstance(first_error, sqlite3.OperationalError)
            assert "locked" in str(first_error).lower()
        finally:
            # Always release both workers so an assertion produces a normal
            # failure rather than leaking a blocked thread into the test run.
            release_registration.set()
        entry = registration_future.result(timeout=5)
        retry_writer.set()
        writer_future.result(timeout=5)
    return entry, outcome


def test_users_has_real_name_columns(tmp_path):
    """users 表有 4 个实名字段列。"""
    s = _store(tmp_path)
    with s._tx() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(users)")}
    s.close()
    for col in ("real_name", "phone", "school", "student_id"):
        assert col in cols, f"users 应有 {col}"


def test_contests_has_require_real_name(tmp_path):
    """contests 表有 require_real_name 列。"""
    s = _store(tmp_path)
    with s._tx() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(contests)")}
    s.close()
    assert "require_real_name" in cols


def test_create_user_with_real_name(tmp_path):
    """create_user 带实名字段。"""
    s = _store(tmp_path)
    u = s.create_user(
        "rnuser1", "rn1@e.com", "x",
        real_name="张三", phone="13800138000", school="测试大学", student_id="2024001",
    )
    assert u["real_name"] == "张三"
    assert u["phone"] == "13800138000"
    assert u["school"] == "测试大学"
    assert u["student_id"] == "2024001"
    s.close()


def test_create_user_without_real_name_defaults_empty(tmp_path):
    """不带实名 → 4 字段默认空字符串。"""
    s = _store(tmp_path)
    u = s.create_user("rnuser2", "rn2@e.com", "x")
    assert u["real_name"] == ""
    assert u["phone"] == ""
    s.close()


def test_update_user_real_name_whitelist(tmp_path):
    """update_user 白名单含实名字段（补填生效，不被吞）。"""
    s = _store(tmp_path)
    uid = s.create_user("rnuser3", "rn3@e.com", "x")["id"]
    s.update_user(uid, real_name="李四", phone="13900139000", school="补填大学", student_id="2024002")
    u = s.get_user(uid)
    assert u["real_name"] == "李四"
    assert u["phone"] == "13900139000"
    assert u["school"] == "补填大学"
    assert u["student_id"] == "2024002"
    s.close()


def test_profile_update_endpoint_real_name(tmp_path):
    """PUT /api/auth/profile 补填实名。"""
    from bzplat.backend.main import create_app

    app = _app(tmp_path)
    store = app.state.store
    u = store.create_user("rnuser4", "rn4@ex.com", hash_password("pw123456"))
    store.update_user(u["id"], email_verified=1)
    client = TestClient(app)
    _, tok = app.state.auth.authenticate("rnuser4", "pw123456")
    h = {"Authorization": f"Bearer {tok}"}
    r = client.put("/api/auth/profile", json={
        "real_name": "王五", "phone": "13700137000", "school": "API大学", "student_id": "2024003",
    }, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["user"]["real_name"] == "王五"
    assert r.json()["user"]["phone"] == "13700137000"


def test_profile_update_phone_validation(tmp_path):
    """PUT /api/auth/profile 手机号格式校验（非法 → 400）。"""
    app = _app(tmp_path)
    store = app.state.store
    u = store.create_user("rnuser5", "rn5@ex.com", hash_password("pw123456"))
    store.update_user(u["id"], email_verified=1)
    client = TestClient(app)
    _, tok = app.state.auth.authenticate("rnuser5", "pw123456")
    h = {"Authorization": f"Bearer {tok}"}
    r = client.put("/api/auth/profile", json={"phone": "not-a-phone"}, headers=h)
    assert r.status_code == 400
    # 合法手机号（空值跳过校验）
    r2 = client.put("/api/auth/profile", json={"phone": "13800138000"}, headers=h)
    assert r2.status_code == 200
    # 空值跳过（清空允许）
    r3 = client.put("/api/auth/profile", json={"phone": ""}, headers=h)
    assert r3.status_code == 200


def test_register_with_real_name(tmp_path):
    """注册时选填实名（经 AuthManager.register）。"""
    s = _store(tmp_path)
    from bzplat.backend.auth.auth_manager import AuthManager

    auth = AuthManager(s)
    u = auth.register(
        "rnuser6", "rn6@e.com", "password123",
        real_name="赵六", phone="13600136000", school="注册大学", student_id="2024004",
    )
    assert u["real_name"] == "赵六"
    s.close()


def test_contest_require_real_name_register_check(tmp_path):
    """建赛 require_real_name + 报名校验（要求时拒未填/接受已填）。"""
    from bzplat.backend.contests.manager import ContestManager

    s = _store(tmp_path)
    org = s.create_user("rnorg", "rno@e.com", "x", role="organizer")["id"]
    ua = s.create_user("rnu", "rnu@e.com", "x")["id"]  # 未填实名
    ba = s.create_bot(ua, "rnbotA", binary_path="/tmp", format="elf", game_id="holdem")["id"]
    cm = ContestManager(s, type("X", (), {"challenge": lambda self, *a, **k: None})())
    c = cm.create(org, "实名赛", game_id="holdem", require_real_name=1)["id"]
    assert int(s.get_contest(c)["require_real_name"]) == 1
    s.update_contest(c, status="open")
    # register 校验：未填实名 → 拒
    try:
        asyncio.run(cm.register(c, ua, ba))
        assert False, "未填实名应被拒"
    except ValueError as e:
        assert "实名" in str(e)
    # Store 低层写入口也必须复核，组织者/admin 不能绕过实名完整性。
    with pytest.raises(ValueError, match="实名"):
        s.add_contest_entry(c, ua, ba)
    # 已填实名 → 接受并冻结报名快照。
    s.update_user(ua, real_name="孙八", phone="13400134000", school="补填大学", student_id="2024006")
    result = asyncio.run(cm.register(c, ua, ba))
    assert result is not None  # 注册成功
    assert result["identity_source"] == "registration_profile"
    assert result["real_name_snapshot"] == "孙八"
    assert result["identity_captured_at"]
    s.close()


@pytest.mark.parametrize("entry_writer", _ENTRY_WRITERS)
def test_require_real_name_toggle_cannot_cross_registration_linearization(
    tmp_path, monkeypatch, entry_writer
):
    """A 0→1 toggle cannot slip between the identity read and entry insert."""
    db_path = tmp_path / f"identity-toggle-{entry_writer}.db"
    first = Store(str(db_path))
    user = first.create_user(
        f"toggle-{entry_writer}",
        f"toggle-{entry_writer}@e.com",
        "x",
        real_name="不得读取姓名",
        phone="13800138000",
        school="不得读取学校",
        student_id="PRIVATE001",
    )
    bot = first.create_bot(
        user["id"],
        f"toggle-bot-{entry_writer}",
        binary_path="/tmp/toggle-bot",
        format="elf",
        game_id="holdem",
    )
    contest = first.create_contest(
        f"toggle-{entry_writer}",
        organizer_id=user["id"],
        status="open",
        game_id="holdem",
        require_real_name=0,
    )
    second = Store(str(db_path))
    try:
        entry, outcome = _race_registration_against_writer(
            first,
            second,
            monkeypatch,
            method=entry_writer,
            contest_id=contest["id"],
            user_id=user["id"],
            bot_id=bot["id"],
            mutate=lambda store: store.update_contest(
                contest["id"], require_real_name=1
            ),
        )

        retry_error = outcome.get("retry_error")
        assert isinstance(retry_error, ValueError)
        assert "已有报名" in str(retry_error)
        assert int(first.get_contest(contest["id"])["require_real_name"]) == 0
        snapshot_fields = (
            "real_name_snapshot",
            "phone_snapshot",
            "school_snapshot",
            "student_id_snapshot",
            "identity_captured_at",
            "identity_source",
        )
        assert all(entry[field] is None for field in snapshot_fields)

        # Even an over-broad private-read request stays PII-free because the
        # final product gate is still non-real-name and can no longer drift.
        named = first.contest_entries_named(
            contest["id"], include_identity=True
        )[0]
        assert not any(
            field in named
            for field in (
                "real_name", "phone", "school", "student_id",
                "identity_source", "identity_captured_at",
            )
        )
        exported = first.list_contest_export(contest["id"])[0]
        assert all(
            exported[field] is None
            for field in (
                "real_name", "phone", "school", "student_id",
                "identity_source", "identity_captured_at",
            )
        )
    finally:
        second.close()
        first.close()


@pytest.mark.parametrize("entry_writer", _ENTRY_WRITERS)
def test_profile_update_waits_for_registration_snapshot_commit(
    tmp_path, monkeypatch, entry_writer
):
    """Snapshot fields are one old profile; the new profile commits afterwards."""
    db_path = tmp_path / f"identity-profile-{entry_writer}.db"
    first = Store(str(db_path))
    old_profile = {
        "real_name": "报名时姓名",
        "phone": "01001234567",
        "school": "报名时学校",
        "student_id": "000042",
    }
    new_profile = {
        "real_name": "报名后姓名",
        "phone": "13999999999",
        "school": "报名后学校",
        "student_id": "CHANGED042",
    }
    user = first.create_user(
        f"profile-{entry_writer}",
        f"profile-{entry_writer}@e.com",
        "x",
        **old_profile,
    )
    bot = first.create_bot(
        user["id"],
        f"profile-bot-{entry_writer}",
        binary_path="/tmp/profile-bot",
        format="elf",
        game_id="holdem",
    )
    contest = first.create_contest(
        f"profile-{entry_writer}",
        organizer_id=user["id"],
        status="open",
        game_id="holdem",
        require_real_name=1,
    )
    second = Store(str(db_path))
    try:
        entry, outcome = _race_registration_against_writer(
            first,
            second,
            monkeypatch,
            method=entry_writer,
            contest_id=contest["id"],
            user_id=user["id"],
            bot_id=bot["id"],
            mutate=lambda store: store.update_user(user["id"], **new_profile),
        )

        assert "retry_error" not in outcome
        assert outcome["retry_result"]["real_name"] == new_profile["real_name"]
        assert first.get_user(user["id"])["real_name"] == new_profile["real_name"]
        assert entry["real_name_snapshot"] == old_profile["real_name"]
        assert entry["phone_snapshot"] == old_profile["phone"]
        assert entry["school_snapshot"] == old_profile["school"]
        assert entry["student_id_snapshot"] == old_profile["student_id"]
        assert entry["identity_source"] == "registration_profile"
        assert entry["identity_captured_at"] == entry["registered_at"]

        exported = first.list_contest_export(contest["id"])[0]
        assert exported["real_name"] == old_profile["real_name"]
        assert exported["phone"] == old_profile["phone"]
        assert exported["school"] == old_profile["school"]
        assert exported["student_id"] == old_profile["student_id"]
        assert new_profile["real_name"] != exported["real_name"]
    finally:
        second.close()
        first.close()


@pytest.mark.parametrize("initial,target", [(0, 1), (1, 0)])
def test_require_real_name_is_immutable_after_first_entry(
    tmp_path, initial, target
):
    store = Store(str(tmp_path / f"identity-immutable-{initial}-{target}.db"))
    user = store.create_user(
        f"immutable-{initial}",
        f"immutable-{initial}@e.com",
        "x",
        real_name="固定姓名",
        phone="13800138000",
        school="固定学校",
        student_id="FIXED001",
    )
    bot = store.create_bot(
        user["id"],
        f"immutable-bot-{initial}",
        binary_path="/tmp/immutable-bot",
        format="elf",
        game_id="holdem",
    )
    contest = store.create_contest(
        f"immutable-{initial}",
        organizer_id=user["id"],
        status="open",
        game_id="holdem",
        require_real_name=initial,
    )
    store.add_contest_entry(contest["id"], user["id"], bot["id"])

    with pytest.raises(ValueError, match="已有报名"):
        store.update_contest(contest["id"], require_real_name=target)
    assert int(store.get_contest(contest["id"])["require_real_name"]) == initial
    store.close()


def test_require_real_name_can_change_before_first_entry(tmp_path):
    store = Store(str(tmp_path / "identity-mutable-before-entry.db"))
    organizer = store.create_user("pre-entry-org", "pre-entry-org@e.com", "x")
    contest = store.create_contest(
        "pre-entry",
        organizer_id=organizer["id"],
        game_id="holdem",
        require_real_name=0,
    )
    assert store.update_contest(
        contest["id"], require_real_name=1
    )["require_real_name"] == 1
    assert store.update_contest(
        contest["id"], require_real_name=0
    )["require_real_name"] == 0
    store.close()


@pytest.mark.parametrize("reader_kind", ["named", "export"])
def test_identity_read_gate_and_projection_share_one_sql_snapshot(
    tmp_path, monkeypatch, reader_kind
):
    """A replacement non-real roster cannot inherit a stale private read gate.

    Pausing projection construction is intentional: the old implementation had
    already executed its autocommit contest-gate SELECT at this boundary.  The
    fixed implementation has not executed the joined row SELECT yet, so the
    replacement row and its gate are observed from one SQLite statement.
    """
    db_path = tmp_path / f"identity-read-gate-{reader_kind}.db"
    first = Store(str(db_path))
    private_values = (
        "PRIVATE-NAME",
        "01001234567",
        "PRIVATE-SCHOOL",
        "000042",
    )
    user = first.create_user(
        f"read-{reader_kind}",
        f"read-{reader_kind}@e.com",
        "x",
        real_name=private_values[0],
        phone=private_values[1],
        school=private_values[2],
        student_id=private_values[3],
    )
    bot = first.create_bot(
        user["id"],
        f"read-bot-{reader_kind}",
        binary_path="/tmp/read-gate-bot",
        format="elf",
        game_id="holdem",
    )
    contest = first.create_contest(
        f"read-gate-{reader_kind}",
        organizer_id=user["id"],
        status="open",
        game_id="holdem",
        require_real_name=1,
    )
    first.add_entry(contest["id"], user["id"], bot["id"])
    second = Store(str(db_path))
    original_projection = store_db_module._contest_identity_projection_sql
    projection_boundary = threading.Barrier(2)
    release_reader = threading.Event()
    first_call_lock = threading.Lock()
    first_call = True

    def paused_projection(*args, **kwargs):
        nonlocal first_call
        with first_call_lock:
            should_pause = first_call
            first_call = False
        if should_pause:
            projection_boundary.wait(timeout=5)
            if not release_reader.wait(timeout=5):
                raise TimeoutError("identity reader release timed out")
        return original_projection(*args, **kwargs)

    monkeypatch.setattr(
        store_db_module, "_contest_identity_projection_sql", paused_projection
    )

    def read_rows():
        if reader_kind == "named":
            return first.contest_entries_named(
                contest["id"], include_identity=True
            )
        return first.list_contest_export(contest["id"])

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(read_rows)
            projection_boundary.wait(timeout=5)
            try:
                assert second.delete_contest_roster_entry(
                    contest["id"], user["id"]
                )
                second.update_contest(contest["id"], require_real_name=0)
                replacement = second.add_entry(
                    contest["id"], user["id"], bot["id"]
                )
            finally:
                release_reader.set()
            rows = future.result(timeout=5)

        assert int(first.get_contest(contest["id"])["require_real_name"]) == 0
        assert all(
            replacement[field] is None
            for field in (
                "real_name_snapshot",
                "phone_snapshot",
                "school_snapshot",
                "student_id_snapshot",
                "identity_captured_at",
                "identity_source",
            )
        )
        assert len(rows) == 1
        row = rows[0]
        identity_fields = (
            "real_name",
            "phone",
            "school",
            "student_id",
            "identity_source",
            "identity_captured_at",
            "identity_complete",
        )
        if reader_kind == "named":
            assert all(field not in row for field in identity_fields)
        else:
            assert row["identity_required"] == 0
            assert all(row[field] is None for field in identity_fields)
        assert all(value not in repr(rows) for value in private_values)
    finally:
        release_reader.set()
        second.close()
        first.close()


def test_user_profile_does_not_leak_real_name(tmp_path):
    """公开主页 user_profile 不返回实名字段（隐私）。"""
    s = _store(tmp_path)
    s.create_user("rnshow", "rns@e.com", "x", real_name="私密姓名", phone="13300133000", school="私密大学", student_id="私密学号")
    profile = s.user_profile("rnshow")
    assert "real_name" not in profile
    assert "phone" not in profile
    assert "school" not in profile
    assert "student_id" not in profile
    s.close()


def test_contest_identity_contract_docs_are_not_the_legacy_join_model():
    """Keep the root architecture guide aligned with the enforced PII boundary."""
    root = Path(__file__).resolve().parents[3]
    agents = (root / "AGENTS.md").read_text(encoding="utf-8")
    assert "contest_entries_named JOIN 实名字段，但" not in agents
    for marker in (
        "报名时",
        "current_profile_legacy",
        "同一 SQL",
        "official-results",
        "CSV v1",
        "schema=2",
        "29 列",
    ):
        assert marker in agents
