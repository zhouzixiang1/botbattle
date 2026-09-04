"""Focused contracts for communications, asynchronous mail and beginner feedback."""
from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path

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
        assert own.headers["cache-control"] == "private, no-store, max-age=0"
        assert own.headers["referrer-policy"] == "no-referrer"
        assert own.json()["conversation"]["public_id"] == conversation_public_id
        assert "id" not in own.json()["conversation"]
        assert client.get(
            f"/api/communications/threads/{conversation_public_id}",
            headers=bob_headers,
        ).status_code == 404
        admin_thread = client.get(
            f"/api/admin/communications/threads/{conversation_public_id}",
            headers=admin_headers,
        )
        assert admin_thread.status_code == 200
        assert admin_thread.headers["cache-control"] == "private, no-store, max-age=0"
        assert admin_thread.headers["referrer-policy"] == "no-referrer"
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


def test_broadcast_preview_token_binds_fixed_deduplicated_snapshot(
    tmp_path, monkeypatch
):
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
    original_create = service.repository.create_message_to_user
    projected_in_running_tx: list[bool] = []

    def observe_projection(*args, **kwargs):
        connection = kwargs.get("_connection")
        projected_in_running_tx.append(bool(connection and connection.in_transaction))
        return original_create(*args, **kwargs)

    monkeypatch.setattr(
        service.repository, "create_message_to_user", observe_projection
    )
    service.repository.process_broadcast_recipient(broadcast_row, recipient_row)
    assert projected_in_running_tx == [True]
    assert store._conn.execute(
        "SELECT COUNT(*) FROM conversations WHERE broadcast_id=?",
        (broadcast_row["id"],),
    ).fetchone()[0] == 2

    # A stale claimed row cannot project after cancellation; the running CAS fails
    # before the message/delivery transaction is entered.
    store._conn.execute(
        "UPDATE broadcast_recipients SET state='processing' WHERE id=?",
        (recipient_row["id"],),
    )
    store._conn.execute(
        "UPDATE broadcasts SET state='cancelled' WHERE id=?", (broadcast_row["id"],)
    )
    store._conn.commit()
    service.repository.process_broadcast_recipient(broadcast_row, recipient_row)
    assert projected_in_running_tx == [True]
    assert store._conn.execute(
        "SELECT COUNT(*) FROM conversations WHERE broadcast_id=?",
        (broadcast_row["id"],),
    ).fetchone()[0] == 2


def test_broadcast_completes_after_all_channels_terminal_and_delivery_retry_reopens(
    tmp_path,
):
    store = Store(str(tmp_path / "broadcast-terminal.db"))
    admin = _verified_user(store, "adminuser", role=ROLE_ADMIN)
    _verified_user(store, "alice")
    service = CommunicationService(store)
    preview = service.preview_broadcast(
        admin_user_id=admin["id"],
        audience_kind="selected_users",
        audience_filter={"usernames": ["alice"]},
        subject="邮件也要完成",
        body_text="双渠道通知",
        channels=["in_app", "email"],
    )
    service.repository.approve_broadcast(
        preview["public_id"],
        actor_user_id=admin["id"],
        approval_token=preview["approval_token"],
        scheduled_at=None,
    )
    batch = service.repository.claim_broadcast_batch(batch_size=1)
    assert batch is not None
    service.repository.process_broadcast_recipient(
        batch["broadcast"], batch["recipients"][0]
    )

    # Recipient projection is terminal, but queued/sending email is not.
    assert service.repository.claim_broadcast_batch(batch_size=1) is None
    assert service.repository.broadcast_stats(preview["public_id"])["state"] == "running"
    delivery = service.repository.claim_delivery()
    assert delivery is not None and delivery["status"] == "sending"
    assert service.repository.claim_broadcast_batch(batch_size=1) is None
    assert service.repository.broadcast_stats(preview["public_id"])["state"] == "running"

    service.repository.mark_delivery_sent(
        delivery["public_id"], provider_message_id="<terminal@example.test>"
    )
    assert service.repository.claim_broadcast_batch(batch_size=1) is None
    completed = service.repository.broadcast_stats(preview["public_id"])
    assert completed["state"] == "completed"
    assert completed["deliveries"] == {"email:sent": 1, "in_app:sent": 1}

    # Retrying only an email delivery must reopen completed work; cancellation then
    # cancels the queued retry and leaves already-created station mail untouched.
    store._conn.execute(
        "UPDATE deliveries SET status='failed',attempt_count=1,max_attempts=1,"
        "last_error='smtp_unavailable' WHERE public_id=?",
        (delivery["public_id"],),
    )
    store._conn.commit()
    retried = service.repository.retry_failed_broadcast_work(
        preview["public_id"],
        recipient_public_ids=[],
        delivery_public_ids=[delivery["public_id"]],
    )
    assert retried["retried_deliveries"] == [delivery["public_id"]]
    reopened = service.repository.broadcast_stats(preview["public_id"])
    assert reopened["state"] == "scheduled"
    assert reopened["completed_at"] is None
    assert reopened["deliveries"]["email:queued"] == 1
    cancelled = service.repository.cancel_broadcast(preview["public_id"])
    assert cancelled["state"] == "cancelled"
    assert service.repository.broadcast_stats(preview["public_id"])["deliveries"] == {
        "email:cancelled": 1,
        "in_app:sent": 1,
    }


