"""运行时资源硬顶：全局单场执行与 Bot 上传容量。"""
from __future__ import annotations

import os

from bzplat.backend.runtime.config import FULL_RR_MAX_N, MAX_CONCURRENT_MATCHES

# 硬编码只读资源（admin 不可抬高）
BOT_CPUS = 1.0
BOT_MEMORY_MB = 512
# 上传会在进程内保留一份 bytes，并由进程级 admission 串行预检。100 MiB
# 足以覆盖常见 PyInstaller 单文件产物，同时避免无界内存/磁盘占用。
MAX_BOT_UPLOAD_BYTES = 100 * 1024 * 1024
# 单次 Bot stdout 响应行的传输硬顶。StreamReader 与协议解析共用同一常量，
# 防止超长无换行输出先撑大进程内存、随后才在业务层判错。
MAX_BOT_RESPONSE_LINE_BYTES = 64 * 1024


def cpu_count() -> int:
    return os.cpu_count() or 1


def concurrent_ceiling() -> int:
    """全站执行硬顶：所有来源同一时刻合计最多一场。"""
    return min(MAX_CONCURRENT_MATCHES, max(1, cpu_count() // 4))


def clamp_concurrent(requested: int) -> int:
    return max(1, min(int(requested), concurrent_ceiling()))


def default_max_concurrent() -> int:
    return min(MAX_CONCURRENT_MATCHES, concurrent_ceiling())
