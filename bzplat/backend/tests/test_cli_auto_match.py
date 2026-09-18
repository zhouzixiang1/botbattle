"""botzone auto-match CLI：自动排位总开关的本地运维入口。

on/off/status 与 admin HTTP「PUT /api/admin/auto-match」共享
execution_control 的同一事务语义；本测试锁定输出契约、控制位变化、
维护期冲突、QA capability guard、审计日志与 fail-closed 行为。
"""

import json

from typer.testing import CliRunner

from bzplat.backend.cli import app as cli_app
from bzplat.backend.store.db import Store
from bzplat.backend.store.execution import ExecutionRepository


def _control(db_path) -> dict:
    return ExecutionRepository(Store(str(db_path))).control()


def _last_json(stdout: str) -> dict:
    lines = [line for line in stdout.splitlines() if line.strip()]
    return json.loads(lines[-1])


def test_auto_match_on_off_status_roundtrip(tmp_path):
    db = tmp_path / "a.db"
    repo = ExecutionRepository(Store(str(db)))
    repo.set_control(dispatcher_state="running", accepting=True)
    # fresh 控制行默认 auto_enabled=1；归一到关闭再验证 CLI 行为。
    repo.set_auto_enabled(False)
    runner = CliRunner()

    status = runner.invoke(cli_app, ["auto-match", "status", "--db", str(db)])
    assert status.exit_code == 0
    payload = _last_json(status.output)
    assert payload["auto_enabled"] is False
    assert payload["dispatcher_state"] == "running"

    on = runner.invoke(cli_app, ["auto-match", "on", "--db", str(db)])
    assert on.exit_code == 0
    payload = _last_json(on.output)
    assert payload == {"auto_enabled": True, "previous": False}
    assert int(_control(db)["auto_enabled"]) == 1
    audit = db.parent / f"{db.name}.control-cli.log"
    assert "action=on auto_enabled=1 previous=0" in audit.read_text(encoding="utf-8")

    # 幂等：重复 on 报告 previous=True
    again = runner.invoke(cli_app, ["auto-match", "on", "--db", str(db)])
    assert again.exit_code == 0
    assert _last_json(again.output) == {"auto_enabled": True, "previous": True}

    off = runner.invoke(cli_app, ["auto-match", "off", "--db", str(db)])
    assert off.exit_code == 0
    assert _last_json(off.output) == {"auto_enabled": False, "previous": True}
    assert int(_control(db)["auto_enabled"]) == 0
    assert "action=off auto_enabled=0 previous=1" in audit.read_text(encoding="utf-8")


def test_auto_match_on_conflict_during_maintenance(tmp_path):
    db = tmp_path / "m.db"
    repo = ExecutionRepository(Store(str(db)))
    repo.set_control(dispatcher_state="running", accepting=True)
    repo.begin_maintenance("部署测试")
    runner = CliRunner()
    denied = runner.invoke(cli_app, ["auto-match", "on", "--db", str(db)])
    assert denied.exit_code == 3
    combined = denied.output + (
        denied.stderr if hasattr(denied, "stderr") else ""
    )
    assert "maintenance_active" in combined
    assert int(_control(db)["auto_enabled"]) == 0
    # 维护中允许 off（幂等收口）与 status
    assert runner.invoke(cli_app, ["auto-match", "status", "--db", str(db)]).exit_code == 0


def test_auto_match_qa_guard_and_fail_closed(tmp_path, monkeypatch):
    db = tmp_path / "q.db"
    repo = ExecutionRepository(Store(str(db)))
    repo.set_auto_enabled(False)
    runner = CliRunner()
    monkeypatch.setenv("BZ_QA_INSTANCE", "1")
    guarded = runner.invoke(cli_app, ["auto-match", "on", "--db", str(db)])
    assert guarded.exit_code == 3
    assert "qa_capability_guard" in guarded.output + (
        guarded.stderr if hasattr(guarded, "stderr") else ""
    )
    assert int(_control(db)["auto_enabled"]) == 0
    monkeypatch.delenv("BZ_QA_INSTANCE")

    missing = tmp_path / "nope.db"
    for args in (
        ["auto-match", "on", "--db", str(missing)],
        ["auto-match", "bogus", "--db", str(db)],
    ):
        assert runner.invoke(cli_app, args).exit_code != 0
    assert not missing.exists()
