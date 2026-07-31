"""Bot 上传与版本管理。"""
from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path

from ..bots.classify import BinaryRejectError, classify_binary
from ..store import Store

logger = logging.getLogger(__name__)

UPLOAD_ROOT = Path("bot_uploads")
MAX_BYTES = 50 * 1024 * 1024
_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{1,31}$")


class BotError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class BotManager:
    def __init__(
        self, store: Store, *, upload_root: Path | str = UPLOAD_ROOT
    ) -> None:
        self.store = store
        self.upload_root = Path(upload_root)
        self.upload_root.mkdir(parents=True, exist_ok=True)

    def create_from_upload(
        self,
        owner_id: int,
        name: str,
        raw: bytes,
        *,
        display_name: str = "",
        description: str = "",
        upload_note: str = "",
        is_public: bool = True,
    ) -> dict:
        if not _NAME_RE.match(name or ""):
            raise BotError(
                "invalid_name", "bot 名须字母开头，2-32 位字母数字下划线"
            )
        if not raw or len(raw) > MAX_BYTES:
            raise BotError("invalid_size", f"二进制大小须 1..{MAX_BYTES} 字节")
        info = classify_binary(raw)
        if info.format == "macho" or not info.runnable:
            raise BotError(
                "unsupported_binary", info.reject_reason or "不支持的二进制"
            )
        if self.store.get_bot_by_owner_name(owner_id, name):
            raise BotError("name_taken", "同名 bot 已存在")
        bot = self.store.create_bot(
            owner_id=owner_id,
            name=name,
            display_name=display_name or name,
            description=description,
            os=info.os,
            arch=info.arch,
            format=info.format,
            is_public=is_public,
        )
        try:
            self._write_version(bot["id"], raw, info, upload_note=upload_note)
        except Exception:
            self.store.delete_bot(bot["id"])
            raise
        return self.store.get_bot(bot["id"])

    def upload_version(
        self, bot_id: int, owner_id: int, raw: bytes, *, upload_note: str = ""
    ) -> dict:
        bot = self.store.get_bot(bot_id)
        if not bot or bot["owner_id"] != owner_id:
            raise BotError("not_found", "bot 不存在")
        if not raw or len(raw) > MAX_BYTES:
            raise BotError("invalid_size", f"二进制大小须 1..{MAX_BYTES} 字节")
        info = classify_binary(raw)
        if info.format == "macho" or not info.runnable:
            raise BotError(
                "unsupported_binary", info.reject_reason or "不支持的二进制"
            )
        return self._write_version(bot_id, raw, info, upload_note=upload_note)

    def _write_version(
        self, bot_id: int, raw: bytes, info, *, upload_note: str
    ) -> dict:
        checksum = hashlib.sha256(raw).hexdigest()
        bot = self.store.get_bot(bot_id)
        version = int(bot["current_version"]) + 1
        dest_dir = self.upload_root / str(bot_id) / f"v{version}"
        dest_dir.mkdir(parents=True, exist_ok=True)
        ext = ".exe" if info.format == "pe" else ".bin"
        dest = dest_dir / f"bot{ext}"
        dest.write_bytes(raw)
        dest.chmod(0o755)
        self.store.add_bot_version(
            bot_id,
            binary_path=str(dest),
            upload_note=upload_note,
            checksum=checksum,
            size_bytes=len(raw),
            os=info.os,
            arch=info.arch,
            format=info.format,
            version=version,
        )
        if not self.store.get_rating(bot_id):
            self.store.ensure_rating(bot_id)
        return self.store.get_bot(bot_id)

    def list_mine(self, owner_id: int) -> list[dict]:
        return self.store.list_bots(owner_id=owner_id)

    def list_public(self) -> list[dict]:
        return self.store.list_bots(public_only=True)

    def get(self, bot_id: int) -> dict | None:
        return self.store.get_bot(bot_id)

    def set_active(self, bot_id: int, owner_id: int, active: bool) -> dict:
        bot = self.store.get_bot(bot_id)
        if not bot or bot["owner_id"] != owner_id:
            raise BotError("not_found", "bot 不存在")
        self.store.update_bot(bot_id, is_active=1 if active else 0)
        return self.store.get_bot(bot_id)
