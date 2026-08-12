"""Use-case layer for platform communications."""
from __future__ import annotations

from typing import Any

from bzplat.backend.store import Store
from bzplat.backend.store.schema import (
    CONVERSATION_KINDS,
    DELIVERY_CHANNELS,
    ROLE_ADMIN,
    ROLE_ORGANIZER,
    ROLE_USER,
    TPL_RESET_PASSWORD,
    TPL_VERIFY_EMAIL,
    TPL_WELCOME,
    VALID_GAME_IDS,
)

from .repository import CommunicationRepository
from .utils import clean_single_line, clean_text

_TYPE_TO_PREF = {
    "match_done": "email_match_done",
    "followed": "email_followed",
    "contest": "email_contest",
    "comment": "email_comment",
}
_AUDIENCE_KINDS = {
    "active_users", "role", "game_bot_owners", "contest_entrants", "selected_users",
}


class CommunicationService:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.repository = CommunicationRepository(store)

    def notify_user(
        self,
        user_id: int,
        *,
        type: str = "",
        title: str = "",
        body: str = "",
        link: str = "",
        send_email: bool = False,
    ) -> dict[str, Any] | None:
        """Write communication truth plus an atomic legacy notification projection."""
        title = clean_single_line(
            title or "平台通知", max_length=160, field="title"
        )
        body = (body or title).replace("\x00", "").strip()
        if len(body) > 10_000:
            body = body[:10_000]
        queue_email = False
        if send_email:
            pref_key = _TYPE_TO_PREF.get(type)
            queue_email = True
            if pref_key:
                queue_email = bool(
                    self.store.get_notification_prefs(user_id).get(pref_key, 0)
                )
        result = self.repository.create_message_to_user(
            user_id,
            kind="notification",
            subject=title,
            body_text=body,
            metadata={"notification_type": type, "link": link},
            queue_email=queue_email,
            legacy_notification={
                "type": type,
                "title": title,
                "body": body,
                "link": link,
            },
        )
        return result["legacy_notification"] if result else None

    def queue_email_code(
        self,
        user: dict[str, Any],
        *,
        purpose: str,
        code: str,
        expires_at: str,
    ) -> dict[str, Any]:
        if purpose == "verify":
            template_key = TPL_VERIFY_EMAIL
        elif purpose == "reset":
            template_key = TPL_RESET_PASSWORD
        else:
            raise ValueError("无效验证码用途")
        # Intentionally no code/body/subject snapshot here.  The worker resolves the
        # short-lived email_codes row only while sending.
        return self.repository.create_email_code_delivery(
            int(user["id"]),
            purpose=purpose,
            code=code,
            expires_at=expires_at,
            template_key=template_key,
            priority=100,
            max_attempts=5,
        )

    def queue_welcome(self, user: dict[str, Any]) -> dict[str, Any]:
        return self.repository.queue_transactional_email(
            int(user["id"]),
            template_key=TPL_WELCOME,
            payload={
                "kind": "template",
                "context": {
                    "username": user.get("display_name") or user.get("username") or "",
                },
            },
            idempotency_key=f"auth:welcome:user:{user['id']}",
            priority=80,
            max_attempts=5,
        )

    def reply_user(
        self,
        conversation_public_id: str,
        *,
        user_id: int,
        body_text: str,
        reply_to: str | None,
    ) -> dict[str, Any]:
        body = clean_text(body_text, max_length=10_000, field="body")
        return self.repository.append_message(
            conversation_public_id,
            actor_user_id=user_id,
            actor_kind="user",
            body_text=body,
            reply_to_public_id=reply_to,
        )

    def reply_admin(
        self,
        conversation_public_id: str,
        *,
        admin_user_id: int,
        body_text: str,
        reply_to: str | None,
        queue_email: bool = False,
    ) -> dict[str, Any]:
        body = clean_text(body_text, max_length=10_000, field="body")
        return self.repository.append_message(
            conversation_public_id,
            actor_user_id=admin_user_id,
            actor_kind="admin",
            body_text=body,
            reply_to_public_id=reply_to,
            queue_email=queue_email,
        )

    def reply_guest_report(
        self,
        conversation_public_id: str,
        *,
        body_text: str,
        reply_to: str | None,
    ) -> dict[str, Any]:
        body = clean_text(body_text, max_length=10_000, field="body")
        return self.repository.append_message(
            conversation_public_id,
            actor_user_id=None,
            actor_kind="user",
            body_text=body,
            reply_to_public_id=reply_to,
            allow_anonymous_user=True,
        )

    def preview_broadcast(
        self,
        *,
        admin_user_id: int,
        audience_kind: str,
        audience_filter: dict[str, Any],
        subject: str,
        body_text: str,
        channels: list[str],
    ) -> dict[str, Any]:
        if audience_kind not in _AUDIENCE_KINDS:
            raise ValueError("未知广播受众")
        subject = clean_single_line(subject, max_length=160, field="subject")
        body_text = clean_text(body_text, max_length=20_000, field="body")
        normalized_channels = sorted(set(channels))
        if not normalized_channels or not set(normalized_channels) <= DELIVERY_CHANNELS:
            raise ValueError("channels 只能包含 in_app/email")
        # Ordinary broadcast truth is always in-app, even when email is requested.
        if "in_app" not in normalized_channels:
            normalized_channels.insert(0, "in_app")
        self._validate_audience_filter(audience_kind, audience_filter)
        recipients = self.repository.resolve_audience(audience_kind, audience_filter)
        return self.repository.create_broadcast_preview(
            created_by_user_id=admin_user_id,
            audience_kind=audience_kind,
            audience_filter=audience_filter,
            recipients=recipients,
            subject=subject,
            body_text=body_text,
            channels=normalized_channels,
        )

    @staticmethod
    def _validate_audience_filter(kind: str, value: dict[str, Any]) -> None:
        allowed: dict[str, set[str]] = {
            "active_users": set(),
            "role": {"role"},
            "game_bot_owners": {"game_id"},
            "contest_entrants": {"contest_id"},
            "selected_users": {"usernames"},
        }
        if set(value) != allowed[kind]:
            raise ValueError("audience_filter 字段与受众类型不匹配")
        if kind == "role" and value.get("role") not in {
            ROLE_USER, ROLE_ORGANIZER, ROLE_ADMIN,
        }:
            raise ValueError("未知用户角色")
        if kind == "game_bot_owners" and value.get("game_id") not in VALID_GAME_IDS:
            raise ValueError("未知游戏")
        if kind == "contest_entrants":
            if not isinstance(value.get("contest_id"), int) or value["contest_id"] <= 0:
                raise ValueError("contest_id 无效")
        if kind == "selected_users":
            usernames = value.get("usernames")
            if not isinstance(usernames, list) or not 1 <= len(usernames) <= 500:
                raise ValueError("usernames 必须包含 1-500 个公开用户名")
            if any(not isinstance(item, str) or not item for item in usernames):
                raise ValueError("usernames 格式无效")

    def assert_invariants(self) -> None:
        """Cheap runtime contract check used by targeted tests/startup diagnostics."""
        if not CONVERSATION_KINDS:
            raise RuntimeError("communications 状态常量缺失")
