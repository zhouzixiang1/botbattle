"""统一日志配置。

所有模块用 ``logging.getLogger(__name__)`` 即可落盘到 ``logs/app.log`` + 控制台。
格式：``时间 级别 [模块] 消息``，便于排查对局/bot/auto-match/WS 问题。
"""
from __future__ import annotations

import logging
import logging.config
import os
from pathlib import Path

DEFAULT_LOG_DIR = "logs"
# 统一格式：带时间戳、级别、模块名
_FMT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(log_dir: str | os.PathLike[str] | None = None, level: str = "INFO") -> None:
    """配置根 logger：文件（轮转）+ 控制台。幂等，可重复调用。"""
    ld = Path(log_dir or os.environ.get("BZ_LOG_DIR", DEFAULT_LOG_DIR))
    try:
        ld.mkdir(parents=True, exist_ok=True)
    except OSError:
        ld = Path(".")
    app_log = str(ld / "app.log")

    root_level = getattr(logging, level.upper(), logging.INFO)
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
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
                },
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "standard",
                    "level": root_level,
                },
            },
            "root": {
                "level": root_level,
                "handlers": ["file", "console"],
            },
            # uvicorn 访问日志降噪（访问日志仍走 uvicorn 默认，但 app 逻辑落到 app.log）
            "loggers": {
                "uvicorn": {"level": "INFO", "handlers": ["file", "console"], "propagate": False},
                "uvicorn.access": {"level": "INFO", "handlers": ["file", "console"], "propagate": False},
            },
        }
    )


__all__ = ["setup_logging"]
