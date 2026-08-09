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


# ── 平台契约基类（仅类型提示 / 测试构造 fake result 用，各游戏真 result 类不继承）──
# 全面解耦后各游戏 result.py 独立定义（鸭子契约，不共享基类）。本基类集中"最小契约
# 字段"供测试构造最小 fake result（MatchResult(rounds_played=0) / RoundResult([0],[1,-1])），
# 测通用层只读 winners/deltas/rounds_played/rounds/winner 的契约不变量。
# 各游戏真 result 类（games/<game>/result.py）不继承此类——结构兼容（鸭子类型）。
@dataclass
class RoundResult:
    """单轮结果（类型提示用基类；各游戏独立定义同名类）。"""

    winners: list[int]
    deltas: list[int]


@dataclass
class MatchResult:
    """整场结果（类型提示用基类；各游戏独立定义同名类）。"""

    rounds_played: int
    rounds: list[RoundResult] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def winner(self) -> int | None:
        if len(self.rounds) == 1:
            w = self.rounds[0].winners
            if len(w) == 1:  # 唯一胜者
                return w[0]
            # winners 长度 0 或 >1（split pot 平局）→ 平局
            return None
        return None


DecideFn = Callable[[int, dict[str, Any]], Any]
EventFn = Callable[[str, dict[str, Any]], Any]


class SessionFactory(Protocol):
    """构造并运行一局对局的协程工厂：spec.session_factory(decide, **params) → 结果对象。

    唯一调用点是 ``GameSpec.run_session``，它必传 ``on_event`` 关键字参数，故
    工厂签名须显式声明 ``on_event``（与 run_session 对齐，否则外部按本 Protocol
    实现的工厂会在 run_session 里因收到意外的 on_event kwarg 而崩）。
    """

    async def __call__(
        self,
        decide: DecideFn,
        *,
        on_event: EventFn | None = None,
        **params: Any,
    ) -> Any: ...


@dataclass(frozen=True)
class ProtocolSpec:
    """一款游戏的 Bot 行协议（序列化/反序列化/响应契约/兜底响应）。

    各游戏的 protocol.py 独立实现这三个函数（holdem 用紧凑 JSON 动作协议；
    gomoku/pencil 各自一份 board 协议副本，互不共享）。
    """

    dumps_request: Callable[[dict[str, Any]], str]
    loads_response: Callable[[str], dict[str, Any]]
    # 校验已从 Botzone 信封提取出的 response payload。只校验协议形状/类型；
    # 坐标越界、重复落子、加注额不足等游戏内合法性仍由裁判判定。
    validate_response_payload: Callable[[Any], Any]
    # fail_response 仅供人类超时等游戏内兜底，返回值由各游戏自定
    #（holdem 返裸 int -1=fold；棋类返 dict）。Bot 协议/超时故障不得使用它。
    # 调用方把它传给该游戏的 parse_response/parse_xy，类型在此不强约束。
    fail_response: Callable[[], Any]


@dataclass(frozen=True)
class TierDef:
    """一款游戏的单个段位定义（per-game 段位曲线的一档）。"""

    level: int          # 0-based 序号（用于 gating 推导）
    key: str            # 英文 key
    name: str           # 中文段位名
    color: str          # tailwind 文字色类
    bg: str             # tailwind 背景色类（浅）
    min_rating: float   # 该段位最低 rating（含）


