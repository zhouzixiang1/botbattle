"""Fail-closed identity checks for fixed-credential QA accounts.

The QA scripts intentionally use predictable credentials, so a username match is
not proof that an existing row belongs to the script.  Reuse is allowed only when
the complete, script-specific identity contract still matches.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from bzplat.backend.crypto import hash_password, verify_password
from bzplat.backend.store import Store


@dataclass(frozen=True)
class QaAccountSpec:
    """Exact identity contract for one script-owned QA account."""

    namespace: str
    username: str
    email: str
    password: str
    role: str

    @property
    def identity_signature(self) -> str:
        return f"{self.namespace}:{self.username}:{self.role}"


def inspect_dedicated_account(store: Store, spec: QaAccountSpec) -> dict | None:
    """Return an exact existing account or fail without mutating it.

    Both username and email are checked bidirectionally.  This catches a foreign
    row occupying the dedicated email before a later INSERT could partially seed
    the database.
    """
    by_name = store.get_user_by_username(spec.username)
    by_email = store.get_user_by_email(spec.email)

    if by_name is None:
        if by_email is not None:
            raise RuntimeError(
                f"拒绝创建专用 QA 账号 {spec.identity_signature!r}："
                f"邮箱已属于其他账号 {by_email.get('username')!r}"
            )
        return None

    mismatches: list[str] = []
    if by_name.get("email") != spec.email:
        mismatches.append("email")
    if by_name.get("role") != spec.role:
        mismatches.append("role")
    if not verify_password(spec.password, by_name.get("password_hash") or ""):
        mismatches.append("password")
    if by_email is None or by_email.get("id") != by_name.get("id"):
        mismatches.append("email_owner")
    if mismatches:
        raise RuntimeError(
            f"拒绝复用已有账号 {spec.username!r}：专用 QA 身份契约不匹配 "
            f"({', '.join(mismatches)})"
        )
    return by_name


def preflight_dedicated_accounts(
    store: Store, specs: Iterable[QaAccountSpec]
) -> None:
    """Validate every existing name/email before the caller performs writes."""
    for spec in specs:
        inspect_dedicated_account(store, spec)


def get_or_create_dedicated_account(
    store: Store,
    spec: QaAccountSpec,
    *,
    activate: bool = True,
    verify_email: bool = True,
) -> dict:
    """Create or reuse only an account satisfying the exact QA contract."""
    user = inspect_dedicated_account(store, spec)
    if user is None:
        user = store.create_user(
            spec.username,
            spec.email,
            hash_password(spec.password),
            display_name=spec.username,
            role=spec.role,
        )
    if activate or verify_email:
        updates: dict[str, int] = {}
        if activate:
            updates["is_active"] = 1
        if verify_email:
            updates["email_verified"] = 1
        user = store.update_user(user["id"], **updates)
    return user
