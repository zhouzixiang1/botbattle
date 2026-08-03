"""守护测试：通用层不得出现 game-specific 分支（全面解耦不变量）。

镜像 test_game_subpackages_dont_import_engine_top 的源码扫描模式，但反向：
扫描通用层（matches/ contests/ store/ api_routes/ bots/ auth/ rating/ runtime/
notifications/）禁止：
  - `== "holdem"` / `!= "holdem"` / `in ("holdem",...)` / `startswith("holdem")` 等按游戏名分支
  - `("holdem", "gomoku", "pencil")` / `["holdem","gomoku","pencil"]` / `{"holdem",...}`
    硬编码 3-game 列表（任意顺序、含 2-game 子集）
  - `registry.get("holdem")` / `all_tiers("holdem")` 等硬指某游戏的调用（应经变量）
  - `from bzplat.backend.games.holdem import` 直接 import 具体游戏模块（应经 registry）

豁免：纯默认值兜底（如 `game_id: str = "holdem"`、`c.get("game_id", "holdem")`、
`or "holdem"`、`except KeyError: ...holdem`）是 normalize_game_id 的合法语义，不算分支。
用 `# allow-game-fallback` 注释标记豁免点（守护测试跳过该行）。

触发场景：审计发现 db.py FK 重建曾硬编码 3-game 元组（C1）、manager.py 曾有
`if gid == "holdem"` 死分支（I1/I2）。本测试防回归。
"""
from __future__ import annotations

import pathlib
import re

# 测试文件在 bzplat/backend/tests/，parents[2] = bzplat/，再 / "backend" = bzplat/backend
_ROOT = pathlib.Path(__file__).resolve().parents[2] / "backend"

# 扫描的通用层目录/文件（games/ / _compat/ / engine/ / protocol/ 是允许 game-specific 的）
_SCAN_DIRS = ("matches", "contests", "store", "bots", "auth", "rating", "runtime", "notifications")
_SCAN_FILES = ("api_routes.py", "main.py", "cli.py", "logging_config.py")

# 游戏名集合（用于模式匹配）——从注册表派生，避免硬编码 3-game 名导致未来加第 4 款
# 游戏时守护失效（漏报）。schema 与注册表一致性由 test_game_registry + 启动断言守护。
from bzplat.backend.store.schema import VALID_GAME_IDS  # noqa: E402

_GAMES = "(?:" + "|".join(sorted(VALID_GAME_IDS)) + ")"

# 禁止的模式：按游戏名分支
# == "holdem" / != "holdem" / in ("holdem",...) / startswith("holdem") / .get("holdem") / all_tiers("holdem")
_BRANCH_RE = re.compile(
    r'==\s*["\']' + _GAMES + r'["\']'                                  # == / != "holdem"
    r'|!=\s*["\']' + _GAMES + r'["\']'
    r'|\bin\s*[\(\[]\s*[^)\]]*["\']' + _GAMES + r'["\']'               # in (... "holdem" ...)
    r'|\.(?:startswith|endswith)\s*\(\s*["\']' + _GAMES + r'["\']'    # .startswith("holdem")
    # 硬指某游戏的调用：registry.get("holdem") / all_tiers("holdem") / default_match_config("holdem")
    # 排除 = "holdem" 赋值/默认值与 , "holdem" 参数列表里的合法默认（这些由 _FALLBACK_RE 兜底豁免）
    r'|\.(?:get|all_tiers|default_match_config|game_label|is_registered)\s*\(\s*["\']' + _GAMES + r'["\']'
)
# 禁止的模式：硬编码 3-game 列表字面量（任意括号 ()/[]/{}，任意顺序，含 2-game 子集）
# 匹配形如 ("holdem","gomoku","pencil") 或 {"gomoku","holdem"} 或 ["holdem","pencil"] 等
_TUPLE_RE = re.compile(
    r'[\(\[\{]\s*["\']' + _GAMES + r'["\']\s*,\s*["\']' + _GAMES + r'["\']'
    r'(?:\s*,\s*["\']' + _GAMES + r'["\'])*\s*[\)\]\}]'
)
# 禁止的模式：通用层直接 import 具体游戏模块（应经 registry）
_IMPORT_RE = re.compile(
    r'from\s+bzplat\.backend\.games\.' + _GAMES + r'\b'
    r'|import\s+bzplat\.backend\.games\.' + _GAMES + r'\b'
)

# 允许的纯默认值/兜底模式（不算分支）：= "holdem" / , "holdem" / or "holdem" / except KeyError
# 这些是 normalize_game_id 的合法兜底语义。若某行命中 _BRANCH_RE/_TUPLE_RE 又是兜底，
# 用 # allow-game-fallback 注释显式豁免。
_FALLBACK_RE = re.compile(r'#\s*allow-game-fallback')


