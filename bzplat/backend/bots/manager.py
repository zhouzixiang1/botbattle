"""Bot 上传与版本管理。"""
from __future__ import annotations

import hashlib
import logging
import re
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from threading import Lock, RLock
from typing import Iterator

from ..bots.classify import (
    BinaryRejectError,
    classify_binary,
    require_supported_binary,
)
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


def _classify_upload(raw: bytes):
    """Classify once at the upload boundary and expose a stable API error."""
    try:
        return require_supported_binary(classify_binary(raw))
    except BinaryRejectError as exc:
        raise BotError("unsupported_binary", str(exc)) from exc


class BotManager:
    def __init__(
        self, store: Store, *, upload_root: Path | str = UPLOAD_ROOT
    ) -> None:
        self.store = store
        self.upload_root = Path(upload_root)
        self.upload_root.mkdir(parents=True, exist_ok=True)
        self._bot_locks_guard = Lock()
        self._bot_locks: dict[int, RLock] = {}

    @contextmanager
    def _bot_version_lock(self, bot_id: int) -> Iterator[None]:
        """Serialize one Bot's file + DB version transaction within this server.

        Uvicorn serves sync and async endpoints from different execution contexts;
        a state-only UI guard cannot protect API clients or concurrent browser tabs.
        """
        with self._bot_locks_guard:
            lock = self._bot_locks.setdefault(bot_id, RLock())
        with lock:
            yield

    def create_from_upload(
        self,
        owner_id: int,
        name: str,
        raw: bytes,
        *,
        display_name: str = "",
        description: str = "",
        upload_note: str = "",
        game_id: str = "holdem",
        runtime_mode: str | None = None,
        binary_runner=None,
    ) -> dict:
        if not _NAME_RE.match(name or ""):
            raise BotError(
                "invalid_name", "bot 名须字母开头，2-32 位字母数字下划线"
            )
        from bzplat.backend.store.schema import DEFAULT_RUNTIME_MODE, VALID_GAME_IDS, VALID_RUNTIME_MODES

        gid = (game_id or "holdem").strip().lower()
        if gid not in VALID_GAME_IDS:
            raise BotError("invalid_game", f"未知游戏类型: {gid}")
        rmode = (runtime_mode or DEFAULT_RUNTIME_MODE).strip().lower()
        if rmode not in VALID_RUNTIME_MODES:
            raise BotError("invalid_runtime_mode", f"未知运行模式: {rmode}")
        if not raw or len(raw) > MAX_BYTES:
            raise BotError("invalid_size", f"二进制大小须 1..{MAX_BYTES} 字节")
        info = _classify_upload(raw)
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
            game_id=gid,
            runtime_mode=rmode,
            # Hide the row until its staged binary has passed preflight and the
            # version commit succeeds.  Otherwise another request can challenge
            # an unverified upload while this request is still running.
            is_active=False,
        )
        try:
            self._write_version(
                bot["id"],
                raw,
                info,
                upload_note=upload_note,
                runtime_mode=rmode,
                game_id=gid,
                binary_runner=binary_runner,
            )
            self.store.update_bot(bot["id"], is_active=1)
        except Exception:
            self.purge_bot_files(bot["id"])
            self.store.delete_bot(bot["id"])
            raise
        return self.store.get_bot(bot["id"])

    def upload_version(
        self, bot_id: int, owner_id: int, raw: bytes, *, upload_note: str = "",
        runtime_mode: str | None = None, binary_runner=None
    ) -> dict:
        from bzplat.backend.store.schema import DEFAULT_RUNTIME_MODE, VALID_RUNTIME_MODES
        bot = self.store.get_bot(bot_id)
        if not bot or bot["owner_id"] != owner_id:
            raise BotError("not_found", "bot 不存在")
        if not raw or len(raw) > MAX_BYTES:
            raise BotError("invalid_size", f"二进制大小须 1..{MAX_BYTES} 字节")
        info = _classify_upload(raw)
        # 分配版本号、原子落盘、DB 写入和失败回滚必须属于同一 per-bot 临界区。
        # 否则并发上传会写同一个 vN，或预检失败误删另一请求的新版本。
        with self._bot_version_lock(bot_id):
            # Re-read after taking the lock: another tab may have activated a
            # different version while this request was classifying its payload.
            bot = self.store.get_bot(bot_id)
            if not bot or bot["owner_id"] != owner_id:
                raise BotError("not_found", "bot 不存在")
            rmode = (
                runtime_mode or bot.get("runtime_mode") or DEFAULT_RUNTIME_MODE
            ).strip().lower()
            if rmode not in VALID_RUNTIME_MODES:
                raise BotError("invalid_runtime_mode", f"未知运行模式: {rmode}")
            return self._write_version(
                bot_id,
                raw,
                info,
                upload_note=upload_note,
                runtime_mode=rmode,
                game_id=bot["game_id"],
                binary_runner=binary_runner,
            )

    def _run_preflight(
        self,
        bot_id: int,
        game_id: str,
        binary_runner,
        *,
        binary_path: str | None = None,
        runtime_mode: str,
    ) -> tuple[bool, str]:
        """按待发布版本所选模式试跑 canonical 首回合协议。"""
        import asyncio
        from bzplat.backend.games import preflight_bot
        from bzplat.backend.runtime.binary_runner import PlatformRunnerError

        bot = self.store.get_bot(bot_id)
        path = binary_path or (bot or {}).get("binary_path")
        if not bot or not path:
            return False, "预检失败：二进制路径缺失"
        try:
            with Path(path).open("rb") as binary:
                require_supported_binary(classify_binary(binary.read(4096)))
        except (OSError, BinaryRejectError) as exc:
            return False, f"预检失败：{exc}"
        try:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                # Normal API path: BotManager itself runs via asyncio.to_thread,
                # so this worker owns a fresh event loop and a fresh BinaryRunner.
                return asyncio.run(
                    preflight_bot(
                        game_id,
                        path,
                        binary_runner,
                        runtime_mode=runtime_mode,
                    )
                )
            else:
                # Defensive compatibility for direct synchronous calls made from
                # an already-running loop: isolate the nested asyncio.run in a worker.
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    return pool.submit(
                        lambda: asyncio.run(
                            preflight_bot(
                                game_id,
                                path,
                                binary_runner,
                                runtime_mode=runtime_mode,
                            )
                        )
                    ).result()
        except PlatformRunnerError:
            raise
        except Exception as e:
            logger.warning("preflight bot %s failed: %s", bot_id, e)
            return False, f"预检异常: {e}"

    def _write_version(
        self,
        bot_id: int,
        raw: bytes,
        info,
        *,
        upload_note: str,
        runtime_mode: str,
        game_id: str | None = None,
        binary_runner=None,
    ) -> dict:
        with self._bot_version_lock(bot_id):
            try:
                require_supported_binary(info)
            except BinaryRejectError as exc:
                raise BotError("unsupported_binary", str(exc)) from exc
            checksum = hashlib.sha256(raw).hexdigest()
            # 回滚只切换当前激活版本，不删除较新的历史版本。新上传必须接在
            # 历史最大版本之后；若用 current_version + 1，v2 -> 回滚 v1 后
            # 再上传会重复插入 v2，触发 bot_versions 唯一约束并返回 500。
            latest = self.store.get_latest_bot_version(bot_id)
            version = int(latest["version"]) + 1 if latest else 1
            bot_dir = self.upload_root / str(bot_id)
            bot_dir.mkdir(parents=True, exist_ok=True)
            dest_dir = bot_dir / f"v{version}"
            if dest_dir.exists():
                # DB 没有 vN（否则 latest 会包含它），因此这是上次崩溃留下的孤儿。
                logger.warning("remove orphan bot version directory bot=%s version=%s", bot_id, version)
                shutil.rmtree(dest_dir)

            temp_dir = Path(tempfile.mkdtemp(prefix=f".v{version}-", dir=bot_dir))
            temp_dest = temp_dir / "bot.bin"
            dest = dest_dir / "bot.bin"
            promoted = False
            try:
                temp_dest.write_bytes(raw)
                temp_dest.chmod(0o755)
                # Preflight the hidden temporary file before publishing either a
                # bot_versions row or bots.current_version.  A concurrent match can
                # therefore only snapshot the last validated active version.
                if binary_runner is not None:
                    ok, detail = self._run_preflight(
                        bot_id,
                        game_id or (self.store.get_bot(bot_id) or {}).get("game_id"),
                        binary_runner,
                        binary_path=str(temp_dest),
                        runtime_mode=runtime_mode,
                    )
                    if not ok:
                        raise BotError(
                            "preflight_failed", f"Bot 预检失败：{detail}"
                        )
                temp_dir.replace(dest_dir)
                promoted = True
                self.store.add_bot_version(
                    bot_id,
                    binary_path=str(dest),
                    upload_note=upload_note,
                    checksum=checksum,
                    size_bytes=len(raw),
                    os=info.os,
                    arch=info.arch,
                    format=info.format,
                    runtime_mode=runtime_mode,
                    version=version,
                )
            except Exception:
                if promoted:
                    shutil.rmtree(dest_dir, ignore_errors=True)
                raise
            finally:
                shutil.rmtree(temp_dir, ignore_errors=True)
            if not self.store.get_rating(bot_id):
                self.store.ensure_rating(bot_id)
            return self.store.get_bot(bot_id)

    def activate_version(self, bot_id: int, owner_id: int, version: int) -> dict:
        """Activate a current-format version while leaving legacy rows read-only."""
        with self._bot_version_lock(bot_id):
            bot = self.store.get_bot(bot_id)
            if not bot:
                raise BotError("not_found", "bot 不存在")
            if bot["owner_id"] != owner_id:
                raise BotError("forbidden", "无权修改他人的 Bot")
            target = next(
                (
                    row
                    for row in self.store.list_bot_versions(bot_id)
                    if int(row["version"]) == int(version)
                ),
                None,
            )
            if target is None:
                raise BotError("version_not_found", f"版本 {version} 不存在")
            from bzplat.backend.store.schema import (
                SUPPORTED_BINARY_ARCH,
                SUPPORTED_BINARY_ERROR,
                SUPPORTED_BINARY_FORMAT,
                SUPPORTED_BINARY_OS,
            )
            if (
                target.get("format") != SUPPORTED_BINARY_FORMAT
                or target.get("os") != SUPPORTED_BINARY_OS
                or target.get("arch") != SUPPORTED_BINARY_ARCH
            ):
                raise BotError("unsupported_binary", SUPPORTED_BINARY_ERROR)
            try:
                with Path(target["binary_path"]).open("rb") as binary:
                    require_supported_binary(classify_binary(binary.read(4096)))
            except BinaryRejectError as exc:
                raise BotError("unsupported_binary", str(exc)) from exc
            except OSError as exc:
                raise BotError("version_unavailable", "版本二进制文件不可用") from exc
            result = self.store.set_current_version(bot_id, version)
            # Per-bot lock makes disappearance impossible unless the DB itself was
            # externally modified; still fail closed rather than returning None.
            if result is None:
                raise BotError("version_not_found", f"版本 {version} 不存在")
            return result

    def purge_bot_files(self, bot_id: int) -> None:
        """删除 bot 的全部上传文件目录（bot_uploads/<id>/）。

        用于硬删 bot（admin_delete_bot）时清理磁盘——避免 DB 行 CASCADE 删了但文件
        留在磁盘变孤儿。软删（is_active=0）不调本方法（保留文件供恢复/历史对局）。
        """
        with self._bot_version_lock(bot_id):
            dest = self.upload_root / str(bot_id)
            if dest.exists():
                shutil.rmtree(dest, ignore_errors=True)

    def list_mine(
        self, owner_id: int, *, game_id: str | None = None,
        page: int | None = None, per_page: int = 50,
    ) -> list[dict] | dict:
        return self.store.list_bots(
            owner_id=owner_id, game_id=game_id, page=page, per_page=per_page,
        )

    def list_public(
        self, *, game_id: str | None = None, owner_id: int | None = None,
        page: int | None = None, per_page: int = 50,
    ) -> list[dict] | dict:
        # 私有 bot 功能已下线——所有 bot 都是公开的。保留方法名为兼容旧调用方，
        # 直接转发到 list_bots（不再有 public_only 过滤）。
        return self.store.list_bots(
            game_id=game_id, owner_id=owner_id, page=page, per_page=per_page,
        )

    def get(self, bot_id: int) -> dict | None:
        return self.store.get_bot(bot_id)

    def set_active(self, bot_id: int, owner_id: int, active: bool) -> dict:
        bot = self.store.get_bot(bot_id)
        if not bot or bot["owner_id"] != owner_id:
            raise BotError("not_found", "bot 不存在")
        self.store.update_bot(bot_id, is_active=1 if active else 0)
        return self.store.get_bot(bot_id)