def test_broadcast_cancel_blocks_pre_smtp_claim_and_cancelled_recovery(tmp_path):
    store = Store(str(tmp_path / "broadcast-cancel-mail.db"))
    admin = _verified_user(store, "adminuser", role=ROLE_ADMIN)
    _verified_user(store, "alice")
    service = CommunicationService(store)

    def claim_email(subject: str) -> tuple[dict, dict]:
        preview = service.preview_broadcast(
            admin_user_id=admin["id"],
            audience_kind="selected_users",
            audience_filter={"usernames": ["alice"]},
            subject=subject,
            body_text="取消边界",
            channels=["in_app", "email"],
        )
        service.repository.approve_broadcast(
            preview["public_id"],
            actor_user_id=admin["id"],
            approval_token=preview["approval_token"],
            scheduled_at=None,
        )
        batch = service.repository.claim_broadcast_batch(batch_size=1)
        assert batch is not None
        service.repository.process_broadcast_recipient(
            batch["broadcast"], batch["recipients"][0]
        )
        delivery = service.repository.claim_delivery()
        assert delivery is not None and delivery["status"] == "sending"
        return preview, delivery

    # Claim is not SMTP admission: cancellation committed before resolve must stop it.
    preview, delivery = claim_email("取消前尚未准入")
    service.repository.cancel_broadcast(preview["public_id"])
    mailer = _RecordingMailer()
    asyncio.run(DeliveryWorker(service.repository, mailer)._deliver(delivery))
    assert mailer.sent == []
    assert store._conn.execute(
        "SELECT status FROM deliveries WHERE public_id=?", (delivery["public_id"],)
    ).fetchone()[0] == "cancelled"

    # Startup recovery must not resurrect a cancelled broadcast's old sending claim.
    store._conn.execute(
        "UPDATE deliveries SET status='sending',cancelled_at=NULL,claimed_at='stale' "
        "WHERE public_id=?",
        (delivery["public_id"],),
    )
    store._conn.commit()
    recovered = service.repository.recover_inflight()
    assert recovered["deliveries"] == 1
    assert store._conn.execute(
        "SELECT status FROM deliveries WHERE public_id=?", (delivery["public_id"],)
    ).fetchone()[0] == "cancelled"
    assert service.repository.claim_delivery() is None

    # Once resolve's write-locked parent CAS commits, SMTP is the explicit external
    # boundary: a later cancel cannot recall a provider-accepted message.
    admitted_preview, admitted_delivery = claim_email("已通过准入边界")
    content = service.repository.resolve_delivery_content(admitted_delivery)
    assert content is not None
    service.repository.cancel_broadcast(admitted_preview["public_id"])
    subject, body_text, body_html = content
    provider_id = mailer.send(
        admitted_delivery["address_snapshot"],
        subject,
        body_text=body_text,
        body_html=body_html,
        message_id="<admitted@example.test>",
    )
    service.repository.mark_delivery_sent(
        admitted_delivery["public_id"], provider_message_id=provider_id
    )
    assert len(mailer.sent) == 1
    assert store._conn.execute(
        "SELECT status FROM deliveries WHERE public_id=?",
        (admitted_delivery["public_id"],),
    ).fetchone()[0] == "sent"


