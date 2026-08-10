"""Bug-report conversation, append-only event and attachment metadata service."""
from __future__ import annotations

import hashlib
import io
import os
import re
import secrets
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from bzplat.backend.store import Store
from bzplat.backend.store.schema import BUG_REPORT_STATUSES

from .repository import (
    CommunicationForbidden,
    CommunicationNotFound,
)
from .utils import (
    canonical_json,
    clean_single_line,
    clean_text,
    now_iso,
    plain_to_safe_html,
    public_id,
    token_hash,
)

BUG_CATEGORIES = frozenset({"match", "bot", "contest", "account", "page", "other"})
BUG_IMPACTS = frozenset({"blocked", "major", "minor", "cosmetic"})
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024
MAX_ATTACHMENTS_PER_REPORT = 5
_FORMATS = {
    "PNG": ("image/png", "png"),
    "JPEG": ("image/jpeg", "jpg"),
    "WEBP": ("image/webp", "webp"),
    "GIF": ("image/gif", "gif"),
}
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")

_TRANSITIONS = {
    "new": {"acknowledged", "needs_info", "in_progress", "resolved", "duplicate", "wont_fix"},
    "acknowledged": {"needs_info", "in_progress", "resolved", "duplicate", "wont_fix"},
    "needs_info": {"in_progress", "resolved", "duplicate", "wont_fix"},
    "in_progress": {"needs_info", "resolved", "duplicate", "wont_fix"},
    "resolved": set(),
    "duplicate": set(),
    "wont_fix": set(),
}


