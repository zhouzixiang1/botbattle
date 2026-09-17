"""用户云存储 blob 层：内容寻址实体文件的落盘、晋升与回收。

清单（命名条目、配额）在 Store 的 ``user_storage_files`` 表；本模块只管
数据库旁 ``user_assets/<user_id>/<sha256>`` 的文件实体：

- 同一用户内相同内容只存一份（跨用户不共享，避免存在性侧信道）；
- 上传先写暂存目录，经 Store 事务确认配额后原子晋升为 blob；
- 删除清单行后，若内容不再被该用户任何条目引用则回收文件。
"""
from __future__ import annotations

import hashlib
import logging
import re
import shutil
import tempfile
from pathlib import Path

from bzplat.backend.store import Store

logger = logging.getLogger(__name__)

STORAGE_ROOT = Path("user_assets")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# 文件名：不允许路径分隔符与控制字符；长度 1..200；显式排除 '.'/'..'。
_NAME_RE = re.compile(r"^[^/\x00-\x1f\x7f]{1,200}$")
_FORBIDDEN_NAMES = {".", ".."}


class UserStorageError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def validate_storage_name(raw: object) -> str:
    """归一并校验用户文件名；非法即抛 UserStorageError。"""
    name = str(raw or "").strip()
    if (
        not name
        or name in _FORBIDDEN_NAMES
        or "\\" in name
        or not _NAME_RE.match(name)
    ):
        raise UserStorageError(
            "invalid_name",
            "文件名须为 1–200 个字符，且不能包含路径分隔符或控制字符",
        )
    return name


class UserStorageManager:
    def __init__(
        self,
        store: Store,
        *,
        root: Path | str = STORAGE_ROOT,
        create_root: bool = True,
    ) -> None:
        self.store = store
        self.root = Path(root)
        if create_root:
            self.root.mkdir(mode=0o755, parents=True, exist_ok=True)
        if self.root.is_symlink() or not self.root.is_dir():
            raise UserStorageError(
                "unsafe_storage_root", "canonical user_assets 创建失败"
            )

    # ---- 暂存与晋升 ----

    def new_staging(self) -> Path:
        """上传流式写入的暂存目录（与 blob 同根，保证可原子 rename）。"""
        return Path(tempfile.mkdtemp(prefix=".incoming-", dir=self.root))

    def blob_path(self, user_id: int, sha256: str) -> Path:
        if not _SHA256_RE.match(sha256 or ""):
            raise UserStorageError("invalid_checksum", "内容指纹不合法")
        return self.root / str(int(user_id)) / sha256

    def promote_staged(
        self, staged_file: Path, user_id: int, sha256: str
    ) -> Path:
        """把暂存文件原子晋升为该用户的内容寻址 blob（幂等）。

        无条件 replace 而不是复用旧实体：同指纹即同内容，覆盖无害，而
        rename 刷新的 mtime 正是延迟回收的“最近晋升”空闲证据——复用旧
        实体会让删除后再次上传同内容的 blob 保留陈旧 mtime，在宽限期
        判定上被误当作长期无引用。已建立的对局硬链接持有旧 inode，
        不受目录项替换影响。
        """
        target = self.blob_path(user_id, sha256)
        target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        staged_file.replace(target)
        return target

    @staticmethod
    def drop_staging(staging_dir: Path) -> None:
        shutil.rmtree(staging_dir, ignore_errors=True)

    # ---- 延迟回收 ----

    def sweep_unreferenced(self, *, min_age_seconds: float = 3600.0) -> dict:
        """回收无清单引用且文件空闲超过宽限期的实体与暂存残留。

        清单是唯一权威：任何在途上传在提交后必有清单行。两道防线保证
        周期执行（与并发上传交错）时的安全：其一，promote 的原子 replace
        会刷新 mtime，引用恢复的 blob 必然重新进入宽限期；其二，unlink
        前按 ``(user_id, sha)`` 单事务复核引用计数，覆盖快照与删除之间的
        窗口。同时清掉崩溃遗留的 ``.incoming-*``。返回
        {blobs, staging, bytes}。
        """
        import time as _time

        cutoff = _time.time() - min_age_seconds
        referenced = self.store.all_user_storage_shas()
        removed_blobs = 0
        removed_bytes = 0
        removed_staging = 0
        try:
            entries = list(self.root.iterdir())
        except OSError:
            return {"blobs": 0, "staging": 0, "bytes": 0}
        for entry in entries:
            try:
                if entry.name.startswith(".incoming-"):
                    if entry.is_dir() and entry.stat().st_mtime < cutoff:
                        shutil.rmtree(entry, ignore_errors=True)
                        removed_staging += 1
                    continue
                if not entry.is_dir():
                    continue
                try:
                    user_id = int(entry.name)
                except ValueError:
                    continue
                keep = referenced.get(user_id, set())
                for blob in entry.iterdir():
                    try:
                        stat = blob.stat()
                    except OSError:
                        continue
                    if blob.name in keep or stat.st_mtime >= cutoff:
                        continue
                    # 快照读与 unlink 之间可能恰有并发上传提交了引用该
                    # 内容的清单行；删除前按权威清单单事务复核。
                    if self.store.count_user_storage_sha_references(
                        user_id, blob.name
                    ):
                        continue
                    try:
                        blob.unlink()
                        removed_blobs += 1
                        removed_bytes += stat.st_size
                    except OSError as exc:
                        logger.warning(
                            "user storage sweep unlink failed path=%s error=%s",
                            blob, exc,
                        )
                # 用户目录清空后顺手移除（硬删用户的最终回收）。
                try:
                    entry.rmdir()
                except OSError:
                    pass
            except OSError:
                continue
        return {
            "blobs": removed_blobs,
            "staging": removed_staging,
            "bytes": removed_bytes,
        }
