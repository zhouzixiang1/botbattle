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
"""
from __future__ import annotations

from bzplat.backend.crypto import hash_password
from bzplat.backend.store import Store
from fastapi.testclient import TestClient


def _store(tmp_path):
    return Store(str(tmp_path / "rn.db"))


def _app(tmp_path):
    from bzplat.backend.main import create_app

    return create_app(db_path=str(tmp_path / "app.db"))


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
    ub = s.create_user("rnu2", "rnu2@e.com", "x", real_name="钱七", phone="13500135000", school="有实名大学", student_id="2024005")["id"]
    ba = s.create_bot(ua, "rnbotA", binary_path="/tmp", format="elf", game_id="holdem")["id"]
    bb = s.create_bot(ub, "rnbotB", binary_path="/tmp", format="elf", game_id="holdem")["id"]
    cm = ContestManager(s, type("X", (), {"challenge": lambda self, *a, **k: None})())
    c = cm.create(org, "实名赛", game_id="holdem", require_real_name=1)["id"]
    assert int(s.get_contest(c)["require_real_name"]) == 1
    s.update_contest(c, status="open")
    s.add_contest_entry(c, ua, ba)  # 手动加（绕开 register 校验，便于测试）
    s.add_contest_entry(c, ub, bb)
    # register 校验：未填实名 → 拒
    try:
        cm.register(c, ua, ba)
        assert False, "未填实名应被拒"
    except ValueError as e:
        assert "实名" in str(e)
    # 已填实名 → 接受（但已报名会因去重拒，先删 entry 再测）
    s.delete_entry(c, ua)
    s.update_user(ua, real_name="孙八", phone="13400134000", school="补填大学", student_id="2024006")
    result = cm.register(c, ua, ba)
    assert result is not None  # 注册成功
    s.close()


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
