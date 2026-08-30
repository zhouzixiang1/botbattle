"""botzone maintenance CLI：部署排空的本地运维入口。

begin/end 与 admin HTTP「准备维护/结束维护」共享 execution_control 的
同一事务语义；本测试锁定输出契约、控制位变化、确认门、schema 预检、
审计日志与冲突退出码。
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


def test_maintenance_begin_status_end_roundtrip(tmp_path):
    db = tmp_path / "m.db"
    repo = ExecutionRepository(Store(str(db)))  # 初始化 schema 与 control 默认态
    # fresh 控制行是 stopped；dispatcher 上线时才标记 running。CLI begin
    # 只对 running 态合法，这里模拟在线服务。
    assert repo.control()["dispatcher_state"] == "stopped"
    repo.set_control(dispatcher_state="running", accepting=True)
    control = repo.control()
    assert control["dispatcher_state"] == "running"
    assert int(control["accepting"]) == 1

    runner = CliRunner()
    begin = runner.invoke(
        cli_app,
        ["maintenance", "begin", "--db", str(db), "--reason", "部署测试"],
    )
    assert begin.exit_code == 0
    assert f"db={db.resolve()}" in begin.output
    begin_status = _last_json(begin.output)
    assert begin_status["requested"] is True
    assert begin_status["ready"] is True
    assert begin_status["active_count"] == 0
    control = _control(db)
    assert int(control["accepting"]) == 0
    assert int(control["auto_enabled"]) == 0
    assert int(control["deployment_drain_requested"]) == 1
    # begin 落一行邻接审计日志
    audit = db.parent / f"{db.name}.maintenance-cli.log"
    assert "action=begin" in audit.read_text(encoding="utf-8")

    status = runner.invoke(cli_app, ["maintenance", "status", "--db", str(db)])
    assert status.exit_code == 0
    assert _last_json(status.output) == begin_status

    # begin 幂等：重复提交仍是同一排空态
    again = runner.invoke(cli_app, ["maintenance", "begin", "--db", str(db)])
    assert again.exit_code == 0
    assert _last_json(again.output)["requested"] is True

    # end 必须显式确认服务已同版本重启
    unconfirmed = runner.invoke(cli_app, ["maintenance", "end", "--db", str(db)])
    assert unconfirmed.exit_code != 0
    assert int(_control(db)["deployment_drain_requested"]) == 1

    end = runner.invoke(
        cli_app,
        ["maintenance", "end", "--db", str(db), "--confirm-service-restarted"],
    )
    assert end.exit_code == 0
    end_status = _last_json(end.output)
    assert end_status["requested"] is False
    control = _control(db)
    assert int(control["accepting"]) == 1
    assert int(control["deployment_drain_requested"]) == 0
    # auto 不随结束维护隐式开启
    assert int(control["auto_enabled"]) == 0
    audit_text = audit.read_text(encoding="utf-8")
    assert "action=end" in audit_text

    # 非 maintenance 态下 end 幂等返回当前控制行，不报错
    repeat_end = runner.invoke(
        cli_app,
        ["maintenance", "end", "--db", str(db), "--confirm-service-restarted"],
    )
    assert repeat_end.exit_code == 0
    assert _last_json(repeat_end.output)["requested"] is False


def test_maintenance_begin_conflict_when_not_running(tmp_path):
    db = tmp_path / "paused.db"
    repo = ExecutionRepository(Store(str(db)))
    repo.pause("测试暂停")
    runner = CliRunner()
    paused = runner.invoke(cli_app, ["maintenance", "begin", "--db", str(db)])
    assert paused.exit_code == 3
    combined = paused.output + (
        paused.stderr if hasattr(paused, "stderr") else ""
    )
    assert "maintenance_state_conflict" in combined
    assert int(repo.control()["deployment_drain_requested"]) == 0

    stopped_db = tmp_path / "stopped.db"
    stopped_repo = ExecutionRepository(Store(str(stopped_db)))
    assert stopped_repo.control()["dispatcher_state"] == "stopped"
    stopped = runner.invoke(
        cli_app, ["maintenance", "begin", "--db", str(stopped_db)]
    )
    assert stopped.exit_code == 3
    assert "maintenance_state_conflict" in stopped.output + (
        stopped.stderr if hasattr(stopped, "stderr") else ""
    )


def test_maintenance_fail_closed_on_missing_db(tmp_path):
    missing = tmp_path / "nope.db"
    runner = CliRunner()
    for args in (
        ["maintenance", "begin", "--db", str(missing)],
        ["maintenance", "status", "--db", str(missing)],
        ["maintenance", "end", "--db", str(missing), "--confirm-service-restarted"],
    ):
        assert runner.invoke(cli_app, args).exit_code != 0
    # fail-closed：绝不静默新建数据库
    assert not missing.exists()


def test_maintenance_fail_closed_on_legacy_schema(tmp_path):
    db = tmp_path / "legacy.db"
    store = Store(str(db))
    store.close()
    import sqlite3

    con = sqlite3.connect(str(db))
    con.execute("ALTER TABLE execution_control RENAME TO execution_control_full")
    con.execute(
        "CREATE TABLE execution_control (singleton INTEGER PRIMARY KEY, "
        "accepting INTEGER, auto_enabled INTEGER)"
    )
    con.commit()
    con.close()
    runner = CliRunner()
    result = runner.invoke(cli_app, ["maintenance", "begin", "--db", str(db)])
    assert result.exit_code != 0
    assert "deployment_drain_requested" in result.output + (
        result.stderr if hasattr(result, "stderr") else ""
    )


def test_maintenance_rejects_unknown_action_and_long_reason(tmp_path):
    db = tmp_path / "u.db"
    Store(str(db)).close()
    runner = CliRunner()
    result = runner.invoke(cli_app, ["maintenance", "deploy", "--db", str(db)])
    assert result.exit_code != 0
    long_reason = runner.invoke(
        cli_app,
        ["maintenance", "begin", "--db", str(db), "--reason", "x" * 201],
    )
    assert long_reason.exit_code != 0
