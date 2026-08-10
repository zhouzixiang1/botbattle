"""Mutating QA helpers keep every artifact in an isolated runtime."""
from __future__ import annotations

import importlib.util
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from bzplat.backend.crypto import hash_password
from bzplat.backend.store import Store


ROOT = Path(__file__).resolve().parents[3]


def load_script(stem: str):
    module_name = f"qa_script_{stem}"
    spec = importlib.util.spec_from_file_location(
        module_name, ROOT / "scripts" / f"{stem}.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_contest_stress_default_uploads_follow_database(tmp_path):
    module = load_script("contest_stress")
    db_path = tmp_path / "runtime" / "contest.db"
    bot_ids, _admin_id, _token = module.seed(str(db_path), 1)

    store = Store(str(db_path))
    try:
        expected = (db_path.parent / "bot_uploads").resolve()
        for bot_id in bot_ids.values():
            binary = Path(store.get_bot(bot_id)["binary_path"]).resolve()
            assert expected in binary.parents
    finally:
        store.close()


def test_contest_stress_never_reuses_or_mutates_arbitrary_admin(tmp_path):
    module = load_script("contest_stress")
    db_path = tmp_path / "contest.db"
    store = Store(str(db_path))
    real_admin = store.create_user(
        "admin",
        "owner@example.com",
        hash_password("OwnerSecret1234"),
        role="admin",
    )
    store.update_user(real_admin["id"], is_active=0, email_verified=0)
    store.close()

    _bot_ids, admin_id, token = module.seed(str(db_path), 1)

    store = Store(str(db_path))
    try:
        untouched = store.get_user(real_admin["id"])
        dedicated = store.get_user_by_username(module.CONTEST_ADMIN_NAME)
        assert admin_id == dedicated["id"]
        assert dedicated["email"] == (
            f"{module.CONTEST_ADMIN_NAME}@{module.EMAIL_DOMAIN}"
        )
        assert store.get_session(token)["user_id"] == dedicated["id"]
        assert untouched["is_active"] == 0
        assert untouched["email_verified"] == 0
        assert store._conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE user_id=?",
            (real_admin["id"],),
        ).fetchone()[0] == 0
    finally:
        store.close()


def test_contest_stress_rejects_conflicting_dedicated_admin_before_writes(tmp_path):
    module = load_script("contest_stress")
    db_path = tmp_path / "contest.db"
    store = Store(str(db_path))
    namesake = store.create_user(
        module.CONTEST_ADMIN_NAME,
        "foreign@example.com",
        hash_password("ForeignSecret1234"),
        role="admin",
    )
    store.update_user(namesake["id"], is_active=0, email_verified=0)
    store.close()

    with pytest.raises(RuntimeError, match="专用 QA 身份契约不匹配"):
        module.seed(str(db_path), 1)

    store = Store(str(db_path))
    try:
        unchanged = store.get_user(namesake["id"])
        assert unchanged["is_active"] == 0
        assert unchanged["email_verified"] == 0
        assert store.get_user_by_username("cs_u001") is None
        assert store._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    finally:
        store.close()


def test_contest_stress_rejects_conflicting_seed_user_without_activation(tmp_path):
    module = load_script("contest_stress")
    db_path = tmp_path / "contest.db"
    store = Store(str(db_path))
    namesake = store.create_user(
        "cs_u001",
        "cs_u001@contest.local",
        hash_password(module.PASSWORD),
        role="organizer",
    )
    store.update_user(namesake["id"], is_active=0, email_verified=0)
    store.close()

    with pytest.raises(RuntimeError, match="role"):
        module.seed(str(db_path), 1)

    store = Store(str(db_path))
    try:
        unchanged = store.get_user(namesake["id"])
        assert unchanged["role"] == "organizer"
        assert unchanged["is_active"] == 0
        assert unchanged["email_verified"] == 0
    finally:
        store.close()


def test_api_account_seed_is_idempotent_in_isolated_database(tmp_path):
    module = load_script("api_full_test")
    db_path = tmp_path / "api.db"

    first = module.seed_qa_accounts(str(db_path))
    second = module.seed_qa_accounts(str(db_path))
    assert second == first

    store = Store(str(db_path))
    try:
        rows = [store.get_user_by_username(username) for username in first.values()]
        assert all(rows)
        assert len({row["id"] for row in rows}) == len(module.QA_ACCOUNTS)
        assert store.get_user_by_username(first["unverified"])["email_verified"] == 0
        assert all(
            store.get_user_by_username(first[name])["email_verified"] == 1
            for name in ("admin", "alice", "bob", "carol", "org1")
        )
    finally:
        store.close()


def test_api_replay_count_uses_nested_result_contract():
    module = load_script("api_full_test")
    assert module.match_rounds_played({"result": {"rounds_played": 70}}) == 70
    assert module.match_rounds_played({"rounds_played": 70}) == 0
    assert module.qa_contest_payload("run1")["template_id"] == "holdem_rr"


def test_api_no_smtp_registration_rolls_user_back_on_every_retry():
    module = load_script("api_full_test")
    ok, detail = module.verify_no_smtp_registration_rollback()
    assert ok, detail