def test_guest_bug_feedback_allowlist_tracking_and_image_magic(tmp_path, monkeypatch):
    monkeypatch.setenv("BZ_TEST_CAPTCHA", "1")
    monkeypatch.setenv("BZ_RATE_LIMIT", "0")
    app = create_app(db_path=str(tmp_path / "feedback.db"))
    _verified_user(app.state.store, "adminuser", role=ROLE_ADMIN)
    alice = _verified_user(app.state.store, "alice")
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
                "theme": "dark",
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
        assert created.headers["cache-control"].startswith("no-store")
        assert created.headers["referrer-policy"] == "no-referrer"
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
        alice_headers = {"Authorization": f"Bearer {_token(app, alice['username'])}"}
        denied_uploads = [
            client.post(
                f"/api/feedback/bugs/{report['public_id']}/attachments",
                files={"file": ("shot.png", output.getvalue(), "image/png")},
            ),
            client.post(
                f"/api/feedback/bugs/{report['public_id']}/attachments",
                data={"tracking_token": "wrong-token"},
                files={"file": ("shot.png", output.getvalue(), "image/png")},
            ),
            client.post(
                "/api/feedback/bugs/bug_unknown/attachments",
                data={"tracking_token": report["tracking_token"]},
                files={"file": ("shot.png", output.getvalue(), "image/png")},
            ),
            client.post(
                f"/api/feedback/bugs/{report['public_id']}/attachments",
                data={"tracking_token": report["tracking_token"]},
                files={"file": ("shot.png", output.getvalue(), "image/png")},
                headers=alice_headers,
            ),
        ]
        assert {item.status_code for item in denied_uploads} == {404}
        assert {item.json()["detail"] for item in denied_uploads} == {
            "Bug 反馈不存在"
        }
        assert app.state.store._conn.execute(
            "SELECT COUNT(*) FROM bug_attachments"
        ).fetchone()[0] == 0
        attachment = client.post(
            f"/api/feedback/bugs/{report['public_id']}/attachments",
            data={"tracking_token": report["tracking_token"]},
            files={"file": ("shot.png", output.getvalue(), "image/png")},
        )
        assert attachment.status_code == 200, attachment.text
        assert attachment.json()["attachment"]["media_type"] == "image/png"
        attachment_public_id = attachment.json()["attachment"]["public_id"]
        assert app.state.store._conn.execute(
            "SELECT uploaded_by_user_id FROM bug_attachments WHERE public_id=?",
            (attachment_public_id,),
        ).fetchone()[0] is None
        tracked = client.get(
            f"/api/feedback/bugs/{report['public_id']}/track",
            headers={"X-Feedback-Token": report["tracking_token"]},
        )
        assert tracked.status_code == 200
        assert tracked.headers["cache-control"].startswith("no-store")
        assert tracked.json()["bug_report"]["diagnostic"]["bundle"]["client"]["theme"] == "dark"
        assert client.get(
            f"/api/feedback/bugs/{report['public_id']}/track",
            headers={"X-Feedback-Token": "wrong-token"},
        ).status_code == 404
        guest_reply = client.post(
            f"/api/feedback/bugs/{report['public_id']}/track/reply",
            json={"body": "我又试了一次，仍然没有变化"},
            headers={"X-Feedback-Token": report["tracking_token"]},
        )
        assert guest_reply.status_code == 200
        downloaded = client.get(
            f"/api/feedback/bugs/{report['public_id']}/attachments/{attachment_public_id}",
            headers={"X-Feedback-Token": report["tracking_token"]},
        )
        assert downloaded.status_code == 200
        assert downloaded.content == output.getvalue()
        assert downloaded.headers["cache-control"].startswith("private, no-store")
        assert client.get(
            f"/api/feedback/bugs/{report['public_id']}/attachments/{attachment_public_id}",
            headers={"X-Feedback-Token": "wrong-token"},
        ).status_code == 404
        mismatch = client.post(
            f"/api/feedback/bugs/{report['public_id']}/attachments",
            data={"tracking_token": report["tracking_token"]},
            files={"file": ("fake.jpg", output.getvalue(), "image/jpeg")},
        )
        assert mismatch.status_code == 400

        owned_payload = {
            key: value
            for key, value in payload.items()
            if key not in {"captcha_id", "captcha_answer"}
        }
        owned_created = client.post(
            "/api/feedback/bugs", json=owned_payload, headers=alice_headers
        )
        assert owned_created.status_code == 200
        owned_detail = client.get(
            f"/api/feedback/bugs/{owned_created.json()['bug_report']['public_id']}",
            headers=alice_headers,
        )
        assert owned_detail.status_code == 200
        assert owned_detail.headers["cache-control"] == "private, no-store, max-age=0"
        assert owned_detail.headers["referrer-policy"] == "no-referrer"
        owned_list = client.get("/api/feedback/bugs", headers=alice_headers)
        assert owned_list.status_code == 200
        assert owned_list.headers["cache-control"] == "private, no-store, max-age=0"

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
            "created", "attachment_added", "reporter_reply", "admin_reply", "status_changed"
        ]


