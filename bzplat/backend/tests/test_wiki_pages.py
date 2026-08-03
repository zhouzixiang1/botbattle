"""Wiki 页面注册完整性测试（PR-D）。

验证：
- WIKI_PAGES 注册的每个 slug 都能在 wiki/ 目录找到对应 .md 文件
- 每个 slug 经 GET /api/wiki?slug= 返回真实内容（非「暂无内容」占位）
- wiki/ 目录所有 .md 文件都被注册（无遗漏——防止新增文档忘了注册导致前端看不到）
"""
from __future__ import annotations

import pathlib

from fastapi.testclient import TestClient

from bzplat.backend.api_routes import WIKI_PAGES
from bzplat.backend.main import create_app

# tests/ → backend → bzplat → <repo_root>
_WIKI_DIR = pathlib.Path(__file__).resolve().parents[3] / "wiki"


def test_wiki_pages_registered_files_exist():
    """每个 WIKI_PAGES 条目的 file 都存在于 wiki/ 目录。"""
    assert _WIKI_DIR.is_dir(), f"wiki 目录不存在: {_WIKI_DIR}"
    for p in WIKI_PAGES:
        f = _WIKI_DIR / p["file"]
        assert f.is_file(), f"WIKI_PAGES 注册的文件不存在: {p['file']}（slug={p['slug']}）"


def test_wiki_pages_no_orphan_md_files():
    """wiki/ 目录下每个 .md 文件都应在 WIKI_PAGES 注册（防新增文档漏注册）。"""
    registered_files = {p["file"] for p in WIKI_PAGES}
    actual_files = {f.name for f in _WIKI_DIR.glob("*.md")}
    orphans = actual_files - registered_files
    assert not orphans, f"wiki/ 下有未注册的 .md 文件（前端 GET /api/wiki 看不到）: {sorted(orphans)}"


def test_every_slug_returns_real_content():
    """每个注册 slug 经 GET /api/wiki?slug= 返回真实 markdown（非占位）。"""
    app = create_app()
    c = TestClient(app)
    for p in WIKI_PAGES:
        r = c.get(f"/api/wiki?slug={p['slug']}")
        assert r.status_code == 200, f"slug={p['slug']} 返回 {r.status_code}"
        body = r.json()
        md = body.get("markdown", "")
        assert md and "暂无内容" not in md, (
            f"slug={p['slug']} 返回占位内容（文件缺失或未注册）"
        )
        assert body["slug"] == p["slug"]


def test_wiki_slug_unregistered_returns_placeholder():
    """未注册的 slug 返回占位（不崩）。"""
    app = create_app()
    c = TestClient(app)
    r = c.get("/api/wiki?slug=this-slug-does-not-exist")
    assert r.status_code == 200
    assert "暂无内容" in r.json()["markdown"]

