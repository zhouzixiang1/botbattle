"""Fail-closed isolation checks shared by the QA server and mutating scripts."""
from __future__ import annotations

from pathlib import Path


_PRIMARY_RUNTIME_DIRS = ("bot_uploads", "avatars", "logs")
_PRIMARY_SERVICE_PORT = 50380
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def qa_instance_enabled(value: str | None) -> bool:
    """Parse ``BZ_QA_INSTANCE`` identically in every startup layer."""
    return (value or "").strip().lower() in _TRUE_VALUES


def primary_checkout_root(root: Path) -> Path | None:
    """Resolve the primary checkout for a normal or linked Git worktree.

    Existing but malformed Git metadata raises instead of weakening the guard.
    """
    root = root.resolve()
    marker = root / ".git"
    linked = marker.is_file()

    if marker.is_dir():
        git_dir = marker.resolve()
    elif linked:
        try:
            label, separator, raw_path = marker.read_text(encoding="utf-8").strip().partition(":")
        except OSError as exc:
            raise RuntimeError(f"无法读取 Git worktree 指针：{marker}") from exc
        if separator != ":" or label.strip().lower() != "gitdir" or not raw_path.strip():
            raise RuntimeError(f"无法解析 Git worktree 指针：{marker}")
        pointer = Path(raw_path.strip())
        git_dir = (pointer if pointer.is_absolute() else marker.parent / pointer).resolve()
        if not git_dir.is_dir():
            raise RuntimeError(f"Git worktree 元数据目录不存在：{git_dir}")
    else:
        return None

    common_marker = git_dir / "commondir"
    if common_marker.is_file():
        try:
            raw_common = common_marker.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError(f"无法读取 Git commondir：{common_marker}") from exc
        if not raw_common:
            raise RuntimeError(f"Git commondir 为空：{common_marker}")
        common = Path(raw_common)
        common_git_dir = (common if common.is_absolute() else git_dir / common).resolve()
    elif linked:
        raise RuntimeError(f"linked worktree 缺少 Git commondir：{common_marker}")
    else:
        common_git_dir = git_dir

    if not common_git_dir.is_dir():
        raise RuntimeError(f"Git common directory 不存在：{common_git_dir}")
    if common_git_dir.name != ".git":
        raise RuntimeError(f"无法从 Git common directory 推导主 checkout：{common_git_dir}")
    return common_git_dir.parent.resolve()


def assert_qa_database_isolated(db_path: str | Path, source_root: Path) -> Path:
    """Reject the primary checkout's truth-source DB by path or inode.

    This runs before ``Store`` opens the file, so a forged ``BZ_QA_INSTANCE=1``
    marker can never turn the production database into a writable QA target.
    """
    candidate = Path(db_path).expanduser().resolve()
    source = source_root.resolve()
    primary_root = primary_checkout_root(source_root)
    if primary_root is None:
        return candidate
    truth = (primary_root / "botzone.db").resolve()
    same_file = False
    if candidate.exists() and truth.exists():
        try:
            same_file = candidate.samefile(truth)
        except OSError:
            same_file = False
    if candidate == truth or same_file:
        raise RuntimeError(
            "BZ_QA_INSTANCE 拒绝使用主 checkout 的 botzone.db；"
            "请复制到 linked worktree 并显式设置 BZ_DB_PATH"
        )
    in_primary_checkout = candidate == primary_root or primary_root in candidate.parents
    in_current_linked_worktree = (
        source != primary_root
        and (candidate == source or source in candidate.parents)
    )
    if in_primary_checkout and not in_current_linked_worktree:
        raise RuntimeError(
            "BZ_QA_INSTANCE 拒绝在主 checkout 工作树创建或迁移数据库；"
            "请使用当前 linked worktree 或独立临时目录"
        )
    return candidate


def assert_qa_runtime_path_isolated(
    runtime_path: str | Path,
    source_root: Path,
    *,
    purpose: str = "QA 运行时产物",
) -> Path:
    """Reject every primary-checkout mutable runtime directory.

    DB-direct scripts often run with a temporary database while their process CWD
    still points at a checkout.  A relative ``bot_uploads``/``avatars``/``logs``
    path would then mutate the checkout despite the database itself being isolated.
    Resolve the candidate before any writer is constructed and reject production
    directories, descendants and symlink aliases fail-closed.
    """
    candidate = Path(runtime_path).expanduser().resolve()
    source = source_root.resolve()
    primary_root = primary_checkout_root(source_root)
    if primary_root is None:
        return candidate
    for dirname in _PRIMARY_RUNTIME_DIRS:
        truth = (primary_root / dirname).resolve()
        same_file = False
        if candidate.exists() and truth.exists():
            try:
                same_file = candidate.samefile(truth)
            except OSError:
                same_file = False
        if candidate == truth or truth in candidate.parents or same_file:
            raise RuntimeError(
                f"{purpose}拒绝使用主 checkout 的 {dirname}；"
                "请把运行时产物钉到隔离数据库旁或独立临时目录"
            )
    in_primary_checkout = candidate == primary_root or primary_root in candidate.parents
    in_current_linked_worktree = (
        source != primary_root
        and (candidate == source or source in candidate.parents)
    )
    if in_primary_checkout and not in_current_linked_worktree:
        raise RuntimeError(
            f"{purpose}拒绝使用主 checkout 工作树；"
            "请把运行时产物钉到当前 linked worktree 或独立临时目录"
        )
    return candidate


def assert_qa_upload_root_isolated(upload_root: str | Path, source_root: Path) -> Path:
    """Reject production uploads, avatars and logs as a QA upload destination."""
    return assert_qa_runtime_path_isolated(
        upload_root,
        source_root,
        purpose="BZ_QA_INSTANCE ",
    )


def assert_qa_server_startup_isolated(
    *,
    port: int,
    db_path: str | Path,
    log_dir: str | Path,
    avatar_dir: str | Path,
    source_root: Path,
    cwd: Path,
) -> tuple[Path, Path, Path]:
    """Validate every early-writing QA server target without touching disk.

    ``logging_config.setup_logging`` creates three files and ``Store`` may create
    or migrate its database.  The CLI therefore calls this pure resolver before
    either component is constructed.  Relative environment values intentionally
    follow the server process CWD, matching their eventual consumers.
    """
    if port == _PRIMARY_SERVICE_PORT:
        raise RuntimeError(
            "BZ_QA_INSTANCE 拒绝绑定 50380 main 服务端口；请使用 worktree 独立端口"
        )

    base = cwd.expanduser().resolve()

    def from_cwd(raw: str | Path) -> Path:
        candidate = Path(raw).expanduser()
        return (candidate if candidate.is_absolute() else base / candidate).resolve()

    database = assert_qa_database_isolated(from_cwd(db_path), source_root)
    logs = assert_qa_runtime_path_isolated(
        from_cwd(log_dir), source_root, purpose="BZ_QA_INSTANCE 日志目录"
    )
    avatars = assert_qa_runtime_path_isolated(
        from_cwd(avatar_dir), source_root, purpose="BZ_QA_INSTANCE 头像目录"
    )
    return database, logs, avatars
