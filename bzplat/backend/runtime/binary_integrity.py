"""Fail-closed integrity checks for immutable uploaded Bot artifacts."""
from __future__ import annotations

import hashlib
import stat as stat_module
from pathlib import Path
from typing import Any, TypeAlias


VERSION_UNAVAILABLE_REASON = "version_unavailable"
BinaryIntegrityCacheKey: TypeAlias = tuple[
    str, str, int, int, int, int, int, int
]


def require_binary_file_integrity(
    runtime: dict[str, Any],
    path: str,
    *,
    cache: set[BinaryIntegrityCacheKey] | None = None,
) -> None:
    """Validate persisted size/SHA metadata without exposing paths in errors.

    Empty checksum plus zero size identifies a pre-integrity historical row: its
    digest is unavailable, but the referenced file must still exist and be a
    regular file. Any supplied integrity field is authoritative. Cache identity
    includes device, inode, size, mtime and ctime, so replacement or in-place
    modification cannot reuse an earlier digest merely by restoring old mtime.
    """

    expected_checksum = str(runtime.get("checksum") or "").strip().lower()
    expected_size = int(runtime.get("size_bytes") or 0)
    if expected_size < 0:
        raise ValueError(VERSION_UNAVAILABLE_REASON)

    binary_path = Path(path)
    stat_before = binary_path.stat()
    if not stat_module.S_ISREG(stat_before.st_mode):
        raise ValueError(VERSION_UNAVAILABLE_REASON)
    if not expected_checksum and expected_size == 0:
        return
    signature = (
        int(stat_before.st_dev),
        int(stat_before.st_ino),
        int(stat_before.st_size),
        int(stat_before.st_mtime_ns),
        int(stat_before.st_ctime_ns),
    )
    if expected_size > 0 and stat_before.st_size != expected_size:
        raise ValueError(VERSION_UNAVAILABLE_REASON)
    if not expected_checksum:
        return

    cache_key: BinaryIntegrityCacheKey = (
        path,
        expected_checksum,
        expected_size,
        *signature,
    )
    if cache is not None and cache_key in cache:
        return

    digest = hashlib.sha256()
    with binary_path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    stat_after = binary_path.stat()
    signature_after = (
        int(stat_after.st_dev),
        int(stat_after.st_ino),
        int(stat_after.st_size),
        int(stat_after.st_mtime_ns),
        int(stat_after.st_ctime_ns),
    )
    if signature_after != signature or digest.hexdigest().lower() != expected_checksum:
        raise ValueError(VERSION_UNAVAILABLE_REASON)

    if cache is not None:
        if len(cache) >= 1024:
            cache.pop()
        cache.add(cache_key)
