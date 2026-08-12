"""统一日志配置。

所有模块用 ``logging.getLogger(__name__)`` 即可落盘到 ``logs/app.log`` + 控制台。
格式：``时间 级别 [模块] 消息``，便于排查对局/bot/auto-match/WS 问题。

三套独立日志文件（公网暴露后用于排查与安全审计）：
- ``logs/app.log``：业务/系统日志（对局、bot、auto-match、异常等）。
- ``logs/access.log``：HTTP 访问日志（由 AccessLogMiddleware 写入，含真实客户端 IP）。
- ``logs/audit.log``：安全审计日志（敏感操作：登录/注册/上传/admin 写等，含 actor+IP+结果）。
"""
from __future__ import annotations

import logging
import logging.config
import os
import re
from pathlib import Path

from bzplat.backend.qa_safety import qa_instance_enabled

DEFAULT_LOG_DIR = "logs"
# 统一格式：带时间戳、级别、模块名
_FMT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

# access / audit logger 名（消息体本身含结构化字段 ip=... method=... action=... 等）
ACCESS_LOGGER = "bzplat.access"
AUDIT_LOGGER = "bzplat.audit"

# Uvicorn builds its HTTP and WebSocket access messages from positional args:
# HTTP uses ``(client, method, path_with_query, version, status)`` on
# ``uvicorn.access`` while WebSocket uses ``(client, path_with_query[, status])``
# on ``uvicorn.error``.  Keep that stable metadata but project the request target
# to its path before any handler serializes it.  The defensive regex also covers
# an already-rendered Uvicorn message without guessing secret parameter names.
_REQUEST_TARGET_QUERY_RE = re.compile(r'(?P<path>/[^\s"?]*)\?[^\s"]*')


def _path_only(value: object) -> object:
    if not isinstance(value, str):
        return value
    path, separator, _query = value.partition("?")
    if separator and path.startswith("/"):
        return path or "/"
    return value


def _strip_rendered_request_queries(value: object) -> object:
    if not isinstance(value, str):
        return value
    return _REQUEST_TARGET_QUERY_RE.sub(r"\g<path>", value)


class UvicornRequestTargetFilter(logging.Filter):
    """Remove request queries from every serialized Uvicorn HTTP/WS record."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not (record.name == "uvicorn" or record.name.startswith("uvicorn.")):
            return True

        if isinstance(record.args, tuple):
            args = list(record.args)
            if record.name == "uvicorn.access" and len(args) >= 3:
                args[2] = _path_only(args[2])
            elif (
                record.name == "uvicorn.error"
                and "WebSocket %s" in str(record.msg)
                and len(args) >= 2
            ):
                args[1] = _path_only(args[1])
            # Defensive support for a compatible Uvicorn formatter that has
            # already combined the request line into one positional value.
            record.args = tuple(_strip_rendered_request_queries(arg) for arg in args)
        elif isinstance(record.args, dict):
            record.args = {
                key: _strip_rendered_request_queries(value)
                for key, value in record.args.items()
            }

        record.msg = _strip_rendered_request_queries(record.msg)
        return True


def setup_logging(log_dir: str | os.PathLike[str] | None = None, level: str = "INFO") -> None:
    """配置根 logger：文件（轮转）+ 控制台。幂等，可重复调用。"""
    ld = Path(log_dir or os.environ.get("BZ_LOG_DIR", DEFAULT_LOG_DIR))
    try:
        ld.mkdir(parents=True, exist_ok=True)
    except OSError:
        # QA startup has already validated an isolated absolute target. Falling
        # back to process CWD here could silently create app/access/audit.log in
        # the primary checkout after that guard passed, so QA must fail closed.
        if qa_instance_enabled(os.environ.get("BZ_QA_INSTANCE")):
            raise
        ld = Path(".")
    app_log = str(ld / "app.log")
    access_log = str(ld / "access.log")
    audit_log = str(ld / "audit.log")

    root_level = getattr(logging, level.upper(), logging.INFO)
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "filters": {
                "uvicorn_path_only": {"()": UvicornRequestTargetFilter},
            },
            "formatters": {
                "standard": {"format": _FMT, "datefmt": _DATEFMT},
            },
            "handlers": {
                "file": {
                    "class": "logging.handlers.RotatingFileHandler",
                    "filename": app_log,
                    "maxBytes": 5 * 1024 * 1024,
                    "backupCount": 5,
                    "encoding": "utf-8",
                    "formatter": "standard",
                    "level": root_level,
                    "filters": ["uvicorn_path_only"],
                },
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "standard",
                    "level": root_level,
                    "filters": ["uvicorn_path_only"],
                },
                # access.log：只落文件（访问量大，不刷控制台）
                "access_file": {
                    "class": "logging.handlers.RotatingFileHandler",
                    "filename": access_log,
                    "maxBytes": 5 * 1024 * 1024,
                    "backupCount": 5,
                    "encoding": "utf-8",
                    "formatter": "standard",
                    "level": logging.INFO,
                },
                # audit.log：落文件 + 控制台（安全事件要可见）
                "audit_file": {
                    "class": "logging.handlers.RotatingFileHandler",
                    "filename": audit_log,
                    "maxBytes": 5 * 1024 * 1024,
                    "backupCount": 5,
                    "encoding": "utf-8",
                    "formatter": "standard",
                    "level": logging.INFO,
                },
            },
            "root": {
                "level": root_level,
                "handlers": ["file", "console"],
            },
            "loggers": {
                # Uvicorn HTTP/WS 共用平台 handler，确保 query 投影在序列化前生效。
                "uvicorn": {"level": "INFO", "handlers": ["file", "console"], "propagate": False},
                "uvicorn.error": {"level": "INFO", "handlers": ["file", "console"], "propagate": False},
                "uvicorn.access": {"level": "INFO", "handlers": ["file", "console"], "propagate": False},
                # access / audit 独立 logger，propagate=False 不污染 root(app.log)
                ACCESS_LOGGER: {"level": "INFO", "handlers": ["access_file"], "propagate": False},
                AUDIT_LOGGER: {"level": "INFO", "handlers": ["audit_file", "console"], "propagate": False},
            },
        }
    )


__all__ = [
    "setup_logging",
    "ACCESS_LOGGER",
    "AUDIT_LOGGER",
    "UvicornRequestTargetFilter",
]
