"""Shared QA sample-Bot provisioning with checksum-based version refresh."""
from __future__ import annotations

import hashlib
import stat
from pathlib import Path

from bzplat.backend.bots.manager import BotManager
from bzplat.backend.store.schema import is_supported_binary_metadata


def _sha256_file(path: Path | str) -> str | None:
    try:
        digest = hashlib.sha256()
        with Path(path).open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _canonical_current_binary(
    manager: BotManager,
    bot_id: int,
    current: dict | None,
) -> Path | None:
    """Return a runnable current binary owned by this QA runtime only.

    A copied database can retain an absolute path into the primary checkout or
    another worktree.  Equal bytes do not authorize cross-runtime reuse: the
    version must live at the exact path produced by this manager and remain a
    regular file executable by the non-root sandbox user.
    """
    if current is None:
        return None
    try:
        version = int(current["version"])
        if version <= 0:
            return None
        root = manager.upload_root.resolve()
        candidate = Path(str(current["binary_path"])).resolve()
        canonical = (root / str(bot_id) / f"v{version}" / "bot.bin").resolve()
        mode = candidate.stat().st_mode
    except (KeyError, TypeError, ValueError, OSError):
        return None
    if candidate != canonical or root not in candidate.parents:
        return None
    if not candidate.is_file() or not mode & stat.S_IXOTH:
        return None
    return candidate


def _mirror_matches_current(
    bot: dict,
    current: dict,
    current_path: Path,
) -> bool:
    """Check that the denormalized bots row mirrors its current version."""
    try:
        mirror_path = Path(str(bot["binary_path"])).resolve()
        return bool(
            int(bot.get("current_version") or 0) == int(current["version"])
            and mirror_path == current_path
            and all(
                str(bot.get(field) or "") == str(current.get(field) or "")
                for field in ("format", "os", "arch", "runtime_mode")
            )
        )
    except (KeyError, TypeError, ValueError, OSError):
        return False


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

    bot_id = int(existing["id"])
    expected_checksum = hashlib.sha256(raw).hexdigest()
    # Seed helpers can run beside API workers in the same process.  Reuse the
    # manager's per-Bot RLock across inspection, optional upload and activation;
    # upload_version/set_active are intentionally re-entrant on this lock.
    with manager._bot_version_lock(bot_id):
        existing = store.get_bot(bot_id)
        if existing is None or int(existing.get("owner_id") or 0) != int(owner_id):
            raise RuntimeError(f"QA Bot {name!r} 在同步期间消失或归属变化")
        current = store.get_current_bot_version(bot_id)
        current_path = _canonical_current_binary(manager, bot_id, current)
        reusable = bool(
            current
            and current_path is not None
            and current.get("checksum") == expected_checksum
            and int(current.get("size_bytes") or 0) == len(raw)
            and is_supported_binary_metadata(
                str(current.get("format") or ""),
                str(current.get("os") or ""),
                str(current.get("arch") or ""),
            )
            and _sha256_file(current_path) == expected_checksum
            and _mirror_matches_current(existing, current, current_path)
        )
        if reusable:
            # Dedicated QA seed state must stay runnable even if an earlier admin
            # scenario deactivated it.  Activation now reads the same verified
            # local mirror under the same per-Bot lock.
            if not bool(existing.get("is_active")):
                return manager.set_active(bot_id, owner_id, True)
            return existing

        # Any path, mode, executable-bit, content, metadata or mirror drift gets
        # a fresh immutable version in this runtime.  Never mutate/reuse a path
        # inherited from another checkout merely because its bytes match.
        synced = manager.upload_version(
            bot_id,
            owner_id,
            raw,
            upload_note=f"QA sample sync {expected_checksum[:12]}",
            runtime_mode=str(existing.get("runtime_mode") or "traditional"),
        )
        if not bool(synced.get("is_active")):
            synced = manager.set_active(bot_id, owner_id, True)
        return synced


__all__ = ["ensure_qa_sample_bot"]