class FeedbackService:
    def __init__(self, store: Store, attachment_root: str | Path) -> None:
        self.store = store
        # Keep the lexical path so an existing symlink cannot be hidden by resolve().
        self.attachment_root = Path(attachment_root).expanduser().absolute()

    def create_report(
        self,
        *,
        reporter_user_id: int | None,
        category: str,
        impact: str,
        title: str,
        body: str,
        current_route: str,
        diagnostics: dict[str, Any],
    ) -> dict[str, Any]:
        if category not in BUG_CATEGORIES:
            raise ValueError("未知问题分类")
        if impact not in BUG_IMPACTS:
            raise ValueError("未知影响程度")
        title = clean_single_line(title, max_length=160, field="title")
        body = clean_text(body, max_length=20_000, field="body")
        created = now_iso()
        conversation_pid = public_id("cnv")
        message_pid = public_id("msg")
        bug_pid = public_id("bug")
        tracking_token = secrets.token_urlsafe(32) if reporter_user_id is None else ""
        with self.store._tx() as conn:
            if reporter_user_id is not None and conn.execute(
                "SELECT 1 FROM users WHERE id=?", (reporter_user_id,)
            ).fetchone() is None:
                raise CommunicationNotFound("用户不存在")
            conn.execute(
                "INSERT INTO conversations(public_id,kind,subject,status,created_by_user_id,"
                "created_by_kind,created_at,updated_at) VALUES(?,'bug_report',?,'open',?,?,?,?)",
                (
                    conversation_pid,
                    title,
                    reporter_user_id,
                    "user",
                    created,
                    created,
                ),
            )
            conversation_id = int(conn.execute(
                "SELECT id FROM conversations WHERE public_id=?", (conversation_pid,)
            ).fetchone()[0])
            conn.execute(
                "INSERT INTO conversation_participants(public_id,conversation_id,user_id,"
                "participant_kind,joined_at) VALUES(?,?,?,?,?)",
                (public_id("cpt"), conversation_id, reporter_user_id, "user", created),
            )
            conn.execute(
                "INSERT INTO conversation_participants(public_id,conversation_id,user_id,"
                "participant_kind,joined_at) VALUES(?,?,NULL,'platform',?)",
                (public_id("cpt"), conversation_id, created),
            )
            conn.execute(
                "INSERT INTO messages(public_id,conversation_id,author_user_id,author_kind,"
                "body_text,sanitized_html,metadata_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    message_pid,
                    conversation_id,
                    reporter_user_id,
                    "user",
                    body,
                    plain_to_safe_html(body),
                    canonical_json({
                        "category": category, "impact": impact, "current_route": current_route,
                    }),
                    created,
                ),
            )
            conn.execute(
                "INSERT INTO bug_reports(public_id,conversation_id,reporter_user_id,"
                "tracking_token_hash,category,impact,title,current_route,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,'new',?,?)",
                (
                    bug_pid,
                    conversation_id,
                    reporter_user_id,
                    token_hash(tracking_token) if tracking_token else "",
                    category,
                    impact,
                    title,
                    current_route,
                    created,
                    created,
                ),
            )
            bug_id = int(conn.execute(
                "SELECT id FROM bug_reports WHERE public_id=?", (bug_pid,)
            ).fetchone()[0])
            conn.execute(
                "INSERT INTO bug_report_events(public_id,bug_report_id,event_type,"
                "actor_user_id,to_status,note,created_at) VALUES(?,?,'created',?,'new','',?)",
                (public_id("bge"), bug_id, reporter_user_id, created),
            )
            conn.execute(
                "INSERT INTO diagnostic_bundles(public_id,bug_report_id,schema_version,"
                "bundle_json,created_at) VALUES(?,?,?,?,?)",
                (
                    public_id("dgb"),
                    bug_id,
                    int(diagnostics.get("schema_version", 1)),
                    canonical_json(diagnostics),
                    created,
                ),
            )
        return {
            "public_id": bug_pid,
            "conversation_public_id": conversation_pid,
            "status": "new",
            "created_at": created,
            **({"tracking_token": tracking_token} if tracking_token else {}),
        }

    def list_owned(self, user_id: int, *, page: int, per_page: int) -> dict[str, Any]:
        page = max(1, page)
        per_page = max(1, min(100, per_page))
        with self.store._tx() as conn:
            total = int(conn.execute(
                "SELECT COUNT(*) FROM bug_reports WHERE reporter_user_id=?", (user_id,)
            ).fetchone()[0])
            rows = conn.execute(
                "SELECT b.public_id,c.public_id AS conversation_public_id,b.category,b.impact,"
                "b.title,b.current_route,b.status,b.created_at,b.updated_at "
                "FROM bug_reports b JOIN conversations c ON c.id=b.conversation_id "
                "WHERE b.reporter_user_id=? ORDER BY b.id DESC LIMIT ? OFFSET ?",
                (user_id, per_page, (page - 1) * per_page),
            ).fetchall()
            return {
                "bug_reports": [dict(row) for row in rows],
                "page": page,
                "per_page": per_page,
                "total": total,
            }

    def list_admin(
        self, *, status: str | None, page: int, per_page: int
    ) -> dict[str, Any]:
        if status is not None and status not in BUG_REPORT_STATUSES:
            raise ValueError("未知 Bug 状态")
        page = max(1, page)
        per_page = max(1, min(100, per_page))
        where = "WHERE b.status=?" if status else ""
        params: list[Any] = [status] if status else []
        with self.store._tx() as conn:
            total = int(conn.execute(
                f"SELECT COUNT(*) FROM bug_reports b {where}", tuple(params)
            ).fetchone()[0])
            rows = conn.execute(
                "SELECT b.public_id,c.public_id AS conversation_public_id,b.category,b.impact,"
                "b.title,b.current_route,b.status,b.created_at,b.updated_at,u.username "
                "FROM bug_reports b JOIN conversations c ON c.id=b.conversation_id "
                "LEFT JOIN users u ON u.id=b.reporter_user_id "
                f"{where} ORDER BY b.id DESC LIMIT ? OFFSET ?",
                tuple(params + [per_page, (page - 1) * per_page]),
            ).fetchall()
            return {
                "bug_reports": [dict(row) for row in rows],
                "page": page,
                "per_page": per_page,
                "total": total,
            }

    def get_detail(
        self, bug_public_id: str, *, user_id: int | None, admin: bool
    ) -> dict[str, Any]:
        with self.store._tx() as conn:
            row = conn.execute(
                "SELECT b.*,c.public_id AS conversation_public_id,u.username "
                "FROM bug_reports b JOIN conversations c ON c.id=b.conversation_id "
                "LEFT JOIN users u ON u.id=b.reporter_user_id WHERE b.public_id=?",
                (bug_public_id,),
            ).fetchone()
            if row is None or (not admin and row["reporter_user_id"] != user_id):
                raise CommunicationNotFound("Bug 反馈不存在")
            events = [
                {
                    "public_id": event["public_id"],
                    "event_type": event["event_type"],
                    "from_status": event["from_status"],
                    "to_status": event["to_status"],
                    "note": event["note"],
                    "created_at": event["created_at"],
                    "actor_username": event["actor_username"],
                }
                for event in conn.execute(
                    "SELECT e.public_id,e.event_type,e.from_status,e.to_status,e.note,"
                    "e.created_at,u.username AS actor_username FROM bug_report_events e "
                    "LEFT JOIN users u ON u.id=e.actor_user_id "
                    "WHERE e.bug_report_id=? ORDER BY e.id",
                    (row["id"],),
                )
            ]
            attachments = [
                {
                    "public_id": item["public_id"],
                    "original_name": item["original_name"],
                    "media_type": item["media_type"],
                    "size_bytes": item["size_bytes"],
                    "sha256": item["sha256"],
                    "created_at": item["created_at"],
                }
                for item in conn.execute(
                    "SELECT public_id,original_name,media_type,size_bytes,sha256,created_at "
                    "FROM bug_attachments WHERE bug_report_id=? ORDER BY id",
                    (row["id"],),
                )
            ]
            diagnostic = conn.execute(
                "SELECT public_id,schema_version,bundle_json,created_at "
                "FROM diagnostic_bundles WHERE bug_report_id=?", (row["id"],)
            ).fetchone()
            return {
                "public_id": row["public_id"],
                "conversation_public_id": row["conversation_public_id"],
                "category": row["category"],
                "impact": row["impact"],
                "title": row["title"],
                "current_route": row["current_route"],
                "status": row["status"],
                "reporter_username": row["username"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "events": events,
                "attachments": attachments,
                "diagnostic": (
                    {
                        "public_id": diagnostic["public_id"],
                        "schema_version": diagnostic["schema_version"],
                        "bundle": __import__("json").loads(diagnostic["bundle_json"]),
                        "created_at": diagnostic["created_at"],
                    }
                    if diagnostic else None
                ),
            }

    def update_status(
        self,
        bug_public_id: str,
        *,
        admin_user_id: int,
        new_status: str,
        note: str,
        duplicate_of_public_id: str | None = None,
    ) -> dict[str, Any]:
        if new_status not in BUG_REPORT_STATUSES:
            raise ValueError("未知 Bug 状态")
        note = (note or "").replace("\x00", "").strip()
        if len(note) > 2_000:
            raise ValueError("note 不能超过 2000 个字符")
        now = now_iso()
        with self.store._tx() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM bug_reports WHERE public_id=?", (bug_public_id,)
            ).fetchone()
            if row is None:
                raise CommunicationNotFound("Bug 反馈不存在")
            old_status = str(row["status"])
            if new_status == old_status:
                raise ValueError("状态未变化")
            if new_status not in _TRANSITIONS[old_status]:
                raise ValueError(f"不允许从 {old_status} 变为 {new_status}")
            duplicate_id = None
            if new_status == "duplicate":
                if not duplicate_of_public_id or duplicate_of_public_id == bug_public_id:
                    raise ValueError("duplicate 状态必须引用另一条 Bug")
                duplicate = conn.execute(
                    "SELECT id FROM bug_reports WHERE public_id=?",
                    (duplicate_of_public_id,),
                ).fetchone()
                if duplicate is None:
                    raise ValueError("duplicate_of 不存在")
                duplicate_id = duplicate["id"]
            conn.execute(
                "UPDATE bug_reports SET status=?,duplicate_of_id=?,updated_at=? WHERE id=?",
                (new_status, duplicate_id, now, row["id"]),
            )
            conn.execute(
                "INSERT INTO bug_report_events(public_id,bug_report_id,event_type,"
                "actor_user_id,from_status,to_status,note,created_at) "
                "VALUES(?,?,'status_changed',?,?,?,?,?)",
                (
                    public_id("bge"), row["id"], admin_user_id, old_status,
                    new_status, note, now,
                ),
            )
            if new_status in {"resolved", "duplicate", "wont_fix"}:
                conn.execute(
                    "UPDATE conversations SET status='closed',closed_at=?,updated_at=? "
                    "WHERE id=?",
                    (now, now, row["conversation_id"]),
                )
            return {"public_id": bug_public_id, "status": new_status, "updated_at": now}

    def authorize_attachment(
        self,
        bug_public_id: str,
        *,
        user_id: int | None,
        admin: bool,
        tracking_token: str,
    ) -> dict[str, Any]:
        with self.store._tx() as conn:
            row = conn.execute(
                "SELECT id,public_id,reporter_user_id,tracking_token_hash,status "
                "FROM bug_reports WHERE public_id=?", (bug_public_id,)
            ).fetchone()
            if row is None:
                raise CommunicationNotFound("Bug 反馈不存在")
            allowed = admin or (
                user_id is not None and row["reporter_user_id"] == user_id
            ) or (
                row["reporter_user_id"] is None
                and bool(tracking_token)
                and secrets.compare_digest(
                    str(row["tracking_token_hash"]), token_hash(tracking_token)
                )
            )
            if not allowed:
                raise CommunicationForbidden("无权为该反馈上传附件")
            count = int(conn.execute(
                "SELECT COUNT(*) FROM bug_attachments WHERE bug_report_id=?",
                (row["id"],),
            ).fetchone()[0])
            if count >= MAX_ATTACHMENTS_PER_REPORT:
                raise ValueError(f"每条反馈最多 {MAX_ATTACHMENTS_PER_REPORT} 个附件")
            return dict(row)

    def save_attachment(
        self,
        bug: dict[str, Any],
        *,
        uploaded_by_user_id: int | None,
        original_name: str,
        claimed_media_type: str,
        raw: bytes,
    ) -> dict[str, Any]:
        if not raw or len(raw) > MAX_ATTACHMENT_BYTES:
            raise ValueError(f"图片大小必须在 1-{MAX_ATTACHMENT_BYTES} 字节")
        try:
            with Image.open(io.BytesIO(raw)) as image:
                width, height = image.size
                if width <= 0 or height <= 0 or width * height > 40_000_000:
                    raise ValueError("图片像素尺寸超出限制")
                if int(getattr(image, "n_frames", 1)) > 200:
                    raise ValueError("动图帧数超出限制")
                image.verify()
                image_format = str(image.format or "").upper()
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise ValueError("附件不是可解析的图片") from exc
        detected = _FORMATS.get(image_format)
        if detected is None:
            raise ValueError("仅支持 PNG/JPEG/WebP/GIF 图片")
        detected_media_type, extension = detected
        if claimed_media_type.lower() != detected_media_type:
            raise ValueError("附件 MIME 与图片 magic 不一致")
        sha256 = hashlib.sha256(raw).hexdigest()
        safe_name = _SAFE_FILENAME_RE.sub("_", Path(original_name or "image").name)[:120]
        safe_name = safe_name or f"image.{extension}"
        if self.attachment_root.is_symlink():
            raise ValueError("附件根目录不能是符号链接")
        self.attachment_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.attachment_root.is_symlink() or not self.attachment_root.is_dir():
            raise ValueError("附件根目录无效")
        self.attachment_root.chmod(0o700)
        root_real = self.attachment_root.resolve(strict=True)
        report_dir = self.attachment_root / str(bug["public_id"])
        if report_dir.is_symlink():
            raise ValueError("附件报告目录不能是符号链接")
        report_dir.mkdir(mode=0o700, parents=False, exist_ok=True)
        if report_dir.is_symlink() or not report_dir.is_dir():
            raise ValueError("附件报告目录无效")
        report_dir = report_dir.resolve(strict=True)
        if report_dir.parent != root_real:
            raise ValueError("附件目录越界")
        report_dir.chmod(0o700)
        stored_name = f"{sha256[:16]}-{secrets.token_hex(8)}.{extension}"
        path = report_dir / stored_name
        fd = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw)
            created = now_iso()
            attachment_pid = public_id("att")
            try:
                with self.store._tx() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    current_count = int(conn.execute(
                        "SELECT COUNT(*) FROM bug_attachments WHERE bug_report_id=?",
                        (bug["id"],),
                    ).fetchone()[0])
                    if current_count >= MAX_ATTACHMENTS_PER_REPORT:
                        raise ValueError(
                            f"每条反馈最多 {MAX_ATTACHMENTS_PER_REPORT} 个附件"
                        )
                    conn.execute(
                        "INSERT INTO bug_attachments(public_id,bug_report_id,"
                        "uploaded_by_user_id,original_name,media_type,size_bytes,sha256,"
                        "storage_path,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (
                            attachment_pid, bug["id"], uploaded_by_user_id, safe_name,
                            detected_media_type, len(raw), sha256, str(path), created,
                        ),
                    )
                    conn.execute(
                        "INSERT INTO bug_report_events(public_id,bug_report_id,event_type,"
                        "actor_user_id,note,created_at) VALUES(?,?,'attachment_added',?,'',?)",
                        (public_id("bge"), bug["id"], uploaded_by_user_id, created),
                    )
                    conn.execute(
                        "UPDATE bug_reports SET updated_at=? WHERE id=?",
                        (created, bug["id"]),
                    )
            except Exception:
                path.unlink(missing_ok=True)
                raise
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return {
            "public_id": attachment_pid,
            "original_name": safe_name,
            "media_type": detected_media_type,
            "size_bytes": len(raw),
            "sha256": sha256,
            "created_at": created,
        }