def test_feedback_frontend_guards_writes_across_identity_changes():
    frontend_root = Path(__file__).resolve().parents[2] / "frontend" / "src"
    source = (frontend_root / "pages" / "Feedback.tsx").read_text(encoding="utf-8")
    messages_source = (frontend_root / "pages" / "Messages.tsx").read_text(
        encoding="utf-8"
    )
    admin_source = (frontend_root / "pages" / "admin" / "EmailTab.tsx").read_text(
        encoding="utf-8"
    )
    api_source = (frontend_root / "api.ts").read_text(encoding="utf-8")

    assert "<FeedbackForIdentity key={user?.id ?? 'guest'} user={user} />" in source
    assert source.count(
        "frozenAuthRequestOptions(controller.signal, identity.userId)"
    ) >= 2
    assert "credentials: userId === null ? 'omit' : 'include'" in source
    assert "identityEpochRef" in source
    assert "operationControllersRef" in source
    assert "abortIdentityOperations()" in source
    assert source.count("operationIsCurrent(operation)") >= 12
    assert "selectReport(created.public_id, operation.epoch)" in source
    assert "selectReport(reportPublicId, operation.epoch)" in source
    download_source = source.split("const downloadAttachment = async", 1)[1].split(
        "const addAttachments = async", 1
    )[0]
    assert "const operation = beginIdentityOperation()" in download_source
    assert "signal: operation.controller.signal" in download_source
    assert download_source.count("operationIsCurrent(operation)") >= 3
    assert "operation.userId" in download_source
    assert "operation.authToken" not in source
    assert "headers.set('Authorization'" not in source
    assert "userToken" not in source
    assert "finishIdentityOperation(operation)" in download_source
    assert "suppressAuth: true" in source
    assert "credentials: 'omit'" in source
    assert "cache: 'no-store'" in source
    assert "referrerPolicy: 'no-referrer'" in source
    assert "suppressAuth?: boolean" in api_source
    assert "const soft = suppressAuth ||" in api_source
    assert "currentUserMemory" in api_source
    assert "currentIdentityGeneration" in api_source
    assert "currentIdentitySnapshot()" in api_source
    assert "sentIdentityIsCurrent(" in api_source
    assert "localStorage.removeItem(legacyKey)" in api_source
    assert "export const userToken" not in api_source
    assert "headers.set('Authorization'" not in api_source

    assert "<MessagesForIdentity key={user?.id ?? 'guest'} user={user} />" in messages_source
    assert "listRequestRef.current.controller?.abort()" in messages_source
    assert "sendRequestRef.current.controller?.abort()" in messages_source
    assert "requestOptions(controller.signal)" in messages_source
    assert "credentials: 'include'" in messages_source
    assert "cache: 'no-store'" in messages_source
    assert "referrerPolicy: 'no-referrer'" in messages_source

    assert "attachmentControllersRef" in admin_source
    assert "cancelAttachmentRequests()" in admin_source
    assert "signal: controller.signal" in admin_source
    assert "credentials: 'include'" in admin_source
    assert "cache: 'no-store'" in admin_source
    assert "referrerPolicy: 'no-referrer'" in admin_source
    assert "!selectionMatches('bug', bugPublicId)" in admin_source