def _scan_file(py: pathlib.Path) -> list[str]:
    """返回该文件的违规行列表（file:line: content）。

    跳过：注释行、多行/单行 docstring（按三引号切换状态跟踪）、allow-game-fallback 豁免行。
    """
    violations: list[str] = []
    try:
        text = py.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return violations
    rel = py.relative_to(_ROOT)
    in_docstring = False
    for i, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        # 跟踪三引号 docstring 状态（粗略：按行内 """ 切换）
        count = line.count('"""')
        if in_docstring:
            if count % 2 == 1:
                in_docstring = False
            continue  # docstring 内的行一律跳过
        if count:
            if count % 2 == 1:
                in_docstring = True
            # 单行 docstring（count==2）整行跳过；多行首行也跳过
            continue
        # 跳过注释行
        if stripped.startswith("#"):
            continue
        # 显式豁免标记
        if _FALLBACK_RE.search(line):
            continue
        # 检查分支
        if _BRANCH_RE.search(line):
            violations.append(f"{rel}:{i}: game-name 分支 {line.strip()}")
        # 检查硬编码 3-game 元组
        if _TUPLE_RE.search(line):
            violations.append(f"{rel}:{i}: 硬编码 3-game 列表 {line.strip()}")
        # 检查直接 import 具体游戏模块
        if _IMPORT_RE.search(line):
            violations.append(f"{rel}:{i}: 通用层直接 import 具体游戏模块 {line.strip()}")
    return violations


def test_tongyong_layer_no_game_branches():
    """通用层不得按游戏名分支，不得硬编码 3-game 列表（应经 registry 派生）。

    解耦契约（AGENTS.md）：通用层经 registry.get(game_id) 取 spec，
    禁止 if game_id== 分支；跨游戏聚合用 _all_game_ids()/VALID_GAME_IDS，
    不得硬编码 ("holdem","gomoku","pencil")。
    """
    files: list[pathlib.Path] = []
    for d in _SCAN_DIRS:
        files.extend((_ROOT / d).rglob("*.py"))
    for f in _SCAN_FILES:
        p = _ROOT / f
        if p.is_file():
            files.append(p)

    violations: list[str] = []
    for py in files:
        violations.extend(_scan_file(py))

    assert not violations, (
        "通用层不得出现 game-specific 分支或硬编码 3-game 列表（全面解耦不变量）。\n"
        "应经 games 注册表（registry.get / _all_game_ids / VALID_GAME_IDS）派生。\n"
        "若确为 normalize_game_id 兜底语义，在该行加 # allow-game-fallback 注释豁免。\n"
        "违规：\n" + "\n".join(violations)
    )


# ── 守护测试自身的有效性（正则能抓各类变体）──────────────────────
def test_guard_regex_catches_branch_variants():
    """PR4：守护正则覆盖各类 game-name 分支变体（防守护测试盲区）。

    上一版 _BRANCH_RE 只抓 == "holdem"，漏 != / in / startswith / .get("holdem")。
    本测试断言各变体都被抓，且合法兜底（= "holdem" / or "holdem"）不被误抓。
    """
    # 应被抓的违规变体（真实调用形式，方法调用带点）
    must_catch = [
        'if gid == "holdem":',
        'if gid != "gomoku":',
        'if x in ("holdem", "gomoku"):',
        'if gid.startswith("pencil"):',
        'return _reg.get("holdem")',
        '_game_registry.all_tiers("holdem")',
        'registry.default_match_config("gomoku")',
        '("holdem","gomoku","pencil")',
        '["gomoku","holdem"]',            # 乱序 2-game 子集
        '{"holdem","gomoku","pencil"}',   # frozenset 字面量
        'from bzplat.backend.games.holdem import X',
        'import bzplat.backend.games.gomoku',
    ]
    for sample in must_catch:
        assert _BRANCH_RE.search(sample) or _TUPLE_RE.search(sample) or _IMPORT_RE.search(sample), (
            f"守护正则漏抓违规变体: {sample!r}"
        )
    # 合法兜底不应被抓（这些是 normalize 语义，由 = / or / , 上下文区分，不在 _BRANCH_RE 内）
    ok_fallbacks = [
        'game_id: str = "holdem"',
        'gid = game_id or "holdem"',
        'c.get("game_id", "holdem")',
    ]
    for sample in ok_fallbacks:
        assert not _BRANCH_RE.search(sample), f"守护正则误抓合法兜底: {sample!r}"


# ── runner/engine.run_session 不得硬编码游戏专属参数名（PR3：**match_params 透传）──
# runner.run_binaries / run_bot_vs_human / run_callables 与 engine.registry.run_session
# 不得把 num_hands/n_dots/board_size/starting_stack/sb/bb 列为具名参数——否则第 4 游戏
# 带新参数（如 komi）必改通用层签名（违反"零改动新增游戏"承诺）。应改 **match_params 透传。
_GAME_PARAM_NAMES = {"num_hands", "n_dots", "board_size", "starting_stack", "sb", "bb"}


def test_runner_signatures_use_passthrough_not_named_game_params():
    """runner 三方法 + engine.run_session 用 **match_params 透传，不列游戏专属具名参数。"""
    import inspect

    from bzplat.backend.games import run_session as _engine_run_session
    from bzplat.backend.matches.runner import MatchRunner

    targets = [
        ("MatchRunner.run_binaries", MatchRunner.run_binaries),
        ("MatchRunner.run_bot_vs_human", MatchRunner.run_bot_vs_human),
        ("MatchRunner.run_callables", MatchRunner.run_callables),
        ("engine.registry.run_session", _engine_run_session),
    ]
    for label, fn in targets:
        params = inspect.signature(fn).parameters
        # 允许的具名参数（非游戏专属）
        leaked = _GAME_PARAM_NAMES & set(params)
        assert not leaked, (
            f"{label} 不得把游戏专属参数 {leaked} 列为具名参数——"
            "应经 **match_params/**params 透传（否则第 4 游戏带新参数必改通用层签名）"
        )
        # 必须有透传用的 **kwargs 参数
        has_kwargs = any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
        )
        assert has_kwargs, f"{label} 须有 **match_params 透传游戏参数"
