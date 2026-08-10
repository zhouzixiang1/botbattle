"""Focused contracts for communications, asynchronous mail and beginner feedback."""
from __future__ import annotations

import asyncio
import io
import json

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from bzplat.backend.auth.auth_manager import AuthManager
from bzplat.backend.communications.feedback import FeedbackService
from bzplat.backend.communications.repository import (
    CommunicationForbidden,
    CommunicationRepository,
)
from bzplat.backend.communications.service import CommunicationService
from bzplat.backend.communications.worker import DeliveryWorker
from bzplat.backend.crypto import hash_password
from bzplat.backend.main import create_app
from bzplat.backend.notifications import NotificationManager
from bzplat.backend.store import Store
from bzplat.backend.store.schema import CODE_VERIFY, ROLE_ADMIN


class _RecordingMailer:
    def __init__(self, *, configured: bool = True) -> None:
        self.config = type(
            "Config", (), {"configured": configured, "code_ttl_minutes": 30}
        )()
        self.sent: list[dict] = []

    def send(
        self,
        to_addr: str,
        subject: str,
        *,
        body_text: str,
        body_html: str,
        message_id: str,
    ) -> str:
        self.sent.append({
            "to": to_addr,
            "subject": subject,
            "body_text": body_text,
            "body_html": body_html,
            "message_id": message_id,
        })
        return message_id


def _verified_user(store: Store, username: str, *, role: str = "user") -> dict:
    user = store.create_user(
        username,
        f"{username}@example.com",
        hash_password("password12"),
        role=role,
    )
    store.update_user(user["id"], email_verified=1)
    return store.get_user(user["id"])


def _token(app, username: str) -> str:
    return app.state.auth.authenticate(username, "password12")[1]


def test_auth_code_is_queued_without_message_body_and_rendered_only_in_worker(tmp_path):
    store = Store(str(tmp_path / "auth-queue.db"))
    communications = CommunicationService(store)
    ttl_source = _RecordingMailer(configured=False)
    auth = AuthManager(store, mailer=ttl_source, communications=communications)
    user = auth.register("alice", "alice@example.com", "password12", display_name="A")

    auth.send_verify_code(user)
    code_row = store.get_latest_email_code(user["id"], CODE_VERIFY)
    assert code_row is not None
    code = code_row["code"]
    delivery = dict(store._conn.execute(
        "SELECT * FROM deliveries WHERE template_key='verify_email'"
    ).fetchone())
    assert delivery["status"] == "queued"
    assert code not in delivery["payload_json"]
    assert store._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    assert store._conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0

    mailer = _RecordingMailer()
    worker = DeliveryWorker(communications.repository, mailer)
    assert asyncio.run(worker.run_once()) == 1
    assert len(mailer.sent) == 1
    assert code in mailer.sent[0]["body_text"]
    assert mailer.sent[0]["message_id"].startswith("<")
    assert mailer.sent[0]["message_id"].endswith("@mail.botbattle.local>")
    assert store._conn.execute(
        "SELECT status FROM deliveries WHERE public_id=?", (delivery["public_id"],)
    ).fetchone()[0] == "sent"