def test_admin_broadcast_history_detail_and_bounded_failed_retry(tmp_path):
    app = create_app(db_path=str(tmp_path / "broadcast-admin.db"))
    admin = _verified_user(app.state.store, "adminuser", role=ROLE_ADMIN)
    alice = _verified_user(app.state.store, "alice")
    preview = app.state.communications.preview_broadcast(
        admin_user_id=admin["id"],
        audience_kind="selected_users",
        audience_filter={"usernames": ["alice"]},
        subject="维护提醒",
        body_text="今晚维护，请提前保存进度。",
        channels=["in_app", "email"],
    )
    approved = app.state.communications.repository.approve_broadcast(
        preview["public_id"],
        actor_user_id=admin["id"],
        approval_token=preview["approval_token"],
        scheduled_at=None,
    )
    batch = app.state.communications.repository.claim_broadcast_batch(batch_size=1)
    assert batch is not None
    broadcast_row = batch["broadcast"]
    recipient_row = batch["recipients"][0]
    app.state.communications.repository.process_broadcast_recipient(
        broadcast_row, recipient_row
    )
    delivery_rows = app.state.store._conn.execute(
        "SELECT public_id FROM deliveries WHERE broadcast_id=? ORDER BY id",
        (broadcast_row["id"],),
    ).fetchall()
    assert len(delivery_rows) == 2
    recipient_public_id = recipient_row["public_id"]
    delivery_public_id = delivery_rows[-1]["public_id"]
    app.state.store._conn.execute(
        "UPDATE broadcast_recipients SET state='failed',attempt_count=1,max_attempts=1,"
        "last_error='recipient_projection_failed' WHERE public_id=?",
        (recipient_public_id,),
    )
    app.state.store._conn.execute(
        "UPDATE deliveries SET status='failed',attempt_count=1,max_attempts=1,"
        "last_error='smtp_unavailable' WHERE public_id=?",
        (delivery_public_id,),
    )
    app.state.store._conn.execute(
        "UPDATE broadcasts SET state='completed' WHERE id=?", (broadcast_row["id"],)
    )
    app.state.store._conn.commit()

    client = TestClient(app)
    headers = {"Authorization": f"Bearer {_token(app, 'adminuser')}"}
    listing = client.get(
        "/api/admin/communications/broadcasts?per_page=10", headers=headers
    )
    assert listing.status_code == 200
    item = listing.json()["broadcasts"][0]
    assert item["public_id"] == approved["public_id"]
    assert item["failed_recipient_count"] == 1
    assert item["failed_delivery_count"] == 1
    assert "id" not in item and "address_snapshot" not in item

    detail_response = client.get(
        f"/api/admin/communications/broadcasts/{approved['public_id']}",
        headers=headers,
    )
    assert detail_response.status_code == 200
    detail = detail_response.json()["broadcast"]
    assert detail["failed_recipients"][0]["public_id"] == recipient_public_id
    assert any(
        delivery["public_id"] == delivery_public_id
        for delivery in detail["failed_deliveries"]
    )
    serialized = json.dumps(detail).lower()
    assert "alice@example.com" not in serialized
    assert "address_snapshot" not in serialized

    retried = client.post(
        f"/api/admin/communications/broadcasts/{approved['public_id']}/retry-failed",
        json={
            "recipient_public_ids": [recipient_public_id],
            "delivery_public_ids": [delivery_public_id],
        },
        headers=headers,
    )
    assert retried.status_code == 200, retried.text
    assert retried.json()["retry"]["retried_recipients"] == [recipient_public_id]
    assert retried.json()["retry"]["retried_deliveries"] == [delivery_public_id]
    states = app.state.store._conn.execute(
        "SELECT "
        "(SELECT state FROM broadcast_recipients WHERE public_id=?),"
        "(SELECT status FROM deliveries WHERE public_id=?),"
        "(SELECT state FROM broadcasts WHERE id=?)",
        (recipient_public_id, delivery_public_id, broadcast_row["id"]),
    ).fetchone()
    assert tuple(states) == ("pending", "queued", "scheduled")
    repeated = client.post(
        f"/api/admin/communications/broadcasts/{approved['public_id']}/retry-failed",
        json={
            "recipient_public_ids": [recipient_public_id],
            "delivery_public_ids": [delivery_public_id],
        },
        headers=headers,
    )
    assert repeated.status_code == 200
    assert set(repeated.json()["retry"]["ignored"]) == {
        recipient_public_id, delivery_public_id,
    }
    assert app.state.store._conn.execute(
        "SELECT COUNT(*) FROM users WHERE id=?", (alice["id"],)
    ).fetchone()[0] == 1


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
