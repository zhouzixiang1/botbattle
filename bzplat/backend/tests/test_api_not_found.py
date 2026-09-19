"""未知 /api/* 路径应返 JSON 404，不返 SPA HTML（catch-all 契约修复）。

原 main.py 的 SPA catch-all（/{full_path:path}）会吞掉未注册的 /api/* 返 200 + index.html，
导致前端 api.ts 把 HTML 当返回值解析成静默错误数据。修复：catch-all 之前加 /api/{rest:path}
fallback raise HTTPException(404)。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bzplat.backend.main import create_app

# SPA catch-all 仅在 frontend/dist 存在时挂载（main.py: if dist.is_dir()）。
# 单元测试环境无 dist（不 build 前端）；回归测试据此跳过。
_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


def _client(tmp_path):
    app = create_app(db_path=str(tmp_path / "nf.db"))
    return TestClient(app)


def test_get_unknown_api_returns_json_404(tmp_path):
    """GET 未注册的 /api/* → 404 + JSON（不是 200 HTML）。"""
    c = _client(tmp_path)
    r = c.get("/api/this-route-does-not-exist")
    assert r.status_code == 404, f"未知 API 应 404，实际 {r.status_code}"
    ct = r.headers.get("content-type", "")
    assert "application/json" in ct, f"应返 JSON，实际 Content-Type={ct}"
    body = r.json()
    assert "detail" in body, f"404 body 应含 detail，实际 {body}"
    # 防回归：响应体不是 HTML
    text = r.text.lstrip()
    assert not text.startswith("<"), f"响应体不应是 HTML，实际: {text[:80]!r}"


def test_post_unknown_api_returns_json_404(tmp_path):
    """POST 未注册的 /api/* → 404 JSON（多 method 覆盖）。"""
    c = _client(tmp_path)
    r = c.post("/api/no-such-endpoint", json={"x": 1})
    assert r.status_code == 404
    assert "application/json" in r.headers.get("content-type", "")
    assert "detail" in r.json()


def test_spa_fallback_preserved_for_frontend_routes(tmp_path):
    """回归保护：非 /api 的未知前端路径仍返 SPA HTML（200），catch-all 未被破坏。

    仅在 frontend/dist 存在时验证（SPA catch-all 依赖 dist）；测试环境无 dist 则跳过。
    """
    if not _DIST.is_dir():
        pytest.skip("无 frontend/dist（测试环境不 build 前端）；SPA catch-all 未挂载")
    c = _client(tmp_path)
    r = c.get("/arena")  # 旧前端路由（已删但仍走 SPA fallback）
    assert r.status_code == 200, f"前端路径应走 SPA 返 200，实际 {r.status_code}"
    assert "text/html" in r.headers.get("content-type", "")
    # 应是 index.html（SPA）
    assert "<html" in r.text.lower() or "<!doctype" in r.text.lower(), (
        "前端路径应返 SPA HTML"
    )


def test_api_health_still_works(tmp_path, monkeypatch):
    """回归保护：已注册的 /api/health 正常返 JSON（fallback 不影响真实路由）。"""
    monkeypatch.setenv("BZ_QA_INSTANCE", "1")
    c = _client(tmp_path)
    r = c.get("/api/health")
    assert r.status_code == 200
    assert "application/json" in r.headers.get("content-type", "")
    body = r.json()
    assert body["ok"] is True
    assert body["qa_instance"] is True
    assert "db" not in body  # 公开健康检查不得泄漏服务器绝对路径
    assert c.app.state.bot_manager.upload_root == (tmp_path / "bot_uploads").resolve()


def test_qa_database_guard_runs_before_store_open(tmp_path, monkeypatch):
    """A forged QA marker must fail before Store can create/migrate the target."""
    import bzplat.backend.main as main_module

    target = tmp_path / "must-not-exist.db"
    monkeypatch.setenv("BZ_QA_INSTANCE", "1")

    def reject(_db_path, _source_root):
        assert not target.exists()
        raise RuntimeError("unsafe primary DB")

    monkeypatch.setattr(main_module, "assert_qa_database_isolated", reject)
    with pytest.raises(RuntimeError, match="unsafe primary DB"):
        main_module.create_app(db_path=str(target))
    assert not target.exists()


def test_spa_fallback_404s_unknown_top_level_paths(tmp_path):
    """未知顶层路径 404：HashRouter 下 SPA 只需「/」；已知路由段书签兼容。
    扫描器探测 /wp-login.php、/.env、/blog 不再收到 200+HTML。"""
    import bzplat.backend.main as main_mod
    from fastapi.testclient import TestClient

    dist = (
        main_mod.Path(main_mod.__file__).resolve().parents[1]
        / "frontend" / "dist"
    )
    if not dist.is_dir():
        import pytest

        pytest.skip("需要已构建的 frontend/dist；行为由带 dist 的本地门禁与防漂移子集测试钉住")
    app = main_mod.create_app(db_path=str(tmp_path / "spa404.db"))
    client = TestClient(app)
    assert client.get("/wp-login.php").status_code == 404
    assert client.get("/.env").status_code == 404
    assert client.get("/blog").status_code == 404
    assert client.get("/wp-json/batch/v1").status_code == 404
    # 已知路由段（历史书签）与真实静态文件照常。
    assert client.get("/arena").status_code == 200
    assert client.get("/match/20260919-x").status_code == 200
    assert client.get("/login").status_code == 200
    assert client.get("/favicon.svg").status_code == 200
    robots = client.get("/robots.txt")
    assert robots.status_code == 200
    assert "User-agent" in robots.text


def test_spa_whitelist_covers_frontend_top_level_routes():
    """防漂移（无需 dist）：app-shell.tsx 的全部顶层路由段必须在白名单内。

    main.py 的 _SPA_PATH_TOP_LEVEL_SEGMENTS 定义在 create_app 内部，这里
    以正则从源码直接提取，保证前端新增顶层路由而忘同步时测试变红。
    """
    import bzplat.backend.main as main_mod
    import re as _re

    source = (
        main_mod.Path(main_mod.__file__).resolve().parents[2]
        / "bzplat" / "frontend" / "src" / "components" / "shell"
        / "app-shell.tsx"
    ).read_text(encoding="utf-8")
    segments = {
        m.group(1)
        for m in _re.finditer(r'path="(/[a-z0-9-]+)', source)
    }
    assert segments, "app-shell.tsx 路由提取失败（文件结构变化？）"
    text = main_mod.__file__
    main_src = main_mod.Path(text).read_text(encoding="utf-8")
    whitelist = _re.search(
        r"_SPA_PATH_TOP_LEVEL_SEGMENTS = frozenset\(\{(.*?)\}\)",
        main_src, _re.S,
    )
    assert whitelist, "白名单常量提取失败"
    allowed = set(_re.findall(r'"([a-z0-9-]+)"', whitelist.group(1)))
    missing = {s.lstrip("/") for s in segments} - allowed
    # arena 是已删除旧路由的书签兼容项，允许出现在差集中。
    assert missing <= {"arena"}, f"前端顶层路由未入白名单: {missing}"