def test_notification_truth_projection_and_thread_permissions(tmp_path):
    app = create_app(db_path=str(tmp_path / "permissions.db"))
    alice = _verified_user(app.state.store, "alice")
    bob = _verified_user(app.state.store, "bobby")
    admin = _verified_user(app.state.store, "adminuser", role=ROLE_ADMIN)
    notification = app.state.notifier.notify(
        alice["id"], type="contest", title="赛程更新", body="新一轮已发布"
    )
    assert notification["communication_message_public_id"]
    conversation_public_id = app.state.store._conn.execute(
        "SELECT c.public_id FROM conversations c JOIN messages m "
        "ON m.conversation_id=c.id WHERE m.public_id=?",
        (notification["communication_message_public_id"],),
    ).fetchone()[0]

    with TestClient(app) as client:
        alice_headers = {"Authorization": f"Bearer {_token(app, 'alice')}"}
        bob_headers = {"Authorization": f"Bearer {_token(app, 'bobby')}"}
        admin_headers = {"Authorization": f"Bearer {_token(app, 'adminuser')}"}
        own = client.get(
            f"/api/communications/threads/{conversation_public_id}",
            headers=alice_headers,
        )
        assert own.status_code == 200
        assert own.json()["conversation"]["public_id"] == conversation_public_id
        assert "id" not in own.json()["conversation"]
        assert client.get(
            f"/api/communications/threads/{conversation_public_id}",
            headers=bob_headers,
        ).status_code == 404
        assert client.get(
            f"/api/admin/communications/threads/{conversation_public_id}",
            headers=admin_headers,
        ).status_code == 200
        marked = client.post(
            f"/api/communications/threads/{conversation_public_id}/read",
            headers=alice_headers,
        )
        assert marked.status_code == 200
        assert app.state.store._conn.execute(
            "SELECT is_read FROM notifications WHERE id=?", (notification["id"],)
        ).fetchone()[0] == 1
        reply = client.post(
            f"/api/communications/threads/{conversation_public_id}/reply",
            json={"body": "我看到了"},
            headers=alice_headers,
        )
        assert reply.status_code == 200
        assert client.get(
            "/api/admin/communications/inbox", headers=admin_headers
        ).json()["total"] == 1
        preview_body = {
            "audience": {"kind": "selected_users", "usernames": ["alice"]},
            "subject": "测试",
            "body": "内容",
            "channels": ["in_app"],
        }
        assert client.post(
            "/api/admin/communications/broadcasts/preview",
            json=preview_body,
            headers=alice_headers,
        ).status_code == 403
        assert client.post(
            "/api/admin/communications/broadcasts/preview",
            json={**preview_body, "unexpected": True},
            headers=admin_headers,
        ).status_code == 422


def test_broadcast_preview_token_binds_fixed_deduplicated_snapshot(tmp_path):
    store = Store(str(tmp_path / "broadcast.db"))
    admin = _verified_user(store, "adminuser", role=ROLE_ADMIN)
    _verified_user(store, "alice")
    _verified_user(store, "bobby")
    service = CommunicationService(store)
    preview = service.preview_broadcast(
        admin_user_id=admin["id"],
        audience_kind="selected_users",
        audience_filter={"usernames": ["alice", "bobby", "alice"]},
        subject="维护通知",
        body_text="今晚维护",
        channels=["in_app"],
    )
    assert preview["audience_count"] == 2
    with pytest.raises(CommunicationForbidden):
        service.repository.approve_broadcast(
            preview["public_id"],
            actor_user_id=admin["id"],
            approval_token="wrong-token-value-that-is-long",
            scheduled_at=None,
        )
    approved = service.repository.approve_broadcast(
        preview["public_id"],
        actor_user_id=admin["id"],
        approval_token=preview["approval_token"],
        scheduled_at=None,
    )
    assert approved["audience_snapshot_hash"] == preview["audience_snapshot_hash"]
    worker = DeliveryWorker(service.repository, _RecordingMailer())
    assert asyncio.run(worker.run_once()) == 2
    asyncio.run(worker.run_once())  # no recipients left: converge running→completed
    stats = service.repository.broadcast_stats(preview["public_id"])
    assert stats["state"] == "completed"
    assert stats["recipients"] == {"delivered": 2}
    assert stats["deliveries"]["in_app:sent"] == 2
    # A crash after projecting the thread but before settling the recipient must
    # converge on the same conversation/message rather than duplicate a broadcast.
    broadcast_row = dict(store._conn.execute(
        "SELECT * FROM broadcasts WHERE public_id=?", (preview["public_id"],)
    ).fetchone())
    recipient_row = dict(store._conn.execute(
        "SELECT * FROM broadcast_recipients WHERE broadcast_id=? ORDER BY id LIMIT 1",
        (broadcast_row["id"],),
    ).fetchone())
    store._conn.execute(
        "UPDATE broadcast_recipients SET state='processing' WHERE id=?",
        (recipient_row["id"],),
    )
    store._conn.execute(
        "UPDATE broadcasts SET state='running',completed_at=NULL WHERE id=?",
        (broadcast_row["id"],),
    )
    store._conn.commit()
    service.repository.process_broadcast_recipient(broadcast_row, recipient_row)
    assert store._conn.execute(
        "SELECT COUNT(*) FROM conversations WHERE broadcast_id=?",
        (broadcast_row["id"],),
    ).fetchone()[0] == 2


