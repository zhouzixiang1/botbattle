"""运行时资源硬顶：半负载并发 ceiling。

每场对局 = 2 个 bot 容器 × 1 核 → 满载对局数 full = max(1, cpu_count // 2)。
半负载硬顶 ceiling = max(1, full // 2) = max(1, cpu_count // 4)。
"""
from __future__ import annotations

import os

# 硬编码只读资源（admin 不可抬高）
BOT_CPUS = 1.0
BOT_MEMORY_MB = 512

ACTION_TIMEOUT_MIN = 1
ACTION_TIMEOUT_MAX = 300
FULL_RR_MAX_N = 12  # 全员单/双循环人数上限


def cpu_count() -> int:
    return os.cpu_count() or 1


def concurrent_ceiling() -> int:
    """半负载并发硬顶：max(1, cpu_count // 4)。"""
    return max(1, cpu_count() // 4)


def clamp_concurrent(requested: int) -> int:
    return max(1, min(int(requested), concurrent_ceiling()))


def default_max_concurrent() -> int:
    return min(2, concurrent_ceiling())