def tier_for_in(rating: float | int | None, tiers: list[TierDef]) -> TierDef:
    """在指定段位曲线里按 rating 查段位（共享查表算法，全面解耦 PR-D）。

    各 games/<game>/tiers.py 的 tier_for 是同一算法——此前各存一份副本（有害重复）。
    本函数集中算法；各 tiers.py 只声明 TIERS 数据列表（曲线数据独立，可分游戏调），
    调 tier_for_in(rating, TIERS) 即可。
    """
    if rating is None:
        return tiers[-1]
    r = float(rating)
    for t in tiers:
        if r >= t.min_rating:
            return t
    return tiers[-1]


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
    eta_for_match: Callable[[dict[str, Any]], int]         # 按 match_config 算每场秒数（取代 if game_id 缩放分支）
    judge_params: list[JudgeParamSpec] = field(default_factory=list)

    # 段位曲线（完全 per-game，替代全局 engine/tiers.py）。查表算法共享 base.tier_for_in。
    tiers: list[TierDef] = field(default_factory=list)

    # 赛事模板（本游戏的 DEFAULT_TEMPLATES 条目）
    templates: list[dict[str, Any]] = field(default_factory=list)
    default_scoring: str = "poker_3_1_0"

    # 管理端元信息（admin /api/admin/judges 展示用）
    code_path: str = ""
    summary: str = ""

    # 公开裁判源码：要对全体玩家公开明文展示的源码文件相对路径（相对 games/<game>/ 包目录）。
    # 裁判是公开可审计的规则定义——源码必须对全体玩家透明（区别于 Bot 的私有黑盒二进制）。
    # 默认由 game_id 派生权威纯规则文件 + 三件套（适配引擎 / 行协议 /
    # 结果契约），GET /api/judges/{game_id}/source 返回。显式覆写仍必须包含
    # <game_id>_judge.py，且只允许包根目录内的 Python 文件名，防止路径穿越。
    source_files: tuple[str, ...] = ()

    # 座位数（2=双人，当前全平台双人；预留 N 人扩展钩子，通用层已声明但 DB/评分仍按 2 人）。
    num_seats: int = 2

    # Bot 预检（上传时试跑：构造首个请求，验证响应合法）——拒绝明显不合格的 bot。
    # 返回 (ok: bool, detail: str)。ok=False 时上传被拒（detail 给前端展示）。
    preflight_check: Callable[[str, Any], Awaitable[tuple[bool, str]]] | None = None
    # P4 duplicate：构造多 leg 对局计划（默认单 leg；holdem 覆写返回 2 leg 同 deal_sequence）。
    # 返回 list[LegSpec]，每 leg 含 seat_swap（是否对调座位）+ 共享 params（deal_sequence 等）。
    build_match_plan: Callable[[int, dict[str, Any]], list[dict[str, Any]]] | None = None
    # 每方总时间预算（秒）；None=不限时（走原单步 action_timeout 超时）。
    # 仅 pencil 设 900.0（象棋钟：每方累计 15 分钟，超时判负）。
    time_budget_per_side: float | None = None

    def __post_init__(self) -> None:
        """派生并校验公开裁判源码白名单。

        公开接口会直接按 ``source_files`` 读取游戏包中的文件，因此安全边界
        收敛在 GameSpec：清单项必须是不含目录分隔符的 ``.py`` 文件名，且必须
        公开该游戏的权威纯规则 ``<game_id>_judge.py``。这既不在通用层枚举
        游戏名，也避免新游戏遗漏真正的规则实现。
        """
        judge_file = f"{self.game_id}_judge.py"
        files = tuple(self.source_files) or (
            judge_file,
            "engine.py",
            "protocol.py",
            "result.py",
        )
        for rel in files:
            if (
                not isinstance(rel, str)
                or not rel
                or rel in {".", ".."}
                or "/" in rel
                or "\\" in rel
                or not rel.endswith(".py")
            ):
                raise ValueError(f"source_files 只允许游戏包根目录的 Python 文件名: {rel!r}")
        if judge_file not in files:
            raise ValueError(f"source_files 必须包含权威纯裁判源码: {judge_file}")
        if len(files) != len(set(files)):
            raise ValueError("source_files 不允许重复文件")
        object.__setattr__(self, "source_files", files)

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
        # 无段位曲线的游戏：返回最低档占位（不应发生在已配置段位的游戏上）
        if not spec.tiers:
            return TierDef(0, "novice", "新手", "text-sky-700", "bg-sky-50", 0)
        # 查表算法共享 base.tier_for_in（各游戏的 tier_for 包装已删除，统一走此）
        return tier_for_in(rating, spec.tiers)

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
        """聚合所有游戏的裁判元信息（供 admin 管理端 + 公开端点共用）。"""
        out: list[dict[str, Any]] = []
        for spec in self._specs.values():
            out.append({
                "game_id": spec.game_id,
                "label": spec.label,
                "code_path": spec.code_path,
                "summary": spec.summary,
                "source_files": list(spec.source_files),
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
