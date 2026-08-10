"""平台运行参数的代码唯一真相源。

这些值随代码评审、发布生效。生产运行路径不得从 ``platform_settings``、
管理端请求或前端状态覆盖它们。测试可把显式配置对象注入对应调度器，但不会
改变生产默认值。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo


CONFIGURATION_SOURCE = "code"
PLATFORM_TIMEZONE_NAME = "Asia/Shanghai"


def platform_local_day(now: datetime | None = None) -> str:
    """Return the platform calendar day for durable daily limits.

    ``now`` is injectable for boundary tests.  Production uses an aware UTC
    instant and converts it explicitly instead of depending on the host's
    process timezone.
    """
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise ValueError("platform_local_day requires a timezone-aware datetime")
    return instant.astimezone(ZoneInfo(PLATFORM_TIMEZONE_NAME)).date().isoformat()

# 对局/赛事通用运行参数。
ACTION_TIMEOUT_SEC = 60.0
MAX_CONCURRENT_MATCHES = 2
FULL_RR_MAX_N = 12

# 人类对战运行参数；由 orchestrator 消费，统一放在这里防止散落字面量。
HUMAN_MAX_CONCURRENT_MATCHES = 4
HUMAN_ACTION_TIMEOUT_SEC = 120.0
HUMAN_MAX_CONSECUTIVE_TIMEOUTS = 5


@dataclass(frozen=True, slots=True)
class AutoMatchConfig:
    """闲时天梯调度的不可变代码配置。"""

    enabled: bool = True
    interval: int = 30
    min_idle: int = 5
    cooldown: int = 600
    stale: int = 3600
    reserve: int = 1
    placement_games: int = 10
    max_per_round: int = 2
    daily_cap: int = 200

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ContestSchedulerConfig:
    """赛事时间调度器的不可变代码配置。"""

    enabled: bool = True
    interval: int = 15

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


AUTO_MATCH_CONFIG = AutoMatchConfig()
# 隔离 QA 需要可重复、无后台写竞态的运行时。它仍是代码配置，不接受环境变量
# 覆盖具体参数；BZ_QA_INSTANCE 只负责选择这个固定 profile。生产 profile 及
# 并发/资源契约完全不变。
QA_AUTO_MATCH_CONFIG = replace(AUTO_MATCH_CONFIG, enabled=False)
CONTEST_SCHEDULER_CONFIG = ContestSchedulerConfig()


__all__ = [
    "ACTION_TIMEOUT_SEC",
    "AUTO_MATCH_CONFIG",
    "AutoMatchConfig",
    "CONFIGURATION_SOURCE",
    "CONTEST_SCHEDULER_CONFIG",
    "ContestSchedulerConfig",
    "FULL_RR_MAX_N",
    "HUMAN_ACTION_TIMEOUT_SEC",
    "HUMAN_MAX_CONCURRENT_MATCHES",
    "HUMAN_MAX_CONSECUTIVE_TIMEOUTS",
    "MAX_CONCURRENT_MATCHES",
    "PLATFORM_TIMEZONE_NAME",
    "QA_AUTO_MATCH_CONFIG",
    "platform_local_day",
]
