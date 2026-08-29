"""平台运行参数的代码唯一真相源。

这些值随代码评审、发布生效。生产运行路径不得从 ``platform_settings``、
管理端请求或前端状态覆盖它们。测试可把显式配置对象注入对应调度器，但不会
改变生产默认值。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


CONFIGURATION_SOURCE = "code"

# 对局/赛事通用运行参数。全站所有来源共享最多六个对局槽；
# 实际可同时启动的数量还必须通过进程可见 CPU/内存与每个 job 入队时
# 冻结的资源向量准入。当前主机上六场最重赛事对局合计为
# 24 vCPU / 24 GiB，保留了 API、SQLite、Docker 与上传预检余量。主机
# 资源门可继续收紧，管理员与显式参数不能放大六槽硬顶。
ACTION_TIMEOUT_SEC = 60.0
MAX_CONCURRENT_MATCHES = 6
# 全员单/双循环不再按参赛人数拒绝发布。实际并发仍由 match slots 与
# sandbox capacity 硬顶控制，超大赛程只会进入持久队列，不会放大物理并发。
# 保留该公开配置键并以 ``None`` 明确表示“不限人数”，避免旧诊断客户端
# 把字段缺失误解成接口损坏。
FULL_RR_MAX_N: int | None = None

# Bot 上传从读取请求文件到隐藏版本预检完成共用一个全局槽。等待超过
# 一秒即明确返回繁忙，避免不同 Bot 的并发版本上传同时保留大块 raw
# 并写入多个待预检临时目录。
BOT_UPLOAD_ADMISSION_SLOTS = 1
BOT_UPLOAD_ADMISSION_WAIT_SEC = 1.0

# 人类对战回合参数；并发统一由全局 execution queue 控制。
HUMAN_ACTION_TIMEOUT_SEC = 120.0
HUMAN_MAX_CONSECUTIVE_TIMEOUTS = 5


# 全来源执行队列：每个 job 固定占 1 个全局 match slot，Bot-vs-Bot
# 占 2 个 sandbox unit，人机占 1 个。sandbox 容量按实际槽数 * 2 派生。
EXECUTION_AGING_SECONDS = 60
EXECUTION_USER_ACTIVE_LIMIT = 1
EXECUTION_USER_QUEUED_LIMIT = 4
EXECUTION_CONTEST_SHARE_SLOTS = 1
EXECUTION_AUTO_ACTIVE_LIMIT = 1
EXECUTION_AUTO_LOOKAHEAD = 1
EXECUTION_POLL_SECONDS = 1.0


# 自动排位只有一个管理员可变总开关；bootstrap 目标仅用于公平队列让
# 新 Bot 获得冷启动服务，与公开排名资格阈值完全无关。
# 自动候选前瞻与公平选择策略属于 execution queue 的代码常量，不能从
# platform_settings、环境变量或管理端请求形成第二套运行时配置。
AUTO_MATCH_BOOTSTRAP_TARGET_MATCHES = 10
AUTO_MATCH_IDLE_GRACE_SECONDS = 300
AUTO_MATCH_COOLDOWN_SECONDS = 300
AUTO_MATCH_CONTEST_GUARD_SECONDS = 300
AUTO_MATCH_SCHEDULER_POLICY_VERSION = "idle-only-v1"

# 公开排名资格由独立的已计分场次契约控制；与 auto bootstrap 公平通道
# 数值恰好相同也不构成配置耦合。
RANKING_MIN_RATED_MATCHES = 10


@dataclass(frozen=True, slots=True)
class ContestSchedulerConfig:
    """赛事时间调度器的不可变代码配置。"""

    enabled: bool = True
    interval: int = 15

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


CONTEST_SCHEDULER_CONFIG = ContestSchedulerConfig()


__all__ = [
    "ACTION_TIMEOUT_SEC",
    "AUTO_MATCH_BOOTSTRAP_TARGET_MATCHES",
    "AUTO_MATCH_CONTEST_GUARD_SECONDS",
    "AUTO_MATCH_COOLDOWN_SECONDS",
    "AUTO_MATCH_IDLE_GRACE_SECONDS",
    "AUTO_MATCH_SCHEDULER_POLICY_VERSION",
    "BOT_UPLOAD_ADMISSION_SLOTS",
    "BOT_UPLOAD_ADMISSION_WAIT_SEC",
    "CONFIGURATION_SOURCE",
    "CONTEST_SCHEDULER_CONFIG",
    "ContestSchedulerConfig",
    "FULL_RR_MAX_N",
    "EXECUTION_AGING_SECONDS",
    "EXECUTION_AUTO_ACTIVE_LIMIT",
    "EXECUTION_AUTO_LOOKAHEAD",
    "EXECUTION_CONTEST_SHARE_SLOTS",
    "EXECUTION_POLL_SECONDS",
    "EXECUTION_USER_ACTIVE_LIMIT",
    "EXECUTION_USER_QUEUED_LIMIT",
    "HUMAN_ACTION_TIMEOUT_SEC",
    "HUMAN_MAX_CONSECUTIVE_TIMEOUTS",
    "MAX_CONCURRENT_MATCHES",
    "RANKING_MIN_RATED_MATCHES",
]
