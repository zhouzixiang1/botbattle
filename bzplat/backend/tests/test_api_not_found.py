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


def test_api_health_still_works(tmp_path):
    """回归保护：已注册的 /api/health 正常返 JSON（fallback 不影响真实路由）。"""
    c = _client(tmp_path)
    r = c.get("/api/health")
    assert r.status_code == 200
    assert "application/json" in r.headers.get("content-type", "")
    assert r.json()["ok"] is True