def test_e2e_smoke_rejects_main_port_and_cleans_early_temp_runtime(tmp_path):
    script = ROOT / "scripts" / "e2e_smoke.sh"
    syntax = subprocess.run(
        ["bash", "-n", str(script)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert syntax.returncode == 0, syntax.stderr

    env = os.environ.copy()
    env.update(
        {
            "TMPDIR": str(tmp_path),
            "BZ_E2E_PORT": "50380",
            "BZ_PYTHON": sys.executable,
        }
    )
    rejected = subprocess.run(
        ["bash", str(script)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert rejected.returncode == 2
    assert "50380" in rejected.stderr
    assert list(tmp_path.iterdir()) == [], "early exit must remove its exact mktemp runtime"


def test_qa_scripts_do_not_probe_retired_matchpacks_or_override_fixed_rules():
    load_source = (ROOT / "scripts" / "load_test.py").read_text(encoding="utf-8")
    smoke_source = (ROOT / "scripts" / "e2e_smoke.sh").read_text(encoding="utf-8")

    assert "/api/matchpacks" not in load_source
    assert '"hands": 2' not in smoke_source
    assert "hands_per_match" not in smoke_source


def test_e2e_smoke_requires_completed_numeric_zero_sum_deltas():
    source = (ROOT / "scripts" / "e2e_smoke.sh").read_text(encoding="utf-8")

    assert 'if status == "aborted"' in source
    assert 'if status == "completed"' in source
    assert "len(deltas) != 2" in source
    assert "not isinstance(value, (int, float))" in source
    assert "deltas[0] + deltas[1] != 0" in source
    assert "or [0, 0]" not in source


class _FakeWebSocket:
    def __init__(self, events):
        self._events = iter(events)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def recv(self):
        return json.dumps(next(self._events))

    async def send(self, _payload):
        return None


class _FakeWebSockets:
    def __init__(self, events):
        self._events = events

    def connect(self, *_args, **_kwargs):
        return _FakeWebSocket(self._events)


def test_load_human_websocket_error_is_failure_and_match_end_is_success():
    module = load_script("load_test")
    api = type("ApiStub", (), {"base": "http://qa.invalid"})()

    failed = module._play_human_match(
        api,
        _FakeWebSockets(
            [
                {"type": "your_turn", "request": {}},
                {"type": "error", "reason": "platform_error"},
            ]
        ),
        asyncio,
        "match-1",
        "token",
        "holdem",
    )
    completed = module._play_human_match(
        api,
        _FakeWebSockets([{"type": "match_end"}]),
        asyncio,
        "match-2",
        "token",
        "holdem",
    )

    assert failed is False
    assert completed is True
    assert module.WARN == ["WS holdem 返回 error: platform_error"]


def test_load_human_phase_checks_persisted_completed_status():
    source = (ROOT / "scripts" / "load_test.py").read_text(encoding="utf-8")
    assert "api.wait_match(hu_tok, mid, timeout=30)" in source
    assert 'persisted and m.get("status") == "completed"' in source
    assert "持久化 status=completed" in source


def test_load_missing_websocket_dependency_is_a_hard_failure(monkeypatch):
    module = load_script("load_test")
    monkeypatch.setattr(module, "_websocket_dependencies", lambda: None)

    module.phase4_human(None, {})

    assert module.FAIL == 1
    assert any("WebSocket" in failure for failure in module.FAILS)
    assert not module.WARN


def test_load_test_does_not_mutate_code_owned_auto_match_configuration():
    module = load_script("load_test")
    source = (ROOT / "scripts" / "load_test.py").read_text(encoding="utf-8")

    assert not hasattr(module, "_record_auto_match_outcome")
    assert "--allow-auto-match-miss" not in source
    assert "PATCH runtime 写入口不存在" in source
    assert "公开赛制模板来自代码且只读" in source


def test_qa_script_claims_match_the_observed_coverage():
    load_source = (ROOT / "scripts" / "load_test.py").read_text(encoding="utf-8")
    api_source = (ROOT / "scripts" / "api_full_test.py").read_text(encoding="utf-8")
    contest_source = (ROOT / "scripts" / "contest_stress.py").read_text(encoding="utf-8")
    load_doc = (ROOT / "doc" / "LOADTEST.md").read_text(encoding="utf-8")
    doc_index = (ROOT / "doc" / "INDEX.md").read_text(encoding="utf-8")

    assert "CONCURRENCY =" not in load_source
    assert "--allow-auto-match-miss" not in load_source
    assert "不等待也不声称验证" in load_source
    assert "snapshot 之后的实时增量事件" in load_source
    assert "SSE 实时事件流" not in api_source
    assert "终态 snapshot" in api_source
    assert "首波精确接纳" in api_source
    assert "超额请求明确 429" in api_source
    assert "全部 {n} 局成功发起" not in api_source
    assert "持续打满并发对局（8 场）" not in load_doc
    assert "全端点覆盖" not in doc_index
    assert "生成赛程表" not in contest_source
    assert "查看赛程表" not in contest_source
    assert "不生成 pairings" in contest_source


def test_pencil_browser_regression_stays_bound_to_the_production_incident():
    """The browser suite must retain both the real incident and judge-backed replay."""
    source = (
        ROOT / "bzplat" / "frontend" / "e2e" / "qa-regression.spec.ts"
    ).read_text(encoding="utf-8")

    assert "20260810140318-a8752705" in source
    assert "productionBoxClick" in source
    assert "sentActions).toHaveLength(0)" in source
    assert "real Pencil human play accepts several canvas-picked edges" in source
    assert "Pencil human pass request disables the board" in source
    assert "Pencil human canvas exposes legal edges to keyboard" in source
    assert "response: { x: -1, y: -1 }" in source
    assert "event.type === 'illegal'" in source
