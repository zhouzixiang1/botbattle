"""裁判源码公开测试——验证 GET /api/judges 与 GET /api/judges/{id}/source
对全体玩家公开（无需登录），返回裁判元信息与明文源码。

裁判是公开可审计的规则定义（区别于 Bot 的私有黑盒二进制），源码必须对全体玩家透明。
"""
from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient
import pytest


def _app(tmp_path):
    from bzplat.backend.main import create_app

    return create_app(db_path=str(tmp_path / "jp.db"))


def test_public_judges_list_no_auth(tmp_path):
    """/api/judges 无需登录即可访问，返回三游戏元信息（含 source_files 列表）。"""
    app = _app(tmp_path)
    client = TestClient(app)
    resp = client.get("/api/judges")
    assert resp.status_code == 200
    data = resp.json()
    assert "games" in data
    gids = {g["game_id"] for g in data["games"]}
    # 三款内置游戏都应公开
    assert {"holdem", "gomoku", "pencil"} <= gids
    for g in data["games"]:
        # 每游戏必备字段
        assert g["label"]
        assert g["code_path"]
        assert g["summary"]
        # 权威纯规则 + 平台适配层/协议/结果契约均公开，其他包文件不泄露。
        assert isinstance(g["source_files"], list)
        assert set(g["source_files"]) == {
            f'{g["game_id"]}_judge.py',
            "engine.py",
            "protocol.py",
            "result.py",
        }
        assert g["shared_source_files"] == (
            [] if g["game_id"] == "holdem" else ["_board_protocol.py"]
        )
        # 公开端点不返回可调参数当前值（那是 admin 能力）
        assert "params" not in g


def test_public_judge_source_no_auth(tmp_path):
    """/api/judges/{game_id}/source 无需登录，返回纯规则与适配层源码。"""
    app = _app(tmp_path)
    client = TestClient(app)
    resp = client.get("/api/judges/holdem/source")
    assert resp.status_code == 200
    data = resp.json()
    assert data["game_id"] == "holdem"
    assert data["label"] == "德州扑克"
    files = {f["name"]: f for f in data["files"]}
    # 权威纯规则与三件套都公开
    assert "holdem_judge.py" in files
    assert "engine.py" in files
    assert "protocol.py" in files
    assert "result.py" in files
    # 纯规则源码含真实扑克规则类/函数，非平台适配层占位。
    src = files["holdem_judge.py"]["source"]
    assert isinstance(src, str)
    assert len(src) > 500  # 真实裁判引擎不是占位
    assert "class Holdem" in src
    assert "def compare_full_cards" in src
    # path 字段是受控的包内相对路径，不泄露绝对路径或上级文件。
    for name, item in files.items():
        assert item["path"] == f"backend/games/holdem/{name}"
        assert not item["path"].startswith("/")
        assert ".." not in item["path"]
    assert "/home/" not in resp.text
    assert "bot_uploads/" not in resp.text


def test_public_judge_source_unknown_game_404(tmp_path):
    """未注册的游戏返回 404。"""
    app = _app(tmp_path)
    client = TestClient(app)
    resp = client.get("/api/judges/nonexistent/source")
    assert resp.status_code == 404


def test_public_judges_gomoku_pencil(tmp_path):
    """gomoku / pencil 的权威纯规则源码同样可公开获取。"""
    app = _app(tmp_path)
    client = TestClient(app)
    expected = {
        "gomoku": ("五子棋", ("def check_win", "def is_legal_move")),
        "pencil": ("点格棋", ("class PencilBoard", "def do_action")),
    }
    for gid, (label, markers) in expected.items():
        resp = client.get(f"/api/judges/{gid}/source")
        assert resp.status_code == 200
        data = resp.json()
        assert data["game_id"] == gid
        assert data["label"] == label
        files = {f["name"]: f for f in data["files"]}
        assert set(files) == {
            f"{gid}_judge.py", "engine.py", "protocol.py", "result.py",
            "_board_protocol.py",
        }
        judge = files[f"{gid}_judge.py"]
        assert all(marker in judge["source"] for marker in markers)
        for name, item in files.items():
            expected_path = (
                "backend/games/_board_protocol.py"
                if name == "_board_protocol.py"
                else f"backend/games/{gid}/{name}"
            )
            assert item["path"] == expected_path
            assert not item["path"].startswith("/")
            assert ".." not in item["path"]
        serialized = resp.text
        assert "/home/" not in serialized
        assert "bot_uploads/" not in serialized


@pytest.mark.parametrize(
    "source_files",
    [
        ("../store/schema.py",),
        ("subdir/rules.py",),
        ("..\\store\\schema.py",),
        ("rules.txt",),
        ("engine.py", "protocol.py", "result.py"),
    ],
)
def test_game_spec_rejects_source_paths_outside_package_root(source_files):
    """source_files 是代码级白名单，不得经相对路径读取包外文件。"""
    from bzplat.backend.games.holdem.spec import SPEC

    with pytest.raises(ValueError, match="source_files"):
        replace(SPEC, source_files=source_files)


@pytest.mark.parametrize(
    "shared_source_files",
    [
        ("../store/schema.py",),
        ("subdir/protocol.py",),
        ("..\\store\\schema.py",),
        ("protocol.txt",),
        ("_board_protocol.py", "_board_protocol.py"),
    ],
)
def test_game_spec_rejects_unsafe_shared_source_paths(shared_source_files):
    from bzplat.backend.games.gomoku.spec import SPEC

    with pytest.raises(ValueError, match="shared_source_files"):
        replace(SPEC, shared_source_files=shared_source_files)
