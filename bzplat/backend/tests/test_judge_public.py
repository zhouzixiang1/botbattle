"""裁判源码公开测试——验证 GET /api/judges 与 GET /api/judges/{id}/source
对全体玩家公开（无需登录），返回裁判元信息与明文源码。

裁判是公开可审计的规则定义（区别于 Bot 的私有黑盒二进制），源码必须对全体玩家透明。
"""
from __future__ import annotations

from fastapi.testclient import TestClient


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
        # source_files 必含 engine.py（裁判核心）
        assert isinstance(g["source_files"], list)
        assert "engine.py" in g["source_files"]
        # 公开端点不返回可调参数当前值（那是 admin 能力）
        assert "params" not in g


def test_public_judge_source_no_auth(tmp_path):
    """/api/judges/{game_id}/source 无需登录，返回 engine.py 明文全文（含真实裁判逻辑）。"""
    app = _app(tmp_path)
    client = TestClient(app)
    resp = client.get("/api/judges/holdem/source")
    assert resp.status_code == 200
    data = resp.json()
    assert data["game_id"] == "holdem"
    assert data["label"]
    files = {f["name"]: f for f in data["files"]}
    # 三件套都公开
    assert "engine.py" in files
    assert "protocol.py" in files
    assert "result.py" in files
    # engine.py 源码含真实裁判标记（class / def 等，非空非占位）
    src = files["engine.py"]["source"]
    assert isinstance(src, str)
    assert len(src) > 500  # 真实裁判引擎不是占位
    assert "class " in src or "def " in src
    # path 字段是相对仓库根的可读路径
    assert "holdem" in files["engine.py"]["path"]


def test_public_judge_source_unknown_game_404(tmp_path):
    """未注册的游戏返回 404。"""
    app = _app(tmp_path)
    client = TestClient(app)
    resp = client.get("/api/judges/nonexistent/source")
    assert resp.status_code == 404


def test_public_judges_gomoku_pencil(tmp_path):
    """gomoku / pencil 的源码同样可公开获取。"""
    app = _app(tmp_path)
    client = TestClient(app)
    for gid in ("gomoku", "pencil"):
        resp = client.get(f"/api/judges/{gid}/source")
        assert resp.status_code == 200
        data = resp.json()
        assert data["game_id"] == gid
        files = {f["name"] for f in data["files"]}
        assert "engine.py" in files