def test_guest_bug_feedback_allowlist_tracking_and_image_magic(tmp_path, monkeypatch):
    monkeypatch.setenv("BZ_TEST_CAPTCHA", "1")
    monkeypatch.setenv("BZ_RATE_LIMIT", "0")
    app = create_app(db_path=str(tmp_path / "feedback.db"))
    _verified_user(app.state.store, "adminuser", role=ROLE_ADMIN)
    _verified_user(app.state.store, "alice")
    with TestClient(app) as client:
        captcha = client.get("/api/auth/captcha").json()
        payload = {
            "category": "page",
            "impact": "major",
            "title": "按钮点了没反应",
            "body": "我在赛程页点开始后没有变化",
            "current_route": "/contest/12?token=must-not-store",
            "captcha_id": captcha["captcha_id"],
            "captcha_answer": captcha["answer"],
            "diagnostics": {
                "browser_family": "chrome",
                "os_family": "windows",
                "viewport_width": 1280,
                "viewport_height": 720,
                "locale": "zh-CN",
                "timezone": "Asia/Shanghai",
                "failed_api_template": "/api/contests/*",
                "failed_api_status": 500,
                "trace_id": "trace-123",
            },
        }
        bad = dict(payload)
        bad["diagnostics"] = {**payload["diagnostics"], "cookie": "secret"}
        assert client.post("/api/feedback/bugs", json=bad).status_code == 422
        created = client.post("/api/feedback/bugs", json=payload)
        assert created.status_code == 200, created.text
        report = created.json()["bug_report"]
        assert report["public_id"].startswith("bug_")
        assert report["tracking_token"]

        bundle_raw = app.state.store._conn.execute(
            "SELECT bundle_json FROM diagnostic_bundles"
        ).fetchone()[0]
        bundle = json.loads(bundle_raw)
        assert bundle["route"] == "/contest/12"
        serialized = json.dumps(bundle).lower()
        for forbidden in ("cookie", "token", "email", "user-agent", "stderr", "hole"):
            assert forbidden not in serialized

        image = Image.new("RGB", (2, 2), color="red")
        output = io.BytesIO()
        image.save(output, format="PNG")
        attachment = client.post(
            f"/api/feedback/bugs/{report['public_id']}/attachments",
            data={"tracking_token": report["tracking_token"]},
            files={"file": ("shot.png", output.getvalue(), "image/png")},
        )
        assert attachment.status_code == 200, attachment.text
        assert attachment.json()["attachment"]["media_type"] == "image/png"
        mismatch = client.post(
            f"/api/feedback/bugs/{report['public_id']}/attachments",
            data={"tracking_token": report["tracking_token"]},
            files={"file": ("fake.jpg", output.getvalue(), "image/jpeg")},
        )
        assert mismatch.status_code == 400

        admin_headers = {"Authorization": f"Bearer {_token(app, 'adminuser')}"}
        replied = client.post(
            f"/api/admin/communications/threads/{report['conversation_public_id']}/reply",
            json={"body": "已收到，我们正在检查"},
            headers=admin_headers,
        )
        assert replied.status_code == 200
        changed = client.patch(
            f"/api/admin/bug-reports/{report['public_id']}/status",
            json={"status": "acknowledged", "note": "已收到"},
            headers=admin_headers,
        )
        assert changed.status_code == 200
        detail = client.get(
            f"/api/admin/bug-reports/{report['public_id']}", headers=admin_headers
        ).json()["bug_report"]
        assert [event["event_type"] for event in detail["events"]] == [
            "created", "attachment_added", "admin_reply", "status_changed"
        ]


def test_reopen_preserves_legacy_notifications_without_fabricating_threads(tmp_path):
    path = tmp_path / "legacy-projection.db"
    store = Store(str(path))
    user = _verified_user(store, "alice")
    legacy = store.add_notification(user["id"], title="旧通知")
    store.close()
    reopened = Store(str(path))
    try:
        row = reopened._conn.execute(
            "SELECT * FROM notifications WHERE id=?", (legacy["id"],)
        ).fetchone()
        assert row["title"] == "旧通知"
        assert row["communication_message_public_id"] is None
        assert reopened._conn.execute(
            "SELECT COUNT(*) FROM conversations"
        ).fetchone()[0] == 0
    finally:
        reopened.close()
