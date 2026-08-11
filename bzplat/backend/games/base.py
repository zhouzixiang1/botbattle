"""游戏注册表框架——平台与具体游戏之间的唯一契约。

设计目标（契约解耦）：每款游戏集中拥有自己的裁判/适配器/协议入口/结果/
模板；其中纯裁判零平台依赖，适配器则有意复用平台运行时故障类型。平台通用层
（编排 / 赛制 / 评分 / DB）只依赖本模块定义的 ``GameSpec`` 接口，通过
``GameRegistry`` 单例按 ``game_id`` 取到 spec，再调用 spec 上的能力。
**禁止通用层出现 ``if game_id == ...`` 分支**——游戏差异封装在各自的 spec 里。

新增一款游戏 = 新建 ``games/<game>/`` 包（填 engine/protocol/result/
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

    各游戏的 protocol.py 只暴露本游戏 API。同构的线协议原语可以调用一份
    经审计的共享实现，但共享源码必须由 ``GameSpec.shared_source_files`` 声明并公开。
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
class GameSpec:
    """一款游戏的全部固有属性声明——"游戏类"模型的核心。

    新增游戏 = 实例化一个 GameSpec 并注册。通用层绝不 import 具体游戏模块，
    只经 registry.get(game_id) 取 spec 调用其能力。
    """

    game_id: str
    label: str

    # 裁判引擎
    session_factory: SessionFactory

    # Bot 行协议（对外入口仅暴露本游戏 API；同构底层原语可共享一份公开实现）
    protocol: ProtocolSpec

    # 编排特化（消除 orchestrator 里的具体游戏分支）
    default_match_params: dict[str, Any]
    validate_match_params: Callable[[dict[str, Any]], dict[str, Any]]
    normalize_delta: Callable[[int], float]                # 将座位 0 原始分差换算为本游戏展示单位
    progress_from_events: Callable[[list[dict[str, Any]]], int]  # 技术终局已完成轮数
    eta_for_match: Callable[[dict[str, Any]], int]         # 按 match_config 算每场秒数（取代 if game_id 缩放分支）

    # 赛事模板（本游戏的 DEFAULT_TEMPLATES 条目）
    templates: list[dict[str, Any]]

    # Bot 预检（上传时按所选 runtime_mode 试跑 canonical 首回合）——拒绝不合格的 bot。
    # 返回 (ok: bool, detail: str)。所有注册游戏都必须提供，禁止“未定义即放行”。
    preflight_check: Callable[..., Awaitable[tuple[bool, str]]]

    default_scoring: str = "poker_3_1_0"

    # 公开裁判元信息（GET /api/judges 展示用）
    code_path: str = ""
    summary: str = ""

    # 公开裁判源码：要对全体玩家公开明文展示的源码文件相对路径（相对 games/<game>/ 包目录）。
    # 裁判是公开可审计的规则定义——源码必须对全体玩家透明（区别于 Bot 的私有黑盒二进制）。
    # 默认由 game_id 派生权威纯规则文件 + 三件套（适配引擎 / 行协议 /
    # 结果契约），GET /api/judges/{game_id}/source 返回。显式覆写仍必须包含
    # <game_id>_judge.py，且只允许包根目录内的 Python 文件名，防止路径穿越。
    source_files: tuple[str, ...] = ()

    # 相对 games/ 根目录的共享协议实现。公开源码接口会与本游戏源码一并返回，
    # 避免 protocol.py 成为无法审计的 import shim；游戏模块仍只能导出自己的 builder。
    shared_source_files: tuple[str, ...] = ()

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
        shared_files = tuple(self.shared_source_files)
        for rel in shared_files:
            if (
                not isinstance(rel, str)
                or not rel
                or rel in {".", ".."}
                or "/" in rel
                or "\\" in rel
                or not rel.endswith(".py")
            ):
                raise ValueError(
                    f"shared_source_files 只允许 games 包根目录的 Python 文件名: {rel!r}"
                )
        if len(shared_files) != len(set(shared_files)):
            raise ValueError("shared_source_files 不允许重复文件")
        object.__setattr__(self, "shared_source_files", shared_files)

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

        本方法只做字符串规整；运行时 dispatch 必须显式拒绝空值和未知值。
        产品创建入口如需默认游戏，应在其请求模型或创建函数中明确赋值。
        """
        return (game_id or "").strip().lower()

    def judge_games(self) -> list[dict[str, Any]]:
        """聚合所有游戏的公开裁判元信息。"""
        out: list[dict[str, Any]] = []
        for spec in self._specs.values():
            out.append({
                "game_id": spec.game_id,
                "label": spec.label,
                "code_path": spec.code_path,
                "summary": spec.summary,
                "source_files": list(spec.source_files),
                "shared_source_files": list(spec.shared_source_files),
            })
        return out


# 全局单例（games/__init__.py 实例化并注册三款游戏）
