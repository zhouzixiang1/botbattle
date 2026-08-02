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
        game_id: str = "holdem",
        binary_runner=None,
    ) -> dict:
        if not _NAME_RE.match(name or ""):
            raise BotError(
                "invalid_name", "bot 名须字母开头，2-32 位字母数字下划线"
            )
        from bzplat.backend.store.schema import VALID_GAME_IDS

        gid = (game_id or "holdem").strip().lower()
        if gid not in VALID_GAME_IDS:
            raise BotError("invalid_game", f"未知游戏类型: {gid}")
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
            game_id=gid,
        )
        try:
            self._write_version(bot["id"], raw, info, upload_note=upload_note)
        except Exception:
            self.store.delete_bot(bot["id"])
            raise
        # 预检：试跑 bot 验证响应合法（拒绝明显不合格的二进制）
        if binary_runner is not None:
            ok, detail = self._run_preflight(bot["id"], gid, binary_runner)
            if not ok:
                self.store.delete_bot(bot["id"])
                raise BotError("preflight_failed", f"Bot 预检失败：{detail}")
        return self.store.get_bot(bot["id"])

    def upload_version(
        self, bot_id: int, owner_id: int, raw: bytes, *, upload_note: str = "", binary_runner=None
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
        result = self._write_version(bot_id, raw, info, upload_note=upload_note)
        # 预检
        if binary_runner is not None:
            ok, detail = self._run_preflight(bot_id, bot["game_id"], binary_runner)
            if not ok:
                # 回滚到上一个版本（删除刚写的版本）
                self._rollback_version(bot_id)
                raise BotError("preflight_failed", f"Bot 预检失败：{detail}")
        return result

    def _run_preflight(self, bot_id: int, game_id: str, binary_runner) -> tuple[bool, str]:
        """试跑 bot 验证响应合法（经该游戏的 spec.preflight_check）。"""
        import asyncio
        from bzplat.backend.games import preflight_bot

        bot = self.store.get_bot(bot_id)
        if not bot or not bot.get("binary_path"):
            return True, "无二进制路径，跳过"
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # 在已有事件循环中（如 FastAPI）——用 ensure_future
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    return pool.submit(
                        lambda: asyncio.run(
                            preflight_bot(game_id, bot["binary_path"], binary_runner)
                        )
                    ).result()
            return asyncio.run(preflight_bot(game_id, bot["binary_path"], binary_runner))
        except Exception as e:
            logger.warning("preflight bot %s failed: %s", bot_id, e)
            return False, f"预检异常: {e}"

    def _rollback_version(self, bot_id: int) -> None:
        """删除最新版本，回退到上一版本（预检失败时）。"""
        bot = self.store.get_bot(bot_id)
        if not bot:
            return
        cur_ver = int(bot.get("current_version") or 0)
        if cur_ver <= 1:
            return  # 第一版无法回滚
        # 删除最新版本目录 + DB 记录
        import shutil
        dest_dir = self.upload_root / str(bot_id) / f"v{cur_ver}"
        if dest_dir.exists():
            shutil.rmtree(dest_dir, ignore_errors=True)
        self.store.delete_bot_version(bot_id, cur_ver)

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

    def list_mine(self, owner_id: int, *, game_id: str | None = None) -> list[dict]:
        return self.store.list_bots(owner_id=owner_id, game_id=game_id)

    def list_public(
        self, *, game_id: str | None = None, owner_id: int | None = None
    ) -> list[dict]:
        return self.store.list_bots(
            public_only=True, game_id=game_id, owner_id=owner_id
        )

    def get(self, bot_id: int) -> dict | None:
        return self.store.get_bot(bot_id)

    def set_active(self, bot_id: int, owner_id: int, active: bool) -> dict:
        bot = self.store.get_bot(bot_id)
        if not bot or bot["owner_id"] != owner_id:
            raise BotError("not_found", "bot 不存在")
        self.store.update_bot(bot_id, is_active=1 if active else 0)
        return self.store.get_bot(bot_id)
