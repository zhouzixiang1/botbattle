"""SQLite repository for communications.

The repository owns every multi-table transaction in the communication domain.  It
never returns an internal primary key from a public projection.
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta
from typing import Any, Iterable

from bzplat.backend.store import Store

from .templates import get_template
from .utils import (
    canonical_json,
    content_hash,
    now_iso,
    plain_to_safe_html,
    public_id,
    token_hash,
)


class CommunicationNotFound(LookupError):
    pass


class CommunicationForbidden(PermissionError):
    pass


class CommunicationConflict(RuntimeError):
    pass


def _dict(row: Any) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _json(raw: str | None, default: Any) -> Any:
    try:
        return json.loads(raw or "")
    except (TypeError, json.JSONDecodeError):
        return default


class CommunicationRepository:
    def __init__(self, store: Store) -> None:
        self.store = store

    # ── ordinary conversation/message truth ──────────────────────────

    def create_message_to_user(
        self,
        user_id: int,
        *,
        kind: str,
        subject: str,
        body_text: str,
        author_kind: str = "platform",
        author_user_id: int | None = None,
        metadata: dict[str, Any] | None = None,
        queue_email: bool = False,
        email_priority: int = 50,
        legacy_notification: dict[str, str] | None = None,
        broadcast_id: int | None = None,
        idempotency_prefix: str | None = None,
    ) -> dict[str, Any] | None:
        """Atomically create a one-user thread, message and channel deliveries."""
        created = now_iso()
        conversation_pid = public_id("cnv")
        message_pid = public_id("msg")
        metadata_json = canonical_json(metadata or {})
        with self.store._tx() as conn:  # domain transaction boundary
            user = conn.execute(
                "SELECT id, username, email, is_active FROM users WHERE id=?",
                (user_id,),
            ).fetchone()
            if user is None:
                return None
            if idempotency_prefix:
                existing = conn.execute(
                    "SELECT c.public_id AS conversation_public_id,"
                    "m.public_id AS message_public_id FROM deliveries d "
                    "JOIN messages m ON m.id=d.message_id "
                    "JOIN conversations c ON c.id=m.conversation_id "
                    "WHERE d.idempotency_key=?",
                    (f"{idempotency_prefix}:in_app:{user_id}",),
                ).fetchone()
                if existing:
                    projection = conn.execute(
                        "SELECT * FROM notifications "
                        "WHERE communication_message_public_id=?",
                        (existing["message_public_id"],),
                    ).fetchone()
                    return {
                        "conversation_public_id": existing["conversation_public_id"],
                        "message_public_id": existing["message_public_id"],
                        "legacy_notification": _dict(projection),
                    }
            conn.execute(
                "INSERT INTO conversations(public_id,kind,subject,status,"
                "created_by_user_id,created_by_kind,broadcast_id,created_at,updated_at) "
                "VALUES(?,?,?,'open',?,?,?,?,?)",
                (
                    conversation_pid,
                    kind,
                    subject,
                    author_user_id,
                    author_kind,
                    broadcast_id,
                    created,
                    created,
                ),
            )
            conversation_id = int(conn.execute(
                "SELECT id FROM conversations WHERE public_id=?",
                (conversation_pid,),
            ).fetchone()[0])
            conn.executemany(
                "INSERT INTO conversation_participants(public_id,conversation_id,"
                "user_id,participant_kind,joined_at) VALUES(?,?,?,?,?)",
                (
                    (public_id("cpt"), conversation_id, user_id, "user", created),
                    (public_id("cpt"), conversation_id, None, "platform", created),
                ),
            )
            conn.execute(
                "INSERT INTO messages(public_id,conversation_id,reply_to_id,"
                "author_user_id,author_kind,body_text,sanitized_html,metadata_json,created_at) "
                "VALUES(?,?,NULL,?,?,?,?,?,?)",
                (
                    message_pid,
                    conversation_id,
                    author_user_id,
                    author_kind,
                    body_text,
                    plain_to_safe_html(body_text),
                    metadata_json,
                    created,
                ),
            )
            message_id = int(conn.execute(
                "SELECT id FROM messages WHERE public_id=?", (message_pid,)
            ).fetchone()[0])
            prefix = idempotency_prefix or f"message:{message_pid}"
            self._insert_delivery(
                conn,
                message_id=message_id,
                broadcast_id=broadcast_id,
                channel="in_app",
                recipient_user_id=user_id,
                address_snapshot=str(user["username"]),
                status="sent",
                priority=0,
                idempotency_key=f"{prefix}:in_app:{user_id}",
                created_at=created,
                sent_at=created,
            )
            if queue_email and user["email"]:
                self._insert_delivery(
                    conn,
                    message_id=message_id,
                    broadcast_id=broadcast_id,
                    channel="email",
                    recipient_user_id=user_id,
                    address_snapshot=str(user["email"]),
                    status="queued",
                    priority=email_priority,
                    idempotency_key=f"{prefix}:email:{user_id}",
                    created_at=created,
                )
            projection = None
            if legacy_notification is not None:
                cur = conn.execute(
                    "INSERT INTO notifications(user_id,type,title,body,link,is_read,"
                    "communication_message_public_id,created_at) VALUES(?,?,?,?,?,0,?,?)",
                    (
                        user_id,
                        legacy_notification.get("type", ""),
                        legacy_notification.get("title", subject),
                        legacy_notification.get("body", body_text),
                        legacy_notification.get("link", ""),
                        message_pid,
                        created,
                    ),
                )
                projection = _dict(conn.execute(
                    "SELECT * FROM notifications WHERE id=?", (cur.lastrowid,)
                ).fetchone())
            return {
                "conversation_public_id": conversation_pid,
                "message_public_id": message_pid,
                "legacy_notification": projection,
            }

    def append_message(
        self,
        conversation_public_id: str,
        *,
        actor_user_id: int | None,
        actor_kind: str,
        body_text: str,
        reply_to_public_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        queue_email: bool = False,
    ) -> dict[str, Any]:
        created = now_iso()
        message_pid = public_id("msg")
        with self.store._tx() as conn:
            conversation = conn.execute(
                "SELECT * FROM conversations WHERE public_id=?",
                (conversation_public_id,),
            ).fetchone()
            if conversation is None:
                raise CommunicationNotFound("会话不存在")
            if conversation["status"] != "open":
                raise CommunicationConflict("会话已关闭，不能继续回复")
            if conversation["kind"] == "auth":
                raise CommunicationForbidden("事务认证邮件不可回复")
            if actor_kind == "user":
                allowed = conn.execute(
                    "SELECT 1 FROM conversation_participants "
                    "WHERE conversation_id=? AND user_id=? AND participant_kind='user'",
                    (conversation["id"], actor_user_id),
                ).fetchone()
                if allowed is None:
                    raise CommunicationForbidden("无权访问该会话")
            reply_to_id = None
            if reply_to_public_id:
                reply = conn.execute(
                    "SELECT id FROM messages WHERE public_id=? AND conversation_id=?",
                    (reply_to_public_id, conversation["id"]),
                ).fetchone()
                if reply is None:
                    raise CommunicationConflict("reply_to 不属于当前会话")
                reply_to_id = int(reply["id"])
            conn.execute(
                "INSERT INTO messages(public_id,conversation_id,reply_to_id,"
                "author_user_id,author_kind,body_text,sanitized_html,metadata_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    message_pid,
                    conversation["id"],
                    reply_to_id,
                    actor_user_id,
                    actor_kind,
                    body_text,
                    plain_to_safe_html(body_text),
                    canonical_json(metadata or {}),
                    created,
                ),
            )
            message_id = int(conn.execute(
                "SELECT id FROM messages WHERE public_id=?", (message_pid,)
            ).fetchone()[0])
            conn.execute(
                "UPDATE conversations SET updated_at=? WHERE id=?",
                (created, conversation["id"]),
            )
            recipients = conn.execute(
                "SELECT cp.user_id,u.username,u.email FROM conversation_participants cp "
                "JOIN users u ON u.id=cp.user_id "
                "WHERE cp.conversation_id=? AND cp.user_id IS NOT NULL "
                "AND (? IS NULL OR cp.user_id<>?)",
                (conversation["id"], actor_user_id, actor_user_id),
            ).fetchall()
            for recipient in recipients:
                uid = int(recipient["user_id"])
                self._insert_delivery(
                    conn,
                    message_id=message_id,
                    channel="in_app",
                    recipient_user_id=uid,
                    address_snapshot=str(recipient["username"]),
                    status="sent",
                    priority=0,
                    idempotency_key=f"message:{message_pid}:in_app:{uid}",
                    created_at=created,
                    sent_at=created,
                )
                if queue_email and recipient["email"]:
                    self._insert_delivery(
                        conn,
                        message_id=message_id,
                        channel="email",
                        recipient_user_id=uid,
                        address_snapshot=str(recipient["email"]),
                        status="queued",
                        priority=50,
                        idempotency_key=f"message:{message_pid}:email:{uid}",
                        created_at=created,
                    )
                if actor_kind in {"admin", "platform"}:
                    conn.execute(
                        "INSERT OR IGNORE INTO notifications(user_id,type,title,body,link,"
                        "is_read,communication_message_public_id,created_at) "
                        "VALUES(?,?,?,?,?,0,?,?)",
                        (
                            uid,
                            "communication",
                            conversation["subject"],
                            body_text,
                            f"/messages/{conversation_public_id}",
                            message_pid,
                            created,
                        ),
                    )
            bug = conn.execute(
                "SELECT id FROM bug_reports WHERE conversation_id=?",
                (conversation["id"],),
            ).fetchone()
            if bug:
                conn.execute(
                    "INSERT INTO bug_report_events(public_id,bug_report_id,event_type,"
                    "actor_user_id,note,created_at) VALUES(?,?,?,?,?,?)",
                    (
                        public_id("bge"),
                        bug["id"],
                        "admin_reply" if actor_kind in {"admin", "platform"}
                        else "reporter_reply",
                        actor_user_id,
                        "",
                        created,
                    ),
                )
                conn.execute(
                    "UPDATE bug_reports SET updated_at=? WHERE id=?",
                    (created, bug["id"]),
                )
            return {
                "public_id": message_pid,
                "reply_to": reply_to_public_id,
                "author_kind": actor_kind,
                "body_text": body_text,
                "sanitized_html": plain_to_safe_html(body_text),
                "created_at": created,
            }

    def _insert_delivery(
        self,
        conn: Any,
        *,
        message_id: int | None = None,
        broadcast_id: int | None = None,
        channel: str,
        recipient_user_id: int | None,
        address_snapshot: str,
        status: str,
        priority: int,
        idempotency_key: str,
        created_at: str,
        template_key: str = "",
        template_version: int = 0,
        payload: dict[str, Any] | None = None,
        sent_at: str | None = None,
        max_attempts: int = 5,
    ) -> str:
        delivery_pid = public_id("dlv")
        conn.execute(
            "INSERT OR IGNORE INTO deliveries(public_id,message_id,broadcast_id,channel,"
            "recipient_user_id,address_snapshot,status,priority,attempt_count,max_attempts,"
            "next_attempt_at,idempotency_key,template_key,template_version,payload_json,"
            "sent_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,0,?,?,?,?,?,?,?,?,?)",
            (
                delivery_pid,
                message_id,
                broadcast_id,
                channel,
                recipient_user_id,
                address_snapshot,
                status,
                priority,
                max_attempts,
                created_at,
                idempotency_key,
                template_key,
                template_version,
                canonical_json(payload or {}),
                sent_at,
                created_at,
                created_at,
            ),
        )
        row = conn.execute(
            "SELECT public_id FROM deliveries WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        return str(row["public_id"])

    # ── secure transactional email (no code in message/body snapshots) ─────

    def create_email_code_delivery(
        self,
        user_id: int,
        *,
        purpose: str,
        code: str,
        expires_at: str,
        template_key: str,
        priority: int = 100,
        max_attempts: int = 5,
    ) -> dict[str, Any]:
        """Atomically persist the short-lived secret row and its opaque delivery."""
        template = get_template(template_key)
        created = now_iso()
        with self.store._tx() as conn:
            user = conn.execute(
                "SELECT id,email FROM users WHERE id=?", (user_id,)
            ).fetchone()
            if user is None:
                raise CommunicationNotFound("用户不存在")
            code_cur = conn.execute(
                "INSERT INTO email_codes(user_id,purpose,code,expires_at,created_at) "
                "VALUES(?,?,?,?,?)",
                (user_id, purpose, code, expires_at, created),
            )
            code_id = int(code_cur.lastrowid)
            delivery_pid = self._insert_delivery(
                conn,
                channel="email",
                recipient_user_id=user_id,
                address_snapshot=str(user["email"]),
                status="queued",
                priority=priority,
                idempotency_key=f"auth:{purpose}:email-code:{code_id}",
                created_at=created,
                template_key=template.key,
                template_version=template.version,
                payload={
                    "kind": "email_code",
                    "email_code_id": code_id,
                    "purpose": purpose,
                },
                max_attempts=max_attempts,
            )
            return {
                "public_id": delivery_pid,
                "status": "queued",
                "template_key": template.key,
                "template_version": template.version,
            }

    def queue_transactional_email(
        self,
        user_id: int,
        *,
        template_key: str,
        payload: dict[str, Any],
        idempotency_key: str,
        priority: int = 100,
        max_attempts: int = 5,
    ) -> dict[str, Any]:
        template = get_template(template_key)
        created = now_iso()
        with self.store._tx() as conn:
            user = conn.execute(
                "SELECT id,email FROM users WHERE id=?", (user_id,)
            ).fetchone()
            if user is None:
                raise CommunicationNotFound("用户不存在")
            delivery_pid = self._insert_delivery(
                conn,
                channel="email",
                recipient_user_id=user_id,
                address_snapshot=str(user["email"]),
                status="queued",
                priority=priority,
                idempotency_key=idempotency_key,
                created_at=created,
                template_key=template.key,
                template_version=template.version,
                payload=payload,
                max_attempts=max_attempts,
            )
            return {
                "public_id": delivery_pid,
                "status": "queued",
                "template_key": template.key,
                "template_version": template.version,
            }

    # ── safe read models and permissions ─────────────────────────────

    def get_thread(
        self,
        conversation_public_id: str,
        *,
        user_id: int | None = None,
        admin: bool = False,
    ) -> dict[str, Any]:
        with self.store._tx() as conn:
            conversation = conn.execute(
                "SELECT * FROM conversations WHERE public_id=?",
                (conversation_public_id,),
            ).fetchone()
            if conversation is None or conversation["kind"] == "auth":
                raise CommunicationNotFound("会话不存在")
            if not admin:
                allowed = conn.execute(
                    "SELECT 1 FROM conversation_participants "
                    "WHERE conversation_id=? AND user_id=?",
                    (conversation["id"], user_id),
                ).fetchone()
                if allowed is None:
                    raise CommunicationNotFound("会话不存在")
            participants = [
                {
                    "public_id": row["public_id"],
                    "kind": row["participant_kind"],
                    "username": row["username"],
                    "display_name": row["display_name"],
                }
                for row in conn.execute(
                    "SELECT cp.public_id,cp.participant_kind,u.username,u.display_name "
                    "FROM conversation_participants cp "
                    "LEFT JOIN users u ON u.id=cp.user_id "
                    "WHERE cp.conversation_id=? ORDER BY cp.id",
                    (conversation["id"],),
                )
            ]
            messages = []
            for row in conn.execute(
                "SELECT m.public_id,m.author_kind,m.body_text,m.sanitized_html,"
                "m.metadata_json,m.created_at,reply.public_id AS reply_to_public_id,"
                "u.username,u.display_name FROM messages m "
                "LEFT JOIN messages reply ON reply.id=m.reply_to_id "
                "LEFT JOIN users u ON u.id=m.author_user_id "
                "WHERE m.conversation_id=? ORDER BY m.id",
                (conversation["id"],),
            ):
                messages.append({
                    "public_id": row["public_id"],
                    "reply_to": row["reply_to_public_id"],
                    "author": {
                        "kind": row["author_kind"],
                        "username": row["username"],
                        "display_name": row["display_name"],
                    },
                    "body_text": row["body_text"],
                    "sanitized_html": row["sanitized_html"],
                    "metadata": _json(row["metadata_json"], {}),
                    "created_at": row["created_at"],
                })
            bug = conn.execute(
                "SELECT public_id,status,category,impact,current_route "
                "FROM bug_reports WHERE conversation_id=?",
                (conversation["id"],),
            ).fetchone()
            return {
                "conversation": {
                    "public_id": conversation["public_id"],
                    "kind": conversation["kind"],
                    "subject": conversation["subject"],
                    "status": conversation["status"],
                    "created_at": conversation["created_at"],
                    "updated_at": conversation["updated_at"],
                    "participants": participants,
                    **({"bug_report": _dict(bug)} if bug else {}),
                },
                "messages": messages,
            }

    def list_threads(
        self,
        *,
        user_id: int | None = None,
        admin: bool = False,
        box: str = "inbox",
        page: int = 1,
        per_page: int = 30,
    ) -> dict[str, Any]:
        page = max(1, int(page))
        per_page = max(1, min(100, int(per_page)))
        offset = (page - 1) * per_page
        params: list[Any] = []
        where = ["c.kind<>'auth'"]
        joins = ""
        if admin:
            if box == "inbox":
                where.append(
                    "EXISTS(SELECT 1 FROM messages im WHERE im.conversation_id=c.id "
                    "AND im.author_kind='user')"
                )
            elif box == "sent":
                where.append(
                    "EXISTS(SELECT 1 FROM messages sm WHERE sm.conversation_id=c.id "
                    "AND sm.author_kind IN ('admin','platform'))"
                )
        else:
            joins = (
                "JOIN conversation_participants mine ON mine.conversation_id=c.id "
                "AND mine.user_id=? "
            )
            params.append(user_id)
            if box == "sent":
                where.append(
                    "EXISTS(SELECT 1 FROM messages sm WHERE sm.conversation_id=c.id "
                    "AND sm.author_user_id=?)"
                )
                params.append(user_id)
            else:
                where.append(
                    "EXISTS(SELECT 1 FROM messages im WHERE im.conversation_id=c.id "
                    "AND (im.author_user_id IS NULL OR im.author_user_id<>?))"
                )
                params.append(user_id)
        where_sql = " AND ".join(where)
        with self.store._tx() as conn:
            total = int(conn.execute(
                f"SELECT COUNT(DISTINCT c.id) FROM conversations c {joins} "
                f"WHERE {where_sql}",
                tuple(params),
            ).fetchone()[0])
            rows = conn.execute(
                "SELECT DISTINCT c.public_id,c.kind,c.subject,c.status,c.created_at,"
                "c.updated_at,(SELECT body_text FROM messages lm "
                "WHERE lm.conversation_id=c.id ORDER BY lm.id DESC LIMIT 1) latest_body,"
                "(SELECT created_at FROM messages lm WHERE lm.conversation_id=c.id "
                "ORDER BY lm.id DESC LIMIT 1) latest_at,"
                "(SELECT COUNT(*) FROM messages um WHERE um.conversation_id=c.id "
                + (
                    "AND um.id>COALESCE(mine.last_read_message_id,0) "
                    "AND (um.author_user_id IS NULL OR um.author_user_id<>?)"
                    if not admin else "AND 0"
                )
                + ") unread_count FROM conversations c "
                + joins
                + f"WHERE {where_sql} ORDER BY c.updated_at DESC,c.id DESC LIMIT ? OFFSET ?",
                tuple(
                    ([user_id] if not admin else [])
                    + params
                    + [per_page, offset]
                ),
            ).fetchall()
            return {
                "threads": [
                    {
                        "public_id": row["public_id"],
                        "kind": row["kind"],
                        "subject": row["subject"],
                        "status": row["status"],
                        "latest_body": row["latest_body"],
                        "latest_at": row["latest_at"],
                        "unread_count": int(row["unread_count"] or 0),
                        "created_at": row["created_at"],
                        "updated_at": row["updated_at"],
                    }
                    for row in rows
                ],
                "page": page,
                "per_page": per_page,
                "total": total,
            }

    def mark_read(self, conversation_public_id: str, user_id: int) -> str | None:
        with self.store._tx() as conn:
            conversation = conn.execute(
                "SELECT id FROM conversations WHERE public_id=? AND kind<>'auth'",
                (conversation_public_id,),
            ).fetchone()
            if conversation is None:
                raise CommunicationNotFound("会话不存在")
            participant = conn.execute(
                "SELECT id FROM conversation_participants "
                "WHERE conversation_id=? AND user_id=?",
                (conversation["id"], user_id),
            ).fetchone()
            if participant is None:
                raise CommunicationNotFound("会话不存在")
            latest = conn.execute(
                "SELECT id,public_id FROM messages WHERE conversation_id=? "
                "ORDER BY id DESC LIMIT 1",
                (conversation["id"],),
            ).fetchone()
            latest_id = int(latest["id"]) if latest else 0
            conn.execute(
                "UPDATE conversation_participants SET last_read_message_id=? WHERE id=?",
                (latest_id, participant["id"]),
            )
            conn.execute(
                "UPDATE notifications SET is_read=1 WHERE user_id=? AND "
                "communication_message_public_id IN (SELECT public_id FROM messages "
                "WHERE conversation_id=? AND id<=?)",
                (user_id, conversation["id"], latest_id),
            )
            return str(latest["public_id"]) if latest else None

    # ── broadcast fixed snapshot / second approval ──────────────────

    def resolve_audience(
        self, audience_kind: str, audience_filter: dict[str, Any]
    ) -> list[dict[str, Any]]:
        with self.store._tx() as conn:
            if audience_kind == "active_users":
                rows = conn.execute(
                    "SELECT id,username,email FROM users WHERE is_active=1 ORDER BY id"
                ).fetchall()
            elif audience_kind == "role":
                rows = conn.execute(
                    "SELECT id,username,email FROM users WHERE is_active=1 AND role=? "
                    "ORDER BY id",
                    (audience_filter["role"],),
                ).fetchall()
            elif audience_kind == "game_bot_owners":
                rows = conn.execute(
                    "SELECT DISTINCT u.id,u.username,u.email FROM users u "
                    "JOIN bots b ON b.owner_id=u.id "
                    "WHERE u.is_active=1 AND b.is_active=1 AND b.game_id=? ORDER BY u.id",
                    (audience_filter["game_id"],),
                ).fetchall()
            elif audience_kind == "contest_entrants":
                rows = conn.execute(
                    "SELECT DISTINCT u.id,u.username,u.email FROM users u "
                    "JOIN contest_entries ce ON ce.user_id=u.id "
                    "WHERE u.is_active=1 AND ce.contest_id=? ORDER BY u.id",
                    (audience_filter["contest_id"],),
                ).fetchall()
            elif audience_kind == "selected_users":
                usernames = sorted(set(audience_filter.get("usernames") or []))
                if not usernames:
                    return []
                marks = ",".join("?" for _ in usernames)
                rows = conn.execute(
                    f"SELECT id,username,email FROM users WHERE is_active=1 "
                    f"AND username IN ({marks}) ORDER BY id",
                    tuple(usernames),
                ).fetchall()
            else:
                raise ValueError("未知广播受众")
            return [_dict(row) for row in rows]

    def create_broadcast_preview(
        self,
        *,
        created_by_user_id: int,
        audience_kind: str,
        audience_filter: dict[str, Any],
        recipients: Iterable[dict[str, Any]],
        subject: str,
        body_text: str,
        channels: list[str],
        ttl_minutes: int = 15,
    ) -> dict[str, Any]:
        recipient_rows = list(recipients)
        recipient_ids = sorted({int(row["id"]) for row in recipient_rows})
        snapshot = {
            "audience_kind": audience_kind,
            "audience_filter": audience_filter,
            "recipient_ids": recipient_ids,
            "subject": subject,
            "body_text": body_text,
            "channels": sorted(channels),
        }
        snapshot_hash = content_hash(snapshot)
        approval_token = secrets.token_urlsafe(32)
        created = datetime.now()
        expires = created + timedelta(minutes=max(1, min(ttl_minutes, 60)))
        broadcast_pid = public_id("brd")
        with self.store._tx() as conn:
            conn.execute(
                "INSERT INTO broadcasts(public_id,state,created_by_user_id,audience_kind,"
                "audience_filter_json,audience_snapshot_hash,audience_count,subject,body_text,"
                "sanitized_html,channels_json,approval_token_hash,preview_expires_at,"
                "created_at,updated_at) VALUES(?,'draft',?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    broadcast_pid,
                    created_by_user_id,
                    audience_kind,
                    canonical_json(audience_filter),
                    snapshot_hash,
                    len(recipient_ids),
                    subject,
                    body_text,
                    plain_to_safe_html(body_text),
                    canonical_json(sorted(channels)),
                    token_hash(approval_token),
                    expires.isoformat(timespec="seconds"),
                    created.isoformat(timespec="seconds"),
                    created.isoformat(timespec="seconds"),
                ),
            )
            broadcast_id = int(conn.execute(
                "SELECT id FROM broadcasts WHERE public_id=?", (broadcast_pid,)
            ).fetchone()[0])
            conn.executemany(
                "INSERT INTO broadcast_recipients(public_id,broadcast_id,user_id,state,"
                "next_attempt_at,created_at) VALUES(?,?,?,'pending',?,?)",
                [
                    (
                        public_id("brc"), broadcast_id, uid,
                        created.isoformat(timespec="seconds"),
                        created.isoformat(timespec="seconds"),
                    )
                    for uid in recipient_ids
                ],
            )
        return {
            "public_id": broadcast_pid,
            "state": "draft",
            "audience_count": len(recipient_ids),
            "audience_snapshot_hash": snapshot_hash,
            "approval_token": approval_token,
            "preview_expires_at": expires.isoformat(timespec="seconds"),
            "channels": sorted(channels),
            "subject": subject,
            "body_text": body_text,
        }

    def approve_broadcast(
        self,
        public_id_value: str,
        *,
        actor_user_id: int,
        approval_token: str,
        scheduled_at: str | None,
    ) -> dict[str, Any]:
        now = datetime.now()
        now_value = now.isoformat(timespec="seconds")
        with self.store._tx() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM broadcasts WHERE public_id=?", (public_id_value,)
            ).fetchone()
            if row is None:
                raise CommunicationNotFound("广播预览不存在")
            if row["created_by_user_id"] != actor_user_id:
                raise CommunicationForbidden("只能批准自己创建的预览")
            if row["state"] != "draft":
                raise CommunicationConflict("广播预览已批准或取消")
            if not secrets.compare_digest(
                token_hash(approval_token), str(row["approval_token_hash"])
            ):
                raise CommunicationForbidden("二次批准令牌无效")
            if datetime.fromisoformat(row["preview_expires_at"]) < now:
                raise CommunicationConflict("广播预览已过期，请重新预览")
            recipient_ids = [
                int(item[0])
                for item in conn.execute(
                    "SELECT user_id FROM broadcast_recipients "
                    "WHERE broadcast_id=? AND user_id IS NOT NULL ORDER BY user_id",
                    (row["id"],),
                )
            ]
            recomputed = content_hash({
                "audience_kind": row["audience_kind"],
                "audience_filter": _json(row["audience_filter_json"], {}),
                "recipient_ids": recipient_ids,
                "subject": row["subject"],
                "body_text": row["body_text"],
                "channels": sorted(_json(row["channels_json"], [])),
            })
            if recomputed != row["audience_snapshot_hash"]:
                raise CommunicationConflict("广播预览内容或受众快照已变化")
            due = scheduled_at or now_value
            try:
                due_dt = datetime.fromisoformat(due)
            except ValueError as exc:
                raise ValueError("scheduled_at 必须是 ISO 时间") from exc
            if due_dt.tzinfo is not None:
                due_dt = due_dt.astimezone().replace(tzinfo=None)
            state = "scheduled"
            conn.execute(
                "UPDATE broadcasts SET state=?,scheduled_at=?,approved_at=?,updated_at=? "
                "WHERE id=?",
                (
                    state,
                    due_dt.isoformat(timespec="seconds"),
                    now_value,
                    now_value,
                    row["id"],
                ),
            )
            return {
                "public_id": row["public_id"],
                "state": state,
                "audience_count": row["audience_count"],
                "audience_snapshot_hash": row["audience_snapshot_hash"],
                "scheduled_at": due_dt.isoformat(timespec="seconds"),
            }

    def cancel_broadcast(self, public_id_value: str) -> dict[str, Any]:
        now = now_iso()
        with self.store._tx() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM broadcasts WHERE public_id=?", (public_id_value,)
            ).fetchone()
            if row is None:
                raise CommunicationNotFound("广播不存在")
            if row["state"] == "completed":
                raise CommunicationConflict("已完成广播不能取消")
            if row["state"] != "cancelled":
                conn.execute(
                    "UPDATE broadcasts SET state='cancelled',cancelled_at=?,updated_at=? "
                    "WHERE id=?",
                    (now, now, row["id"]),
                )
                conn.execute(
                    "UPDATE broadcast_recipients SET state='cancelled',processed_at=? "
                    "WHERE broadcast_id=? AND state IN ('pending','processing')",
                    (now, row["id"]),
                )
                # A delivery already inside SMTP has an unavoidable at-least-once race;
                # only not-yet-claimed work is safely cancelled here.
                conn.execute(
                    "UPDATE deliveries SET status='cancelled',cancelled_at=?,updated_at=? "
                    "WHERE broadcast_id=? AND status='queued'",
                    (now, now, row["id"]),
                )
            return {"public_id": row["public_id"], "state": "cancelled"}

    def list_broadcast_drafts(self, *, page: int, per_page: int) -> dict[str, Any]:
        return self._list_broadcasts_by_state("draft", page=page, per_page=per_page)

    def _list_broadcasts_by_state(
        self, state: str, *, page: int, per_page: int
    ) -> dict[str, Any]:
        page = max(1, page)
        per_page = max(1, min(100, per_page))
        with self.store._tx() as conn:
            total = int(conn.execute(
                "SELECT COUNT(*) FROM broadcasts WHERE state=?", (state,)
            ).fetchone()[0])
            rows = conn.execute(
                "SELECT public_id,state,audience_kind,audience_count,subject,"
                "audience_snapshot_hash,preview_expires_at,scheduled_at,created_at,updated_at "
                "FROM broadcasts WHERE state=? ORDER BY id DESC LIMIT ? OFFSET ?",
                (state, per_page, (page - 1) * per_page),
            ).fetchall()
            return {
                "broadcasts": [_dict(row) for row in rows],
                "page": page,
                "per_page": per_page,
                "total": total,
            }

    def broadcast_stats(self, public_id_value: str) -> dict[str, Any]:
        with self.store._tx() as conn:
            row = conn.execute(
                "SELECT id,public_id,state,audience_count,created_at,scheduled_at,"
                "completed_at,cancelled_at FROM broadcasts WHERE public_id=?",
                (public_id_value,),
            ).fetchone()
            if row is None:
                raise CommunicationNotFound("广播不存在")
            recipient_counts = {
                item["state"]: int(item["n"])
                for item in conn.execute(
                    "SELECT state,COUNT(*) n FROM broadcast_recipients "
                    "WHERE broadcast_id=? GROUP BY state",
                    (row["id"],),
                )
            }
            delivery_counts = {
                f"{item['channel']}:{item['status']}": int(item["n"])
                for item in conn.execute(
                    "SELECT channel,status,COUNT(*) n FROM deliveries "
                    "WHERE broadcast_id=? GROUP BY channel,status",
                    (row["id"],),
                )
            }
            public = _dict(row)
            public.pop("id", None)
            public["recipients"] = recipient_counts
            public["deliveries"] = delivery_counts
            return public

    # ── worker claims / result settlement ───────────────────────────

    def recover_inflight(self) -> dict[str, int]:
        now = now_iso()
        with self.store._tx() as conn:
            delivery_failed = conn.execute(
                "UPDATE deliveries SET status='failed',claimed_at=NULL,"
                "last_error='recovery_attempt_limit',updated_at=? "
                "WHERE status='sending' AND attempt_count>=max_attempts",
                (now,),
            ).rowcount
            delivery_queued = conn.execute(
                "UPDATE deliveries SET status='queued',claimed_at=NULL,next_attempt_at=?,"
                "updated_at=? WHERE status='sending' AND attempt_count<max_attempts",
                (now, now),
            ).rowcount
            recipient_failed = conn.execute(
                "UPDATE broadcast_recipients SET state='failed',"
                "last_error='recovery_attempt_limit',processed_at=? "
                "WHERE state='processing' AND attempt_count>=max_attempts",
                (now,),
            ).rowcount
            recipient_pending = conn.execute(
                "UPDATE broadcast_recipients SET state='pending',next_attempt_at=? "
                "WHERE state='processing' AND attempt_count<max_attempts",
                (now,),
            ).rowcount
            return {
                "deliveries": delivery_failed + delivery_queued,
                "broadcast_recipients": recipient_failed + recipient_pending,
            }

    def claim_delivery(self) -> dict[str, Any] | None:
        now = now_iso()
        with self.store._tx() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE deliveries SET status='failed',last_error='attempt_limit',"
                "updated_at=? WHERE status='queued' AND attempt_count>=max_attempts",
                (now,),
            )
            row = conn.execute(
                "SELECT * FROM deliveries WHERE channel='email' AND status='queued' "
                "AND attempt_count<max_attempts AND next_attempt_at<=? "
                "ORDER BY priority DESC,next_attempt_at,id LIMIT 1",
                (now,),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE deliveries SET status='sending',attempt_count=attempt_count+1,"
                "claimed_at=?,updated_at=? WHERE id=? AND status='queued'",
                (now, now, row["id"]),
            )
            claimed = conn.execute(
                "SELECT * FROM deliveries WHERE id=?", (row["id"],)
            ).fetchone()
            return _dict(claimed)

    def resolve_delivery_content(
        self, delivery: dict[str, Any]
    ) -> tuple[str, str, str] | None:
        """Return subject/text/html, or None when a secret delivery is stale."""
        with self.store._tx() as conn:
            user = conn.execute(
                "SELECT id,username,display_name,email,is_active,email_verified "
                "FROM users WHERE id=?",
                (delivery.get("recipient_user_id"),),
            ).fetchone()
            if user is None or not user["is_active"]:
                return None
            if delivery.get("template_key"):
                template = get_template(
                    str(delivery["template_key"]), int(delivery["template_version"])
                )
                payload = _json(delivery.get("payload_json"), {})
                context = dict(payload.get("context") or {})
                if payload.get("kind") == "email_code":
                    code_row = conn.execute(
                        "SELECT * FROM email_codes WHERE id=? AND user_id=?",
                        (payload.get("email_code_id"), user["id"]),
                    ).fetchone()
                    if code_row is None or code_row["used_at"] is not None:
                        return None
                    try:
                        if datetime.fromisoformat(code_row["expires_at"]) < datetime.now():
                            return None
                    except (TypeError, ValueError):
                        return None
                    latest = conn.execute(
                        "SELECT id FROM email_codes WHERE user_id=? AND purpose=? "
                        "AND used_at IS NULL ORDER BY id DESC LIMIT 1",
                        (user["id"], code_row["purpose"]),
                    ).fetchone()
                    if latest is None or int(latest["id"]) != int(code_row["id"]):
                        return None
                    # The code exists only in memory for rendering; never persisted in
                    # messages/delivery payload/error/audit fields.
                    context["code"] = code_row["code"]
                    try:
                        ttl = datetime.fromisoformat(
                            code_row["expires_at"]
                        ) - datetime.fromisoformat(code_row["created_at"])
                        context["expires_minutes"] = max(
                            1, int(round(ttl.total_seconds() / 60))
                        )
                    except (TypeError, ValueError):
                        return None
                context.setdefault(
                    "username", user["display_name"] or user["username"]
                )
                return template.render(context)
            message = conn.execute(
                "SELECT m.body_text,m.sanitized_html,c.subject FROM messages m "
                "JOIN conversations c ON c.id=m.conversation_id WHERE m.id=?",
                (delivery.get("message_id"),),
            ).fetchone()
            if message is None:
                return None
            return (
                str(message["subject"]),
                str(message["body_text"]),
                str(message["sanitized_html"]),
            )

    def mark_delivery_sent(
        self, delivery_public_id: str, *, provider_message_id: str
    ) -> None:
        now = now_iso()
        with self.store._tx() as conn:
            row = conn.execute(
                "SELECT * FROM deliveries WHERE public_id=?", (delivery_public_id,)
            ).fetchone()
            if row is None or row["status"] != "sending":
                return
            conn.execute(
                "UPDATE deliveries SET status='sent',provider='smtp',provider_message_id=?,"
                "sent_at=?,claimed_at=NULL,last_error='',updated_at=? WHERE id=?",
                (provider_message_id, now, now, row["id"]),
            )
            conn.execute(
                "INSERT INTO email_outbox(to_addr,subject,template_key,status,error,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    row["address_snapshot"],
                    "[communications delivery]",
                    row["template_key"],
                    "sent",
                    "",
                    now,
                ),
            )

    def mark_delivery_cancelled(self, delivery_public_id: str) -> None:
        now = now_iso()
        with self.store._tx() as conn:
            conn.execute(
                "UPDATE deliveries SET status='cancelled',cancelled_at=?,claimed_at=NULL,"
                "last_error='stale_or_unavailable',updated_at=? "
                "WHERE public_id=? AND status='sending'",
                (now, now, delivery_public_id),
            )

    def mark_delivery_failed_or_retry(
        self, delivery_public_id: str, *, error_code: str
    ) -> str:
        now = datetime.now()
        with self.store._tx() as conn:
            row = conn.execute(
                "SELECT * FROM deliveries WHERE public_id=?", (delivery_public_id,)
            ).fetchone()
            if row is None or row["status"] != "sending":
                return "ignored"
            attempt = int(row["attempt_count"])
            max_attempts = int(row["max_attempts"])
            if attempt >= max_attempts:
                status = "failed"
                next_at = now.isoformat(timespec="seconds")
            else:
                status = "queued"
                delay = min(3600, 30 * (2 ** max(0, attempt - 1)))
                next_at = (now + timedelta(seconds=delay)).isoformat(timespec="seconds")
            conn.execute(
                "UPDATE deliveries SET status=?,next_attempt_at=?,last_error=?,"
                "claimed_at=NULL,updated_at=? WHERE id=?",
                (
                    status,
                    next_at,
                    error_code[:80],
                    now.isoformat(timespec="seconds"),
                    row["id"],
                ),
            )
            if status == "failed":
                conn.execute(
                    "INSERT INTO email_outbox(to_addr,subject,template_key,status,error,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        row["address_snapshot"],
                        "[communications delivery]",
                        row["template_key"],
                        "failed",
                        error_code[:80],
                        now.isoformat(timespec="seconds"),
                    ),
                )
            return status

    def list_failed_deliveries(
        self, *, page: int, per_page: int
    ) -> dict[str, Any]:
        page = max(1, page)
        per_page = max(1, min(100, per_page))
        with self.store._tx() as conn:
            total = int(conn.execute(
                "SELECT COUNT(*) FROM deliveries WHERE status='failed'"
            ).fetchone()[0])
            rows = conn.execute(
                "SELECT d.public_id,d.channel,d.status,d.attempt_count,d.max_attempts,"
                "d.last_error,d.provider,d.provider_message_id,d.template_key,d.created_at,"
                "d.updated_at,c.public_id AS conversation_public_id,u.username "
                "FROM deliveries d LEFT JOIN messages m ON m.id=d.message_id "
                "LEFT JOIN conversations c ON c.id=m.conversation_id "
                "LEFT JOIN users u ON u.id=d.recipient_user_id "
                "WHERE d.status='failed' ORDER BY d.id DESC LIMIT ? OFFSET ?",
                (per_page, (page - 1) * per_page),
            ).fetchall()
            return {
                "deliveries": [_dict(row) for row in rows],
                "page": page,
                "per_page": per_page,
                "total": total,
            }

    def claim_broadcast_batch(self, *, batch_size: int) -> dict[str, Any] | None:
        now = now_iso()
        with self.store._tx() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE broadcast_recipients SET state='failed',"
                "last_error='attempt_limit',processed_at=? "
                "WHERE state='pending' AND attempt_count>=max_attempts",
                (now,),
            )
            broadcast = conn.execute(
                "SELECT b.* FROM broadcasts b WHERE b.state IN ('scheduled','running') "
                "AND (b.scheduled_at IS NULL OR b.scheduled_at<=?) AND ("
                "EXISTS(SELECT 1 FROM broadcast_recipients due "
                "WHERE due.broadcast_id=b.id AND due.state='pending' "
                "AND due.next_attempt_at<=?) OR NOT EXISTS(SELECT 1 "
                "FROM broadcast_recipients unfinished WHERE unfinished.broadcast_id=b.id "
                "AND unfinished.state IN ('pending','processing'))) "
                "ORDER BY b.id LIMIT 1",
                (now, now),
            ).fetchone()
            if broadcast is None:
                return None
            if broadcast["state"] == "scheduled":
                conn.execute(
                    "UPDATE broadcasts SET state='running',started_at=COALESCE(started_at,?),"
                    "updated_at=? WHERE id=?",
                    (now, now, broadcast["id"]),
                )
            recipients = conn.execute(
                "SELECT * FROM broadcast_recipients WHERE broadcast_id=? "
                "AND state='pending' AND next_attempt_at<=? ORDER BY id LIMIT ?",
                (broadcast["id"], now, max(1, min(batch_size, 500))),
            ).fetchall()
            if not recipients:
                unfinished = int(conn.execute(
                    "SELECT COUNT(*) FROM broadcast_recipients WHERE broadcast_id=? "
                    "AND state IN ('pending','processing')",
                    (broadcast["id"],),
                ).fetchone()[0])
                if unfinished == 0:
                    conn.execute(
                        "UPDATE broadcasts SET state='completed',completed_at=?,updated_at=? "
                        "WHERE id=? AND state='running'",
                        (now, now, broadcast["id"]),
                    )
                return None
            ids = [int(row["id"]) for row in recipients]
            marks = ",".join("?" for _ in ids)
            conn.execute(
                f"UPDATE broadcast_recipients SET state='processing',"
                f"attempt_count=attempt_count+1,last_error='' WHERE id IN ({marks})",
                tuple(ids),
            )
            return {
                "broadcast": _dict(broadcast),
                "recipients": [_dict(row) for row in recipients],
            }

    def process_broadcast_recipient(
        self, broadcast: dict[str, Any], recipient: dict[str, Any]
    ) -> None:
        with self.store._tx() as conn:
            current = conn.execute(
                "SELECT br.state,b.state AS broadcast_state FROM broadcast_recipients br "
                "JOIN broadcasts b ON b.id=br.broadcast_id WHERE br.public_id=?",
                (recipient["public_id"],),
            ).fetchone()
            if (
                current is None
                or current["state"] != "processing"
                or current["broadcast_state"] != "running"
            ):
                return
        uid = recipient.get("user_id")
        if uid is None:
            self.finish_broadcast_recipient(recipient["public_id"], state="cancelled")
            return
        channels = set(_json(broadcast.get("channels_json"), []))
        result = self.create_message_to_user(
            int(uid),
            kind="broadcast",
            subject=str(broadcast["subject"]),
            body_text=str(broadcast["body_text"]),
            author_kind="platform",
            metadata={"broadcast_public_id": broadcast["public_id"]},
            queue_email="email" in channels,
            email_priority=10,
            legacy_notification={
                "type": "broadcast",
                "title": str(broadcast["subject"]),
                "body": str(broadcast["body_text"]),
                "link": "/messages",
            },
            broadcast_id=int(broadcast["id"]),
            idempotency_prefix=f"broadcast:{broadcast['public_id']}:user:{uid}",
        )
        if result is None:
            self.finish_broadcast_recipient(recipient["public_id"], state="cancelled")
            return
        with self.store._tx() as conn:
            conversation = conn.execute(
                "SELECT id FROM conversations WHERE public_id=?",
                (result["conversation_public_id"],),
            ).fetchone()
            conn.execute(
                "UPDATE broadcast_recipients SET state='delivered',conversation_id=?,"
                "processed_at=? WHERE public_id=? AND state='processing'",
                (
                    conversation["id"] if conversation else None,
                    now_iso(),
                    recipient["public_id"],
                ),
            )

    def finish_broadcast_recipient(self, recipient_public_id: str, *, state: str) -> None:
        with self.store._tx() as conn:
            conn.execute(
                "UPDATE broadcast_recipients SET state=?,processed_at=? "
                "WHERE public_id=? AND state='processing'",
                (state, now_iso(), recipient_public_id),
            )

    def retry_broadcast_recipient(
        self, recipient_public_id: str, *, error_code: str
    ) -> str:
        """Bound projection retries without persisting provider/exception text."""
        now = datetime.now()
        with self.store._tx() as conn:
            row = conn.execute(
                "SELECT id,state,attempt_count,max_attempts FROM broadcast_recipients "
                "WHERE public_id=?",
                (recipient_public_id,),
            ).fetchone()
            if row is None or row["state"] != "processing":
                return "ignored"
            attempt = int(row["attempt_count"])
            if attempt >= int(row["max_attempts"]):
                state = "failed"
                next_at = now
            else:
                state = "pending"
                next_at = now + timedelta(
                    seconds=min(3600, 30 * (2 ** max(0, attempt - 1)))
                )
            conn.execute(
                "UPDATE broadcast_recipients SET state=?,next_attempt_at=?,last_error=?,"
                "processed_at=? WHERE id=?",
                (
                    state,
                    next_at.isoformat(timespec="seconds"),
                    error_code[:80],
                    now.isoformat(timespec="seconds") if state == "failed" else None,
                    row["id"],
                ),
            )
            return state
