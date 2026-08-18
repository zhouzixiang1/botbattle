"""Bot 上传与版本管理。"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import stat
import tempfile
from contextlib import ExitStack, contextmanager, nullcontext
from pathlib import Path
from threading import Lock, RLock
from typing import Any, Iterator

from ..bots.classify import (
    BinaryRejectError,
    classify_binary,
    require_supported_binary,
)
from ..store import RankedBotSelectionBusyError, Store
from ..runtime.limits import MAX_BOT_UPLOAD_BYTES

logger = logging.getLogger(__name__)

UPLOAD_ROOT = Path("bot_uploads")
# 兼容既有调用方名称；容量真相集中在 runtime/limits.py。
MAX_BYTES = MAX_BOT_UPLOAD_BYTES
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
        self,
        store: Store,
        *,
        upload_root: Path | str = UPLOAD_ROOT,
        create_upload_root: bool = True,
    ) -> None:
        self.store = store
        self.upload_root = Path(upload_root)
        self._create_upload_root = bool(create_upload_root)
        self._upload_root_created = False
        self._upload_root_identity: tuple[int, int] | None = None
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

        # game_id 缺省时使用上传入口的产品默认；显式空值不得被解释成德州。
        gid = ("holdem" if game_id is None else game_id).strip().lower()
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
            self.store.select_ranked_bot(owner_id, bot["id"], if_empty=True)
        except Exception:
            self.purge_bot_files(bot["id"])
            if not self.store.delete_unpublished_bot(bot["id"]):
                # Unexpected references/side effects must not be blessed by the
                # narrow staging rollback.  Generic hard delete remains
                # deliberately fail-closed for the rating projection.
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
                    stored_bot = self.store.get_bot(bot_id)
                    effective_game_id = (
                        (stored_bot or {}).get("game_id")
                        if game_id is None
                        else game_id
                    )
                    ok, detail = self._run_preflight(
                        bot_id,
                        effective_game_id,
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

    def _load_cutover_binary(
        self,
        source_binary_path: Path | str,
        *,
        expected_sha256: str,
        expected_size_bytes: int,
    ) -> tuple[Path, bytes, Any]:
        """Load one operator-injected standard ELF into a stable byte snapshot."""

        try:
            source = Path(source_binary_path).expanduser().resolve(strict=True)
        except (FileNotFoundError, OSError) as exc:
            raise BotError("cutover_asset_unavailable", "标准 Bot 二进制不可读") from exc
        if not source.is_file():
            raise BotError("cutover_asset_unavailable", "标准 Bot 二进制不是普通文件")
        try:
            raw = source.read_bytes()
        except OSError as exc:
            raise BotError("cutover_asset_unavailable", "标准 Bot 二进制不可读") from exc
        if not raw or len(raw) > MAX_BYTES:
            raise BotError(
                "invalid_size", f"标准 Bot 二进制大小须 1..{MAX_BYTES} 字节"
            )
        supplied_size = int(expected_size_bytes)
        if supplied_size != len(raw):
            raise BotError("cutover_asset_mismatch", "标准 Bot 二进制 size 校验失败")
        supplied_checksum = str(expected_sha256 or "").strip().lower()
        actual_checksum = hashlib.sha256(raw).hexdigest()
        if supplied_checksum != actual_checksum:
            raise BotError("cutover_asset_mismatch", "标准 Bot 二进制 SHA-256 校验失败")
        info = _classify_upload(raw[:4096])
        return source, raw, info

    @staticmethod
    def _cutover_manifest_digest(manifest: list[dict[str, Any]]) -> str:
        payload = json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _canonical_cutover_root(self, *, create: bool = False) -> Path:
        root = Path(os.path.abspath(str(self.upload_root.expanduser())))
        expected = Path(self.store.path).expanduser().resolve().parent / "bot_uploads"
        if root != expected:
            raise BotError(
                "noncanonical_upload_root",
                "规则切换必须写入目标数据库旁的 canonical bot_uploads",
            )
        if root.is_symlink() or (root.exists() and not root.is_dir()):
            raise BotError(
                "unsafe_upload_root", "canonical bot_uploads 不得是符号链接或非目录"
            )
        if root.exists():
            root_stat = root.lstat()
            if (
                int(root_stat.st_uid) != os.geteuid()
                or stat.S_IMODE(root_stat.st_mode) & 0o022
                or root.resolve(strict=True) != root
            ):
                raise BotError(
                    "unsafe_upload_root",
                    "canonical bot_uploads 必须由当前用户持有且不可被 group/other 写入",
                )
        if create:
            if not self._create_upload_root:
                raise BotError(
                    "unsafe_upload_root", "dry-run manager 不得创建 bot_uploads"
                )
            existed = os.path.lexists(root)
            root.mkdir(parents=True, exist_ok=True)
            if root.is_symlink() or not root.is_dir():
                raise BotError("unsafe_upload_root", "canonical bot_uploads 创建失败")
            if not existed:
                self._upload_root_created = True
                self._upload_root_identity = self._directory_identity(root)
                self._fsync_path(root, directory=True)
                self._fsync_path(root.parent, directory=True)
        return root

    def _build_game_contract_cutover_plan(
        self,
        *,
        cutover_id: str,
        game_id: str,
        from_contract: dict[str, str],
        to_contract: dict[str, str],
        source: Path,
        raw: bytes,
        info: Any,
        upload_note: str,
    ) -> dict[str, Any]:
        from bzplat.backend.store.schema import (
            VALID_RUNTIME_MODES,
            game_rule_contract,
        )

        gid = str(game_id or "").strip().lower()
        clean_id = str(cutover_id or "").strip()
        if not clean_id:
            raise BotError("invalid_cutover_id", "cutover_id 不能为空")
        expected_target = game_rule_contract(gid)
        source_contract = {
            key: str(from_contract.get(key) or "").strip()
            for key in ("ruleset_version", "protocol_version", "rating_pool_id")
        }
        target_contract = {
            key: str(to_contract.get(key) or "").strip()
            for key in ("ruleset_version", "protocol_version", "rating_pool_id")
        }
        if any(not value for value in source_contract.values()):
            raise BotError("invalid_contract", "from contract 字段不能为空")
        if (
            source_contract["protocol_version"]
            == target_contract["protocol_version"]
        ):
            raise BotError(
                "invalid_contract",
                "game-contract-cutover 仅允许不兼容协议代际",
            )
        root = self._canonical_cutover_root(create=False)
        checksum = hashlib.sha256(raw).hexdigest()

        def runtime_summary(manifest: list[dict[str, Any]]) -> dict[str, Any]:
            existing_counts: dict[str, int] = {}
            replacement_counts: dict[str, int] = {}
            changed = 0
            for entry in manifest:
                previous = str(
                    entry.get("expected_current_runtime_mode")
                    or DEFAULT_RUNTIME_MODE
                )
                replacement = str(
                    entry.get("runtime_mode") or DEFAULT_RUNTIME_MODE
                )
                existing_counts[previous] = existing_counts.get(previous, 0) + 1
                replacement_counts[replacement] = (
                    replacement_counts.get(replacement, 0) + 1
                )
                if previous != replacement:
                    changed += 1
            return {
                "existing_runtime_modes": existing_counts,
                "replacement_runtime_modes": replacement_counts,
                "runtime_mode_change_count": changed,
            }

        existing = self.store.get_protocol_cutover(clean_id)
        if existing is not None:
            expected_marker = {
                "game_id": gid,
                "from_ruleset": source_contract["ruleset_version"],
                "to_ruleset": target_contract["ruleset_version"],
                "from_protocol": source_contract["protocol_version"],
                "to_protocol": target_contract["protocol_version"],
                "from_rating_pool": source_contract["rating_pool_id"],
                "to_rating_pool": target_contract["rating_pool_id"],
            }
            drift = [
                key for key, value in expected_marker.items()
                if str(existing.get(key) or "") != value
            ]
            manifest = existing["version_manifest"]
            if drift or any(
                str(entry.get("checksum") or "") != checksum
                or int(entry.get("size_bytes") or -1) != len(raw)
                for entry in manifest
            ):
                raise BotError(
                    "cutover_id_conflict",
                    "cutover_id 已由不同 contract 或标准二进制占用",
                )
            digest = self._cutover_manifest_digest(manifest)
            if digest != existing["manifest_digest"]:
                raise BotError("cutover_marker_corrupt", "cutover manifest 摘要损坏")
            for entry in manifest:
                expected_path = (
                    root / str(int(entry["bot_id"]))
                    / f"v{int(entry['version'])}" / "bot.bin"
                )
                if Path(str(entry.get("binary_path") or "")) != expected_path:
                    raise BotError(
                        "cutover_marker_corrupt", "cutover manifest 路径不 canonical"
                    )
            return {
                "cutover_id": clean_id,
                "game_id": gid,
                "from_contract": source_contract,
                "to_contract": target_contract,
                "source_binary_path": str(source),
                "source_checksum": checksum,
                "source_size_bytes": len(raw),
                "version_manifest": manifest,
                "manifest_digest": digest,
                "bot_count": len(manifest),
                "already_applied": True,
                **runtime_summary(manifest),
            }

        if target_contract != expected_target:
            raise BotError("invalid_contract", "to contract 与代码 current 不一致")

        bots = self.store.list_bots(active_only=False, game_id=gid)
        assert isinstance(bots, list)
        manifest: list[dict[str, Any]] = []
        for bot in sorted(bots, key=lambda row: int(row["id"])):
            bot_id = int(bot["id"])
            latest = self.store.get_latest_bot_version(bot_id)
            current = self.store.get_current_bot_version(bot_id)
            inherited_runtime_mode = str(
                current.get("runtime_mode")
                if current is not None
                else bot.get("runtime_mode")
                or DEFAULT_RUNTIME_MODE
            )
            if inherited_runtime_mode not in VALID_RUNTIME_MODES:
                raise BotError(
                    "invalid_runtime_mode",
                    f"Bot {bot_id} 当前 runtime_mode 非法",
                )
            version = int(latest["version"] if latest else 0) + 1
            target_path = root / str(bot_id) / f"v{version}" / "bot.bin"
            manifest.append(
                {
                    "bot_id": bot_id,
                    "version": version,
                    "expected_current_version": int(bot.get("current_version") or 0),
                    "expected_current_version_id": (
                        int(current["id"]) if current is not None else None
                    ),
                    "expected_current_checksum": (
                        str(current.get("checksum") or "") if current else ""
                    ),
                    "expected_current_binary_path": (
                        str(current.get("binary_path") or "")
                        if current else str(bot.get("binary_path") or "")
                    ),
                    "expected_current_runtime_mode": str(
                        inherited_runtime_mode
                    ),
                    "binary_path": str(target_path),
                    "checksum": checksum,
                    "size_bytes": len(raw),
                    "os": info.os,
                    "arch": info.arch,
                    "format": info.format,
                    "runtime_mode": inherited_runtime_mode,
                    "upload_note": str(upload_note or "ruleset cutover"),
                }
            )
        return {
            "cutover_id": clean_id,
            "game_id": gid,
            "from_contract": source_contract,
            "to_contract": target_contract,
            "source_binary_path": str(source),
            "source_checksum": checksum,
            "source_size_bytes": len(raw),
            "version_manifest": manifest,
            "manifest_digest": self._cutover_manifest_digest(manifest),
            "bot_count": len(manifest),
            "already_applied": False,
            **runtime_summary(manifest),
        }

    def plan_game_contract_cutover(
        self,
        *,
        cutover_id: str,
        game_id: str,
        from_contract: dict[str, str],
        to_contract: dict[str, str],
        source_binary_path: Path | str,
        expected_sha256: str,
        expected_size_bytes: int,
        upload_note: str = "platform standard ruleset cutover",
    ) -> dict[str, Any]:
        """Dry-run the fixed vN/per-Bot path manifest without writing files."""

        source, raw, info = self._load_cutover_binary(
            source_binary_path,
            expected_sha256=expected_sha256,
            expected_size_bytes=expected_size_bytes,
        )
        return self._build_game_contract_cutover_plan(
            cutover_id=cutover_id,
            game_id=game_id,
            from_contract=from_contract,
            to_contract=to_contract,
            source=source,
            raw=raw,
            info=info,
            upload_note=upload_note,
        )

    @staticmethod
    def _directory_identity(path: Path) -> tuple[int, int]:
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise OSError(f"unsafe cutover directory: {path}")
        return int(info.st_dev), int(info.st_ino)

    @staticmethod
    def _fsync_path(path: Path, *, directory: bool = False) -> None:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        if directory:
            flags |= getattr(os, "O_DIRECTORY", 0)
        fd = os.open(path, flags)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _remove_cutover_dir(
        path: Path, *, expected_identity: tuple[int, int] | None = None
    ) -> None:
        try:
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            try:
                fd = os.open(path, flags)
            except FileNotFoundError:
                return
            try:
                opened = os.fstat(fd)
                identity = (int(opened.st_dev), int(opened.st_ino))
                if expected_identity is not None and identity != expected_identity:
                    logger.error("refuse replaced cutover cleanup target %s", path)
                    return
                os.fchmod(fd, 0o700)
                for name in os.listdir(fd):
                    child_stat = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if stat.S_ISLNK(child_stat.st_mode):
                        os.unlink(name, dir_fd=fd)
                    elif stat.S_ISREG(child_stat.st_mode):
                        os.chmod(name, 0o700, dir_fd=fd, follow_symlinks=False)
                        os.unlink(name, dir_fd=fd)
                    else:
                        raise OSError(f"unsafe cutover child: {path / name}")
            finally:
                os.close(fd)
            current = path.lstat()
            if expected_identity is not None and (
                int(current.st_dev), int(current.st_ino)
            ) != expected_identity:
                logger.error("refuse swapped cutover cleanup target %s", path)
                return
            if stat.S_ISLNK(current.st_mode) or not stat.S_ISDIR(current.st_mode):
                logger.error("refuse unsafe cutover cleanup target %s", path)
                return
            path.rmdir()
        except OSError:
            logger.exception("failed to clean cutover target %s", path)

    def apply_game_contract_cutover(
        self,
        *,
        cutover_id: str,
        game_id: str,
        from_contract: dict[str, str],
        to_contract: dict[str, str],
        source_binary_path: Path | str,
        expected_sha256: str,
        expected_size_bytes: int,
        expected_manifest_digest: str,
        upload_note: str = "platform standard ruleset cutover",
        binary_runner: Any | None = None,
        offline_guard: Any | None = None,
    ) -> dict[str, Any]:
        """Cold-stage one private file per Bot, then atomically switch metadata."""

        source, raw, info = self._load_cutover_binary(
            source_binary_path,
            expected_sha256=expected_sha256,
            expected_size_bytes=expected_size_bytes,
        )
        committed = False
        created_targets: list[tuple[Path, tuple[int, int]]] = []
        created_bot_dirs: list[Path] = []
        temp_dirs: dict[Path, tuple[int, int]] = {}
        store_call_started = False
        guard_context = (
            self.store.offline_cutover_guard()
            if offline_guard is None
            else nullcontext(
                self.store.bind_offline_cutover_guard(offline_guard)
            )
        )
        with guard_context as active_offline_guard:
            plan = self._build_game_contract_cutover_plan(
                cutover_id=cutover_id,
                game_id=game_id,
                from_contract=from_contract,
                to_contract=to_contract,
                source=source,
                raw=raw,
                info=info,
                upload_note=upload_note,
            )
            if plan["manifest_digest"] != str(expected_manifest_digest or ""):
                raise BotError(
                    "cutover_plan_drift",
                    "cutover manifest 与已审核 dry-run 摘要不一致",
                )
            root = self._canonical_cutover_root(create=True)
            manifest = plan["version_manifest"]
            with ExitStack() as locks:
                for bot_id in sorted(int(entry["bot_id"]) for entry in manifest):
                    locks.enter_context(self._bot_version_lock(bot_id))
                try:
                    preflight_detail = "no bots require replacement"
                    if manifest:
                        if binary_runner is None:
                            raise BotError(
                                "cutover_preflight_required",
                                "hard cutover apply 必须注入当前平台 BinaryRunner 预检标准 ELF",
                            )
                        preflight_dir = Path(
                            tempfile.mkdtemp(prefix=".cutover-preflight-", dir=root)
                        )
                        temp_dirs[preflight_dir] = self._directory_identity(
                            preflight_dir
                        )
                        preflight_path = preflight_dir / "bot.bin"
                        with preflight_path.open("xb") as preflight_binary:
                            preflight_binary.write(raw)
                            preflight_binary.flush()
                            os.fsync(preflight_binary.fileno())
                        preflight_path.chmod(0o555)
                        preflight_details: dict[str, str] = {}
                        representatives: dict[str, int] = {}
                        for entry in manifest:
                            representatives.setdefault(
                                str(entry["runtime_mode"]), int(entry["bot_id"])
                            )
                        for inherited_mode, representative_bot_id in sorted(
                            representatives.items()
                        ):
                            ok, detail = self._run_preflight(
                                representative_bot_id,
                                plan["game_id"],
                                binary_runner,
                                binary_path=str(preflight_path),
                                runtime_mode=inherited_mode,
                            )
                            preflight_details[inherited_mode] = detail
                            if not ok:
                                raise BotError(
                                    "cutover_preflight_failed",
                                    "标准 Bot 未通过现行规则首回合预检"
                                    f" ({inherited_mode})：{detail}",
                                )
                        preflight_detail = json.dumps(
                            preflight_details,
                            ensure_ascii=False,
                            sort_keys=True,
                        )
                    if not plan["already_applied"]:
                        for entry in manifest:
                            bot_dir = root / str(int(entry["bot_id"]))
                            if bot_dir.is_symlink() or (
                                bot_dir.exists() and not bot_dir.is_dir()
                            ):
                                raise BotError(
                                    "unsafe_cutover_target",
                                    f"Bot 版本根不得是符号链接或非目录: {bot_dir}",
                                )
                            if not bot_dir.exists():
                                bot_dir.mkdir()
                                created_bot_dirs.append(bot_dir)
                                self._fsync_path(root, directory=True)
                            bot_dir_stat = bot_dir.lstat()
                            if (
                                not stat.S_ISDIR(bot_dir_stat.st_mode)
                                or stat.S_ISLNK(bot_dir_stat.st_mode)
                                or int(bot_dir_stat.st_uid) != os.geteuid()
                                or stat.S_IMODE(bot_dir_stat.st_mode) & 0o022
                                or bot_dir.resolve(strict=True) != bot_dir
                            ):
                                raise BotError(
                                    "unsafe_cutover_target",
                                    f"Bot 版本根归属/权限不安全: {bot_dir}",
                                )
                            dest_dir = bot_dir / f"v{int(entry['version'])}"
                            dest = dest_dir / "bot.bin"
                            if dest_dir.is_symlink():
                                raise BotError(
                                    "unsafe_cutover_target",
                                    f"目标版本目录不得是符号链接: {dest_dir}",
                                )
                            if dest_dir.exists():
                                dest_dir_stat = dest_dir.lstat()
                                try:
                                    dest_stat = dest.lstat()
                                except FileNotFoundError:
                                    dest_stat = None
                                if (
                                    not stat.S_ISDIR(dest_dir_stat.st_mode)
                                    or stat.S_ISLNK(dest_dir_stat.st_mode)
                                    or stat.S_IMODE(dest_dir_stat.st_mode) != 0o555
                                    or int(dest_dir_stat.st_uid) != os.geteuid()
                                    or dest_stat is None
                                    or stat.S_ISLNK(dest_stat.st_mode)
                                    or not stat.S_ISREG(dest_stat.st_mode)
                                    or stat.S_IMODE(dest_stat.st_mode) != 0o555
                                    or int(dest_stat.st_uid) != os.geteuid()
                                    or int(dest_stat.st_nlink) != 1
                                    or dest.resolve(strict=True) != dest
                                ):
                                    raise BotError(
                                        "unsafe_cutover_target",
                                        f"目标版本路径形态非法: {dest_dir}",
                                    )
                                if dest.read_bytes() != raw:
                                    raise BotError(
                                        "cutover_target_exists",
                                        f"目标版本目录已被不同内容占用: {dest_dir}",
                                    )
                                self._fsync_path(dest)
                                self._fsync_path(dest_dir, directory=True)
                                self._fsync_path(bot_dir, directory=True)
                                continue
                            temp_dir = Path(
                                tempfile.mkdtemp(
                                    prefix=f".cutover-v{int(entry['version'])}-",
                                    dir=bot_dir,
                                )
                            )
                            temp_dirs[temp_dir] = self._directory_identity(temp_dir)
                            temp_dest = temp_dir / "bot.bin"
                            with temp_dest.open("wb") as staged:
                                staged.write(raw)
                                staged.flush()
                                os.fsync(staged.fileno())
                            temp_dest.chmod(0o555)
                            self._fsync_path(temp_dest)
                            staged_info = _classify_upload(temp_dest.read_bytes()[:4096])
                            if (
                                staged_info.os != info.os
                                or staged_info.arch != info.arch
                                or staged_info.format != info.format
                                or hashlib.sha256(temp_dest.read_bytes()).hexdigest()
                                != expected_sha256.lower()
                            ):
                                raise BotError(
                                    "cutover_stage_mismatch", "逐 Bot staging 校验失败"
                                )
                            self._fsync_path(temp_dir, directory=True)
                            if os.path.lexists(dest_dir):
                                raise BotError(
                                    "unsafe_cutover_target",
                                    f"目标版本目录在 staging 期间被占用: {dest_dir}",
                                )
                            temp_dir.replace(dest_dir)
                            target_identity = temp_dirs.pop(temp_dir)
                            dest_dir.chmod(0o555)
                            self._fsync_path(dest_dir, directory=True)
                            self._fsync_path(bot_dir, directory=True)
                            created_targets.append((dest_dir, target_identity))

                    store_call_started = True
                    result = self.store.cutover_game_contract(
                        cutover_id=plan["cutover_id"],
                        game_id=plan["game_id"],
                        from_contract=plan["from_contract"],
                        to_contract=plan["to_contract"],
                        version_manifest=manifest,
                        canonical_binary_root=str(root),
                        offline_guard=active_offline_guard,
                    )
                    committed = True
                    result["manifest_digest"] = plan["manifest_digest"]
                    result["asset_paths"] = [
                        str(entry["binary_path"]) for entry in manifest
                    ]
                    result["preflight"] = {
                        "ok": True,
                        "detail": preflight_detail,
                        "runtime_modes": sorted(
                            {str(entry["runtime_mode"]) for entry in manifest}
                        ),
                        "source_checksum": plan["source_checksum"],
                        "source_size_bytes": plan["source_size_bytes"],
                    }
                    result["existing_runtime_modes"] = plan[
                        "existing_runtime_modes"
                    ]
                    result["replacement_runtime_modes"] = plan[
                        "replacement_runtime_modes"
                    ]
                    result["runtime_mode_change_count"] = plan[
                        "runtime_mode_change_count"
                    ]
                    return result
                finally:
                    for temp_dir, identity in list(temp_dirs.items()):
                        self._remove_cutover_dir(
                            temp_dir, expected_identity=identity
                        )
                    if not committed:
                        preserve_assets = False
                        if store_call_started:
                            try:
                                marker = self.store.get_protocol_cutover(
                                    plan["cutover_id"]
                                )
                            except Exception:
                                preserve_assets = True
                                logger.exception(
                                    "cutover DB state unknown; preserve staged assets"
                                )
                            else:
                                if marker is not None:
                                    preserve_assets = True
                                    try:
                                        self.store.assert_protocol_cutover_postconditions(
                                            plan["cutover_id"],
                                            expected_manifest_digest=plan[
                                                "manifest_digest"
                                            ],
                                        )
                                    except Exception:
                                        logger.exception(
                                            "cutover marker exists but postcondition "
                                            "verification failed; preserve assets"
                                        )
                        if not preserve_assets:
                            for target, identity in reversed(created_targets):
                                self._remove_cutover_dir(
                                    target, expected_identity=identity
                                )
                            for bot_dir in reversed(created_bot_dirs):
                                try:
                                    bot_dir.rmdir()
                                except OSError:
                                    pass
                            if (
                                self._upload_root_created
                                and self._upload_root_identity is not None
                            ):
                                try:
                                    if (
                                        self._directory_identity(root)
                                        == self._upload_root_identity
                                        and not any(root.iterdir())
                                    ):
                                        root.rmdir()
                                        self._fsync_path(root.parent, directory=True)
                                        self._upload_root_created = False
                                        self._upload_root_identity = None
                                except OSError:
                                    logger.exception(
                                        "failed to remove newly-created empty cutover root %s",
                                        root,
                                    )

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
            active_contract = self.store.get_active_game_contract(bot["game_id"])
            if target.get("retired_at") is not None:
                raise BotError("version_retired", "该版本已退役，不可回滚")
            if target.get("protocol_version") != active_contract["protocol_version"]:
                raise BotError(
                    "protocol_incompatible", "该版本协议与当前游戏规则不兼容"
                )
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
            try:
                result = self.store.set_current_version(bot_id, version)
            except ValueError as exc:
                raise BotError("protocol_incompatible", str(exc)) from exc
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
            if self.store.bot_has_cutover_audit_versions(bot_id):
                raise BotError(
                    "audit_version_retained",
                    "规则迁移版本及其二进制是审计证据，禁止清理",
                )
            dest = self.upload_root / str(bot_id)
            if dest.exists():
                shutil.rmtree(dest, ignore_errors=True)

    def list_mine(
        self, owner_id: int, *, game_id: str | None = None,
        page: int | None = None, per_page: int = 50,
    ) -> list[dict] | dict:
        return self.store.list_bots(
            owner_id=owner_id,
            active_only=False,
            game_id=game_id,
            page=page,
            per_page=per_page,
        )

    def list_public(
        self, *, game_id: str | None = None, owner_id: int | None = None,
        page: int | None = None, per_page: int = 50,
    ) -> list[dict] | dict:
        # 私有 Bot 功能已下线；公开候选只能包含现行可执行目标。
        return self.store.list_bots(
            game_id=game_id,
            owner_id=owner_id,
            runnable_only=True,
            page=page,
            per_page=per_page,
        )

    def get(self, bot_id: int) -> dict | None:
        return self.store.get_bot(bot_id)

    def _require_activatable(self, bot: dict) -> None:
        """Validate the current mirror before any owner/admin activation."""
        from bzplat.backend.store.schema import (
            SUPPORTED_BINARY_ARCH,
            SUPPORTED_BINARY_ERROR,
            SUPPORTED_BINARY_FORMAT,
            SUPPORTED_BINARY_OS,
        )
        if (
            bot.get("format") != SUPPORTED_BINARY_FORMAT
            or bot.get("os") != SUPPORTED_BINARY_OS
            or bot.get("arch") != SUPPORTED_BINARY_ARCH
        ):
            raise BotError("unsupported_binary", SUPPORTED_BINARY_ERROR)
        contract = self.store.get_active_game_contract(bot["game_id"])
        if bot.get("protocol_version") != contract["protocol_version"]:
            raise BotError(
                "protocol_incompatible", "Bot 当前版本协议与游戏规则不兼容"
            )
        current = self.store.get_current_bot_version(int(bot["id"]))
        if current is not None and current.get("retired_at") is not None:
            raise BotError("version_retired", "Bot 当前版本已退役")
        try:
            with Path(bot["binary_path"]).open("rb") as binary:
                require_supported_binary(classify_binary(binary.read(4096)))
        except BinaryRejectError as exc:
            raise BotError("unsupported_binary", str(exc)) from exc
        except OSError as exc:
            raise BotError("version_unavailable", "Bot 二进制文件不可用") from exc

    def set_active(self, bot_id: int, owner_id: int, active: bool) -> dict:
        return self.patch_owner(bot_id, owner_id, is_active=1 if active else 0)

    def select_ranked(self, bot_id: int, owner_id: int) -> dict:
        """Select one active executable Bot as the owner's game representative."""
        with self._bot_version_lock(bot_id):
            bot = self.store.get_bot(bot_id)
            if not bot:
                raise BotError("not_found", "bot 不存在")
            if int(bot["owner_id"]) != int(owner_id):
                raise BotError("forbidden", "无权修改他人的 Bot")
            if not bot.get("is_active"):
                raise BotError("ranking_unavailable", "Bot 当前未启用，不能参加排位")
            self._require_activatable(bot)
            try:
                return self.store.select_ranked_bot(owner_id, bot_id)
            except LookupError as exc:
                raise BotError("not_found", str(exc)) from exc
            except PermissionError as exc:
                raise BotError("forbidden", str(exc)) from exc
            except RankedBotSelectionBusyError as exc:
                raise BotError("ranking_busy", str(exc)) from exc
            except ValueError as exc:
                raise BotError("ranking_unavailable", str(exc)) from exc

    def clear_ranked(self, bot_id: int, owner_id: int) -> dict:
        """Withdraw the current representative without deleting its history."""
        try:
            return self.store.clear_ranked_bot(owner_id, bot_id)
        except LookupError as exc:
            raise BotError("not_found", str(exc)) from exc
        except PermissionError as exc:
            raise BotError("forbidden", str(exc)) from exc
        except RankedBotSelectionBusyError as exc:
            raise BotError("ranking_busy", str(exc)) from exc

    def patch_owner(self, bot_id: int, owner_id: int, **fields) -> dict:
        """Apply an owner edit without allowing activation of an unsupported file."""
        with self._bot_version_lock(bot_id):
            bot = self.store.get_bot(bot_id)
            if not bot:
                raise BotError("not_found", "bot 不存在")
            if bot["owner_id"] != owner_id:
                raise BotError("forbidden", "无权修改他人的 Bot")
            if bool(fields.get("is_active")):
                self._require_activatable(bot)
            result = self.store.update_bot(bot_id, **fields)
            if result is None:
                raise BotError("not_found", "bot 不存在")
            return result

    def patch_admin(self, bot_id: int, **fields) -> dict:
        """Apply an admin edit without bypassing the executable-target gate."""
        with self._bot_version_lock(bot_id):
            bot = self.store.get_bot(bot_id)
            if not bot:
                raise BotError("not_found", "bot 不存在")
            if bool(fields.get("is_active")):
                self._require_activatable(bot)
            result = self.store.update_bot(bot_id, **fields)
            if result is None:
                raise BotError("not_found", "bot 不存在")
            return result
