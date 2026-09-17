"""用户云存储：清单/配额/内容寻址 blob 与 API 门禁。"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bzplat.backend.crypto import hash_password
from bzplat.backend.main import create_app
from bzplat.backend.runtime import limits as limits_module
from bzplat.backend.store import Store


def _app(tmp_path, monkeypatch=None):
    app = create_app(db_path=str(tmp_path / "storage.db"))
    store = app.state.store
    user = store.create_user(
        "storuser", "storuser@e.com", hash_password("pw123456")
    )
    store.update_user(user["id"], email_verified=1)
    other = store.create_user(
        "storother", "storother@e.com", hash_password("pw123456")
    )
    store.update_user(other["id"], email_verified=1)
    _, token = app.state.auth.authenticate("storuser", "pw123456")
    _, other_token = app.state.auth.authenticate("storother", "pw123456")
    return app, store, user, other, token, other_token


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _blob_root(app) -> Path:
    return app.state.user_storage.root


def test_upload_list_replace_delete_roundtrip(tmp_path):
    app, store, user, _other, token, _ = _app(tmp_path)
    client = TestClient(app)
    root = _blob_root(app)

    response = client.post(
        "/api/storage/files",
        headers=_auth(token),
        files={"file": ("weights.bin", b"AAAA", "application/octet-stream")},
    )
    assert response.status_code == 200, response.text
    body = response.json()["file"]
    assert body["name"] == "weights.bin"
    assert body["size_bytes"] == 4
    sha_a = hashlib.sha256(b"AAAA").hexdigest()
    assert body["sha256"] == sha_a
    blob = root / str(user["id"]) / sha_a
    assert blob.is_file()

    listing = client.get("/api/storage/files", headers=_auth(token)).json()
    assert [f["name"] for f in listing["files"]] == ["weights.bin"]
    assert listing["usage"] == {"files": 1, "bytes": 4}
    assert listing["quota"]["bytes"] == limits_module.USER_STORAGE_QUOTA_BYTES
    assert listing["quota"]["max_files"] == limits_module.USER_STORAGE_MAX_FILES

    # 同名替换：旧内容 blob 在延迟清扫后回收（立即 unlink 与并发同内容
    # 上传存在交错窗口，实体回收统一走 sweep）。
    replaced = client.post(
        "/api/storage/files",
        headers=_auth(token),
        files={"file": ("weights.bin", b"BBBBBB", "application/octet-stream")},
    )
    assert replaced.status_code == 200
    sha_b = hashlib.sha256(b"BBBBBB").hexdigest()
    assert (root / str(user["id"]) / sha_a).is_file()
    app.state.user_storage.sweep_unreferenced(min_age_seconds=0)
    assert (root / str(user["id"]) / sha_a).exists() is False
    assert (root / str(user["id"]) / sha_b).is_file()
    assert store.user_storage_usage(user["id"]) == {"files": 1, "bytes": 6}

    removed = client.delete(
        f"/api/storage/files/weights.bin", headers=_auth(token)
    )
    assert removed.status_code == 200
    # 删除即时效果只体现在清单与配额；实体待清扫。
    assert (root / str(user["id"]) / sha_b).is_file()
    assert store.user_storage_usage(user["id"]) == {"files": 0, "bytes": 0}
    app.state.user_storage.sweep_unreferenced(min_age_seconds=0)
    assert (root / str(user["id"]) / sha_b).exists() is False
    assert client.get("/api/storage/files", headers=_auth(token)).json()[
        "usage"
    ] == {"files": 0, "bytes": 0}
    assert (
        client.delete(
            f"/api/storage/files/weights.bin", headers=_auth(token)
        ).status_code
        == 404
    )


def test_same_content_two_names_keeps_single_blob(tmp_path):
    app, store, user, _other, token, _ = _app(tmp_path)
    client = TestClient(app)
    root = _blob_root(app)
    sha = hashlib.sha256(b"same").hexdigest()

    for name in ("one.bin", "two.bin"):
        assert (
            client.post(
                "/api/storage/files",
                headers=_auth(token),
                files={"file": (name, b"same", "application/octet-stream")},
            ).status_code
            == 200
        )
    assert (root / str(user["id"]) / sha).is_file()
    assert store.user_storage_usage(user["id"]) == {"files": 2, "bytes": 8}

    assert (
        client.delete("/api/storage/files/one.bin", headers=_auth(token)).status_code
        == 200
    )
    # 仍被 two.bin 引用：即使零宽限清扫也必须保留。
    app.state.user_storage.sweep_unreferenced(min_age_seconds=0)
    assert (root / str(user["id"]) / sha).is_file()
    assert (
        client.delete("/api/storage/files/two.bin", headers=_auth(token)).status_code
        == 200
    )
    app.state.user_storage.sweep_unreferenced(min_age_seconds=0)
    assert (root / str(user["id"]) / sha).exists() is False


def test_user_isolation_and_auth_gates(tmp_path):
    app, _store, _user, _other, token, other_token = _app(tmp_path)
    client = TestClient(app)
    assert (
        client.post(
            "/api/storage/files",
            files={"file": ("x.bin", b"data", "application/octet-stream")},
        ).status_code
        == 401
    )
    assert (
        client.get("/api/storage/files").status_code
        == 401
    )
    assert (
        client.post(
            "/api/storage/files",
            headers=_auth(token),
            files={"file": ("secret.bin", b"mine", "application/octet-stream")},
        ).status_code
        == 200
    )
    # 其他用户不可见、不可删。
    assert client.get("/api/storage/files", headers=_auth(other_token)).json()[
        "files"
    ] == []
    assert (
        client.delete(
            "/api/storage/files/secret.bin", headers=_auth(other_token)
        ).status_code
        == 404
    )


def test_name_validation_and_client_path_stripping(tmp_path):
    app, _store, _user, _other, token, _ = _app(tmp_path)
    client = TestClient(app)
    # multipart 文件名按客户端本地路径剥离目录段（'../escape.bin' → 'escape.bin'），
    # 显式 name 字段才按原样校验；文件名永不进入文件系统路径（blob 按内容寻址）。
    for raw_name, kept in (("../escape.bin", "escape.bin"), ("a/b.bin", "b.bin")):
        response = client.post(
            "/api/storage/files",
            headers=_auth(token),
            files={"file": (raw_name, b"zz", "application/octet-stream")},
        )
        assert response.status_code == 200, raw_name
        assert response.json()["file"]["name"] == kept, raw_name
    for raw_name in (".", "x" * 201 + ".bin"):
        response = client.post(
            "/api/storage/files",
            headers=_auth(token),
            files={"file": (raw_name, b"zz", "application/octet-stream")},
        )
        assert response.status_code == 400, raw_name
        assert response.json()["detail"]["code"] == "invalid_name", raw_name
    # Windows 客户端路径取末段。
    stripped = client.post(
        "/api/storage/files",
        headers=_auth(token),
        files={
            "file": ("C:\\Users\\me\\model.pt", b"pp", "application/octet-stream")
        },
    )
    assert stripped.status_code == 200
    assert stripped.json()["file"]["name"] == "model.pt"
    # 显式 name 字段覆盖文件名且不做路径剥离。
    override = client.post(
        "/api/storage/files",
        headers=_auth(token),
        data={"name": "alias.bin"},
        files={"file": ("whatever.bin", b"qq", "application/octet-stream")},
    )
    assert override.status_code == 200
    assert override.json()["file"]["name"] == "alias.bin"
    empty_override = client.post(
        "/api/storage/files",
        headers=_auth(token),
        data={"name": "  "},
        files={"file": ("whatever.bin", b"qq", "application/octet-stream")},
    )
    assert empty_override.status_code == 400
    assert empty_override.json()["detail"]["code"] == "invalid_name"
    bad_override = client.post(
        "/api/storage/files",
        headers=_auth(token),
        data={"name": "../bad"},
        files={"file": ("whatever.bin", b"qq", "application/octet-stream")},
    )
    assert bad_override.status_code == 400
    assert bad_override.json()["detail"]["code"] == "invalid_name"


def test_quota_enforced_in_write_transaction(tmp_path, monkeypatch):
    app, _store, _user, _other, token, _ = _app(tmp_path)
    client = TestClient(app)
    monkeypatch.setattr(limits_module, "USER_STORAGE_QUOTA_BYTES", 8)
    assert (
        client.post(
            "/api/storage/files",
            headers=_auth(token),
            files={"file": ("a.bin", b"12345678", "application/octet-stream")},
        ).status_code
        == 200
    )
    exceeded = client.post(
        "/api/storage/files",
        headers=_auth(token),
        files={"file": ("b.bin", b"123456789", "application/octet-stream")},
    )
    assert exceeded.status_code == 400
    assert exceeded.json()["detail"]["code"] == "storage_quota_exceeded"
    # 覆盖同名不放大占用：等大小替换仍成功。
    assert (
        client.post(
            "/api/storage/files",
            headers=_auth(token),
            files={"file": ("a.bin", b"87654321", "application/octet-stream")},
        ).status_code
        == 200
    )
    # monkeypatch 只收紧 Store 侧配额；传输层仍按真实单文件上限放行，
    # 由写事务以配额理由拒绝（生产里单文件 >256MiB 会在流中被 invalid_size 拒绝）。
    oversize = client.post(
        "/api/storage/files",
        headers=_auth(token),
        files={"file": ("c.bin", b"x" * 9, "application/octet-stream")},
    )
    assert oversize.status_code == 400
    assert oversize.json()["detail"]["code"] == "storage_quota_exceeded"


def test_declared_oversized_body_rejected_before_parse(tmp_path):
    app, *_rest, token = _app(tmp_path)
    client = TestClient(app)
    response = client.post(
        "/api/storage/files",
        headers={
            **_auth(token),
            "Content-Length": str(
                limits_module.USER_STORAGE_QUOTA_BYTES + 2 * 1024 * 1024
            ),
        },
    )
    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "storage_body_too_large"


def test_delete_invalid_name_returns_400_not_500(tmp_path):
    app, _store, _user, _other, token, _ = _app(tmp_path)
    client = TestClient(app)
    for bad in ("%01x.bin", "x" * 201):
        response = client.delete(
            f"/api/storage/files/{bad}", headers=_auth(token)
        )
        assert response.status_code == 400, bad
        assert response.json()["detail"]["code"] == "invalid_name", bad


def test_sweep_respects_grace_period_and_staging(tmp_path):
    app, store, user, _other, token, _ = _app(tmp_path)
    client = TestClient(app)
    root = _blob_root(app)
    assert (
        client.post(
            "/api/storage/files",
            headers=_auth(token),
            files={"file": ("keep.bin", b"data", "application/octet-stream")},
        ).status_code
        == 200
    )
    # 宽限期内（默认 1h）：无引用的孤儿也保留——在途事务安全窗。
    stale_dir = root / f".incoming-crash-{user['id']}"
    stale_dir.mkdir(parents=True)
    (stale_dir / "payload.bin").write_bytes(b"crash")
    orphan = root / str(user["id"]) / ("f" * 64)
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"orphan")
    result = app.state.user_storage.sweep_unreferenced()
    assert result == {"blobs": 0, "staging": 0, "bytes": 0}
    assert orphan.is_file() and stale_dir.is_dir()
    # 零宽限：孤儿与暂存被清，清单引用的实体保留。
    result = app.state.user_storage.sweep_unreferenced(min_age_seconds=0)
    assert result["blobs"] == 1 and result["staging"] == 1
    assert not orphan.exists() and not stale_dir.exists()
    sha = hashlib.sha256(b"data").hexdigest()
    assert (root / str(user["id"]) / sha).is_file()


def test_replace_reject_leaves_blob_for_sweep_not_inline(tmp_path, monkeypatch):
    app, store, user, _other, token, _ = _app(tmp_path)
    client = TestClient(app)
    root = _blob_root(app)
    monkeypatch.setattr(limits_module, "USER_STORAGE_QUOTA_BYTES", 4)
    assert (
        client.post(
            "/api/storage/files",
            headers=_auth(token),
            files={"file": ("a.bin", b"1234", "application/octet-stream")},
        ).status_code
        == 200
    )
    rejected = client.post(
        "/api/storage/files",
        headers=_auth(token),
        files={"file": ("b.bin", b"123456789", "application/octet-stream")},
    )
    assert rejected.status_code == 400
    sha_b = hashlib.sha256(b"123456789").hexdigest()
    # 拒绝路径晋升的实体留给延迟清扫（不立即回收，避免与并发同内容
    # 上传的交错窗口）；清单不受影响。
    assert (root / str(user["id"]) / sha_b).is_file()
    assert store.user_storage_usage(user["id"]) == {"files": 1, "bytes": 4}
    app.state.user_storage.sweep_unreferenced(min_age_seconds=0)
    assert (root / str(user["id"]) / sha_b).exists() is False


def test_storage_table_created_and_reopen_idempotent(tmp_path):
    app, store, *_ = _app(tmp_path)
    with store._tx() as c:
        exists = c.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
            "AND name='user_storage_files'"
        ).fetchone()[0]
        index = c.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='index' "
            "AND name='idx_user_storage_files_user'"
        ).fetchone()[0]
    assert exists == 1 and index == 1
    # 二次打开幂等。
    store.close()
    reopened = Store(str(tmp_path / "storage.db"))
    reopened.close()


def test_repromote_refreshes_mtime_for_grace_window(tmp_path):
    """删除后重新上传同内容：promote 必须刷新 mtime（宽限期证据）。"""
    import os
    import time

    app, _store, user, _other, token, _ = _app(tmp_path)
    client = TestClient(app)
    root = _blob_root(app)
    assert (
        client.post(
            "/api/storage/files",
            headers=_auth(token),
            files={"file": ("w.bin", b"AAAA", "application/octet-stream")},
        ).status_code
        == 200
    )
    sha = hashlib.sha256(b"AAAA").hexdigest()
    blob = root / str(user["id"]) / sha
    # 模拟实体是历史遗留（mtime 早于两倍宽限期）。
    old = time.time() - 7200.0
    os.utime(blob, (old, old))
    assert blob.stat().st_mtime < time.time() - 3600.0
    # 重新上传同内容：无条件 replace 必须把 mtime 拉回当下。
    assert (
        client.post(
            "/api/storage/files",
            headers=_auth(token),
            files={"file": ("w2.bin", b"AAAA", "application/octet-stream")},
        ).status_code
        == 200
    )
    assert blob.stat().st_mtime >= time.time() - 3600.0


def test_sweep_rechecks_references_in_race_window(tmp_path, monkeypatch):
    """快照读与 unlink 之间刚获得引用的实体不得被回收（复核防线）。"""
    import os
    import time

    app, store, user, _other, token, _ = _app(tmp_path)
    client = TestClient(app)
    root = _blob_root(app)
    assert (
        client.post(
            "/api/storage/files",
            headers=_auth(token),
            files={"file": ("w.bin", b"AAAA", "application/octet-stream")},
        ).status_code
        == 200
    )
    sha = hashlib.sha256(b"AAAA").hexdigest()
    blob = root / str(user["id"]) / sha
    old = time.time() - 7200.0
    os.utime(blob, (old, old))
    # 清单行确实存在（等价于“快照之后、unlink 之前并发上传已提交”），
    # 但让快照读返回陈旧的空视图，模拟竞态窗口。
    monkeypatch.setattr(store, "all_user_storage_shas", lambda: {})
    result = app.state.user_storage.sweep_unreferenced(min_age_seconds=3600.0)
    assert result["blobs"] == 0
    assert blob.is_file(), "unlink 前复核必须救回刚获得引用的实体"
    monkeypatch.undo()
    # 快照恢复一致后（仍超宽限期、无引用），可正常回收。
    assert client.delete("/api/storage/files/w.bin", headers=_auth(token)).status_code == 200
    result = app.state.user_storage.sweep_unreferenced(min_age_seconds=0)
    assert result["blobs"] == 1
    assert not blob.exists()


def test_lifespan_starts_periodic_storage_sweep(tmp_path):
    """周期回收任务随 lifespan 启动并在关停时取消。"""
    app, *_ = _app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/api/site/info").status_code == 200
        task = app.state._user_storage_sweep_task
        assert task is not None and not task.done()
        assert task.get_name() == "user-storage-sweep"
    assert app.state._user_storage_sweep_task.done()
