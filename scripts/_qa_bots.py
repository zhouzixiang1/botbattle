"""Shared QA sample-Bot provisioning with checksum-based version refresh."""
from __future__ import annotations

import hashlib
from pathlib import Path

from bzplat.backend.bots.manager import BotManager
from bzplat.backend.store.schema import is_supported_binary_metadata


def _sha256_file(path: str) -> str | None:
    try:
        digest = hashlib.sha256()
        with Path(path).open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def ensure_qa_sample_bot(
    manager: BotManager,
    owner_id: int,
    name: str,
    game_id: str,
    raw: bytes,
) -> dict:
    """Create a dedicated QA Bot or publish the current sample as a new version.

    Idempotency is content-based, not merely name-based. This prevents a fixed QA
    account from keeping a stale protocol binary forever after the checked-in
    sample changes. Only callers that already validated their dedicated account
    namespace may use this helper.
    """
    store = manager.store
    existing = store.get_bot_by_owner_name(owner_id, name)
    if existing is None:
        return manager.create_from_upload(
            owner_id,
            name,
            raw,
            display_name=name,
            game_id=game_id,
        )
    if existing.get("game_id") != game_id:
        raise RuntimeError(
            f"QA Bot {name!r} 游戏不匹配："
            f"{existing.get('game_id')!r} != {game_id!r}"
        )

    expected_checksum = hashlib.sha256(raw).hexdigest()
    current = store.get_current_bot_version(int(existing["id"]))
    current_path = str((current or {}).get("binary_path") or existing.get("binary_path") or "")
    metadata_current = bool(
        current
        and current.get("checksum") == expected_checksum
        and int(current.get("size_bytes") or 0) == len(raw)
        and is_supported_binary_metadata(
            str(current.get("format") or ""),
            str(current.get("os") or ""),
            str(current.get("arch") or ""),
        )
    )
    if metadata_current and _sha256_file(current_path) == expected_checksum:
        # Dedicated QA seed state must stay runnable even if an earlier admin
        # scenario deactivated it. Reactivate only after exact content/platform
        # checks above have succeeded.
        if not bool(existing.get("is_active")):
            return manager.set_active(int(existing["id"]), owner_id, True)
        return existing

    synced = manager.upload_version(
        int(existing["id"]),
        owner_id,
        raw,
        upload_note=f"QA sample sync {expected_checksum[:12]}",
        runtime_mode=str(existing.get("runtime_mode") or "traditional"),
    )
    if not bool(synced.get("is_active")):
        synced = manager.set_active(int(existing["id"]), owner_id, True)
    return synced


__all__ = ["ensure_qa_sample_bot"]
