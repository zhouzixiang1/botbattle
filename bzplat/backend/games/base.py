"""游戏注册表框架——平台与具体游戏之间的唯一契约。

设计目标（全面解耦）：每款游戏是一个完全自包含的包（引擎/协议/结果/段位/
配置/模板各 own 一份），平台通用层（编排 / 赛制 / 评分 / DB）只依赖本模块定义
的 ``GameSpec`` 接口，通过 ``GameRegistry`` 单例按 ``game_id`` 取到 spec，再
调用 spec 上的能力。**禁止通用层出现 ``if game_id == ...`` 分支**——所有游戏
差异都封装在各自的 spec 里。

新增一款游戏 = 新建 ``games/<game>/`` 包（填 engine/protocol/result/tiers/
config/templates/spec），在 ``games/__init__.py`` 注册一行——通用层零改动。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol


# ── 平台契约（鸭子类型，不强制继承）──────────────────────────────
# 每款游戏的结果类（各自的 result.py）须满足以下结构（字段名/语义）：
#   rounds_played: int                 # holdem=手数；棋类=步数（仅显示用）
#   rounds: list[<该游戏的 RoundResult>]  # 每轮含 winners(list[int]) + deltas(list[int])
#   events: list[dict]                 # 事件流（回放/SSE 用）
#   winner: int | None (property)      # 单局棋类取该轮胜者；多手扑克 None
# 平台通用层只读 winners/deltas/rounds_played，绝不触碰游戏专属字段。
# 本模块不 import 任何 result 基类——满足"不要共享"。

DecideFn = Callable[[int, dict[str, Any]], Any]
EventFn = Callable[[str, dict[str, Any]], Any]


class SessionFactory(Protocol):
    """构造并运行一局对局的协程工厂：spec.session_factory(decide, **params) → 结果对象。"""

    async def __call__(self, decide: DecideFn, **params: Any) -> Any: ...


@dataclass(frozen=True)
class ProtocolSpec:
    """一款游戏的 Bot 行协议（序列化/反序列化/超时兜底响应）。

    各游戏的 protocol.py 独立实现这三个函数（holdem 用紧凑 JSON 动作协议；
    gomoku/pencil 各自一份 board 协议副本，互不共享）。
    """

    dumps_request: Callable[[dict[str, Any]], str]
    loads_response: Callable[[str], dict[str, Any]]
    fail_response: Callable[[], dict[str, Any]]


@dataclass(frozen=True)
class TierDef:
    """一款游戏的单个段位定义（per-game 段位曲线的一档）。"""

    level: int          # 0-based 序号（用于 gating 推导）
    key: str            # 英文 key
    name: str           # 中文段位名
    color: str          # tailwind 文字色类
    bg: str             # tailwind 背景色类（浅）
    min_rating: float   # 该段位最低 rating（含）


@dataclass(frozen=True)
class JudgeParamSpec:
    """一款游戏的一个可调裁判参数（admin 可在前端调，热生效）。"""

    setting_key: str    # platform_settings 的 key
    label: str          # 显示名
    field: str          # 对应 run_session 的 kwarg 名（如 starting_stack/board_size）
    default: int        # 默认值（与引擎常量对齐）
    bounds: tuple[int, int]  # (min, max)


@dataclass(frozen=True)
class GameSpec:
    """一款游戏的全部固有属性声明——"游戏类"模型的核心。

    新增游戏 = 实例化一个 GameSpec 并注册。通用层绝不 import 具体游戏模块，
    只经 registry.get(game_id) 取 spec 调用其能力。
    """

    game_id: str
    label: str

    # 裁判引擎
    session_factory: SessionFactory

    # Bot 行协议（本游戏独有，不共享）
    protocol: ProtocolSpec

    # 编排特化（消除 orchestrator 里的 holdem if 分支）
    default_match_params: dict[str, Any]
    validate_match_params: Callable[[dict[str, Any]], dict[str, Any]]
    rounds_per_match: Callable[[dict[str, Any]], int]      # holdem=match_config["hands"]；棋类=1
    normalize_earnings: Callable[[int], float]             # holdem: ea/100.0；棋类: float(ea)
    eta_per_match_sec: float                               # ETA 启发式（estimate 用）
    judge_params: list[JudgeParamSpec] = field(default_factory=list)

    # 段位曲线（完全 per-game，替代全局 engine/tiers.py）
    tiers: list[TierDef] = field(default_factory=list)
    tier_for: Callable[[float | int | None], TierDef] | None = None

    # 赛事模板（本游戏的 DEFAULT_TEMPLATES 条目）
    templates: list[dict[str, Any]] = field(default_factory=list)
    default_scoring: str = "poker_3_1_0"

    # 管理端元信息（admin /api/admin/judges 展示用）
    code_path: str = ""
    summary: str = ""

    # 前端模块路径（lazy 加载该游戏的前端包，如 '@/games/holdem'）
    frontend_module: str = ""

    def run_session(
        self, decide: DecideFn, *, on_event: EventFn | None = None, **params: Any
    ) -> Awaitable[Any]:
        """构造本游戏 Session 并 run_async(decide)。通用层经此入口，不直接 new Session。"""
        return self.session_factory(decide, on_event=on_event, **params)


class GameRegistry:
    """游戏注册表单例——所有 game_id dispatch 的单一真相来源。"""

    def __init__(self) -> None:
        self._specs: dict[str, GameSpec] = {}

    def register(self, spec: GameSpec) -> None:
        if spec.game_id in self._specs:
            raise ValueError(f"游戏已注册: {spec.game_id}")
        self._specs[spec.game_id] = spec

    def get(self, game_id: str) -> GameSpec:
        """取 spec；未知 game_id 抛 KeyError（不再静默兜底 holdem——行为修正）。"""
        gid = self.normalize(game_id)
        try:
            return self._specs[gid]
        except KeyError:
            raise KeyError(f"未注册的游戏: {game_id!r}（合法: {sorted(self._specs)}）") from None

    def all_ids(self) -> frozenset[str]:
        return frozenset(self._specs)

    def is_registered(self, game_id: str) -> bool:
        return self.normalize(game_id) in self._specs

    @staticmethod
    def normalize(game_id: str | None) -> str:
        """规整 game_id（小写、去空白）；**不兜底默认值**——空/未知由调用方决定。

        历史上 normalize_game_id 会把空值兜底成 holdem，这会让"忘记在注册表加新游戏"
        的错误静默退化成跑德州。本框架改为：只做字符串规整，是否兜底由各 dispatch 点
        显式决定（通常未知应报错，而非猜成 holdem）。
        """
        return (game_id or "").strip().lower()

    # ── 便捷函数（通用层经 registry 调用，而非直接 import 具体游戏）──
    def tier_for(self, game_id: str, rating: float | int | None) -> TierDef:
        spec = self.get(game_id)
        if spec.tier_for is not None:
            return spec.tier_for(rating)
        # 无段位曲线的游戏：返回最低档占位（不应发生在已配置段位的游戏上）
        if not spec.tiers:
            return TierDef(0, "novice", "新手", "text-sky-700", "bg-sky-50", 0)
        return spec.tiers[-1]

    def tier_dict(self, game_id: str, rating: float | int | None) -> dict:
        t = self.tier_for(game_id, rating)
        return {
            "level": t.level, "key": t.key, "name": t.name,
            "color": t.color, "bg": t.bg, "min_rating": t.min_rating,
        }

    def all_tiers(self, game_id: str) -> list[dict]:
        spec = self.get(game_id)
        return [
            {"level": t.level, "key": t.key, "name": t.name,
             "color": t.color, "bg": t.bg, "min_rating": t.min_rating}
            for t in spec.tiers
        ]

    def judge_games(self) -> list[dict[str, Any]]:
        """聚合所有游戏的管理端裁判元信息（替代 api_routes 里手写的 JUDGE_GAMES）。"""
        out: list[dict[str, Any]] = []
        for spec in self._specs.values():
            out.append({
                "game_id": spec.game_id,
                "label": spec.label,
                "code_path": spec.code_path,
                "summary": spec.summary,
                "params": [
                    {"key": p.setting_key, "label": p.label, "field": p.field}
                    for p in spec.judge_params
                ],
            })
        return out

    def judge_param_table(self) -> tuple[dict[str, int], dict[str, tuple[int, int]]]:
        """返回 (defaults, bounds) 两张查表，键为 setting_key；供 admin 端点派生。"""
        defaults: dict[str, int] = {}
        bounds: dict[str, tuple[int, int]] = {}
        for spec in self._specs.values():
            for p in spec.judge_params:
                defaults[p.setting_key] = p.default
                bounds[p.setting_key] = p.bounds
        return defaults, bounds


# 全局单例（games/__init__.py 实例化并注册三款游戏）
