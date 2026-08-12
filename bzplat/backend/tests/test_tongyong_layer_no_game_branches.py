"""守护测试：通用层不得出现 game-specific 分支（全面解耦不变量）。

镜像 test_game_subpackages_dont_import_engine_top 的源码扫描模式，但反向：
扫描通用层（matches/ contests/ store/ api_routes/ bots/ auth/ rating/ runtime/
notifications/）禁止：
  - `== "holdem"` / `!= "holdem"` / `in ("holdem",...)` / `startswith("holdem")` 等按游戏名分支
  - `("holdem", "gomoku", "pencil")` / `["holdem","gomoku","pencil"]` / `{"holdem",...}`
    硬编码 3-game 列表（任意顺序、含 2-game 子集）
  - `registry.get("holdem")` 等硬指某游戏的调用（应经变量）
  - `from bzplat.backend.games.holdem import` 直接 import 具体游戏模块（应经 registry）

产品创建入口可以通过请求模型默认值或显式 ``if game_id is None`` 选择默认游戏；
运行时 ``or "holdem"`` / ``get("game_id", "holdem")`` 属于静默降级，必须拒绝。
注册表常量定义里的游戏字面量用 ``# allow-game-registry-definition`` 标记。

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
# == "holdem" / != "holdem" / in ("holdem",...) / startswith("holdem") / .get("holdem")
_BRANCH_RE = re.compile(
    r'==\s*["\']' + _GAMES + r'["\']'                                  # == / != "holdem"
    r'|!=\s*["\']' + _GAMES + r'["\']'
    r'|\bin\s*[\(\[]\s*[^)\]]*["\']' + _GAMES + r'["\']'               # in (... "holdem" ...)
    r'|\.(?:startswith|endswith)\s*\(\s*["\']' + _GAMES + r'["\']'    # .startswith("holdem")
    # 硬指某游戏的调用：registry.get("holdem") / default_match_config("holdem")
    # 排除 = "holdem" 赋值/默认值与 , "holdem" 参数列表里的合法默认（这些由 _FALLBACK_RE 兜底豁免）
    r'|\.(?:get|default_match_config|game_label|is_registered)\s*\(\s*["\']' + _GAMES + r'["\']'
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

# 持久化/运行路径不得把缺失 game_id 猜成某款游戏。
_SILENT_FALLBACK_RE = re.compile(
    r'\bor\s*["\']' + _GAMES + r'["\']'
    r'|\.get\(\s*["\']game_id["\']\s*,\s*["\']' + _GAMES + r'["\']\s*\)'
    r'|\bif\b[^\n]*\belse\s*["\']' + _GAMES + r'["\']'
)
_ALLOW_RE = re.compile(r'#\s*allow-game-registry-definition')


def _scan_file(py: pathlib.Path) -> list[str]:
    """返回该文件的违规行列表（file:line: content）。

    跳过：注释行、多行/单行 docstring，以及注册表常量定义行。
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
        if _ALLOW_RE.search(line):
            continue
        # 检查分支
        if _BRANCH_RE.search(line):
            violations.append(f"{rel}:{i}: game-name 分支 {line.strip()}")
        if _SILENT_FALLBACK_RE.search(line):
            violations.append(f"{rel}:{i}: 静默 game_id 兜底 {line.strip()}")
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
        "产品默认须在创建边界显式赋值；运行时不得把缺失/未知游戏猜成另一款。\n"
        "违规：\n" + "\n".join(violations)
    )


# ── 守护测试自身的有效性（正则能抓各类变体）──────────────────────
def test_guard_regex_catches_branch_variants():
    """PR4：守护正则覆盖各类 game-name 分支变体（防守护测试盲区）。

    上一版 _BRANCH_RE 只抓 == "holdem"，漏 != / in / startswith / .get("holdem")。
    本测试断言各分支与静默兜底变体都被抓，显式创建默认不被误抓。
    """
    # 应被抓的违规变体（真实调用形式，方法调用带点）
    must_catch = [
        'if gid == "holdem":',
        'if gid != "gomoku":',
        'if x in ("holdem", "gomoku"):',
        'if gid.startswith("pencil"):',
        'return _reg.get("holdem")',
        'registry.default_match_config("gomoku")',
        '("holdem","gomoku","pencil")',
        '["gomoku","holdem"]',            # 乱序 2-game 子集
        '{"holdem","gomoku","pencil"}',   # frozenset 字面量
        'from bzplat.backend.games.holdem import X',
        'import bzplat.backend.games.gomoku',
        'gid = game_id or "holdem"',
        'gid = row.get("game_id", "holdem")',
        'gid = row["game_id"] if row and row["game_id"] else "holdem"',
    ]
    for sample in must_catch:
        assert (
            _BRANCH_RE.search(sample)
            or _TUPLE_RE.search(sample)
            or _IMPORT_RE.search(sample)
            or _SILENT_FALLBACK_RE.search(sample)
        ), (
            f"守护正则漏抓违规变体: {sample!r}"
        )
    # 创建边界的显式默认不应被抓。
    explicit_creation_defaults = [
        'game_id: str = "holdem"',
        'gid = "holdem" if game_id is None else game_id',
    ]
    for sample in explicit_creation_defaults:
        assert not _BRANCH_RE.search(sample)
        assert not _SILENT_FALLBACK_RE.search(sample), f"守护正则误抓显式默认: {sample!r}"


# ── runner/engine.run_session 不得硬编码游戏专属参数名（PR3：**match_params 透传）──
# runner.run_binaries / run_bot_vs_human / run_callables 与 engine.registry.run_session
# 不得把 num_hands/n_dots/board_size/starting_stack/sb/bb 列为具名参数——否则第 4 游戏
# 带新参数（如 komi）必改通用层签名（违反"零改动新增游戏"承诺）。应改 **match_params 透传。
_GAME_PARAM_NAMES = {"num_hands", "n_dots", "board_size", "starting_stack", "sb", "bb"}


def test_runner_signatures_use_passthrough_not_named_game_params():
    """runner 四方法 + engine.run_session 用 **match_params 透传，不列游戏专属具名参数。"""
    import inspect

    from bzplat.backend.games import run_session as _engine_run_session
    from bzplat.backend.matches.runner import MatchRunner

    targets = [
        ("MatchRunner.run_binaries", MatchRunner.run_binaries),
        ("MatchRunner.run_bot_vs_human", MatchRunner.run_bot_vs_human),
        ("MatchRunner.run_callables", MatchRunner.run_callables),
        ("MatchRunner.run_duplicate", MatchRunner.run_duplicate),
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


# ── match_config/result 双 JSON 通路守护（彻底重构：删 6 死列，统一通路）──
# orchestrator.create 走 match_config dict（取代 hands/n_dots/**extra 具名）；
# orchestrator._run_match/_run_human_match 走 **mc 透传（取代具名传 num_hands/n_dots/...）；
# store.create_match/update_match 走 match_config/result（取代 total_hands/n_dots/earnings 列）。
# 这些 AST 守护堵住"签名守护"的盲区——签名守护查函数定义，AST 守护查调用侧 + 表结构。
_CONFIG_RESULT_PARAM_NAMES = _GAME_PARAM_NAMES | {
    "total_hands",
    "hands_played",
    "earnings_a",
    "earnings_b",
    "net_bb_a",
}
_DEAD_MATCH_COLUMNS = {
    "total_hands",
    "n_dots",
    "net_bb_a",
    "hands_played",
    "earnings_a",
    "earnings_b",
}


def test_orchestrator_uses_match_config_not_named_params():
    """orchestrator 的 challenge/challenge_human 签名 + _run_match/_run_human_match 调用
    不得用游戏专属具名参数（应 match_config / **mc 透传）。AST 级守护调用侧，堵签名守护盲区。
    """
    import ast

    orch_path = _ROOT / "matches/orchestrator.py"
    tree = ast.parse(orch_path.read_text(encoding="utf-8"))

    for node in ast.walk(tree):
        # 1) challenge / challenge_human 签名不得含游戏专属具名参数
        if isinstance(node, ast.AsyncFunctionDef) and node.name in ("challenge", "challenge_human"):
            leaked = _CONFIG_RESULT_PARAM_NAMES & {a.arg for a in node.args.args + node.args.kwonlyargs}
            assert not leaked, (
                f"orchestrator.{node.name} 签名不得含游戏专属参数 {leaked}——"
                "应统一用 match_config: dict（取代散落 hands/n_dots/... 具名）"
            )
        # 2) _run_match / _run_human_match 里调 run_binaries/run_bot_vs_human 不得具名传游戏参数
        if isinstance(node, ast.AsyncFunctionDef) and node.name in ("_run_match", "_run_human_match"):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Await) and isinstance(sub.value, ast.Call):
                    call = sub.value
                    fname = getattr(call.func, "attr", "") or getattr(call.func, "id", "")
                    if fname in ("run_binaries", "run_bot_vs_human"):
                        leaked_kws = {
                            kw.arg for kw in call.keywords
                            if kw.arg in _CONFIG_RESULT_PARAM_NAMES
                        }
                        assert not leaked_kws, (
                            f"orchestrator.{node.name} 调 {fname}() 不得具名传 {leaked_kws}——"
                            "应整体 **mc 透传（match_config + admin 设置合并后的 dict）"
                        )


def test_store_uses_match_config_and_result():
    """store.create_match / update_match 签名不得含游戏专属/死列参数（应 match_config / result）。"""
    import inspect

    from bzplat.backend.store.db import Store

    for method_name in ("create_match", "update_match"):
        fn = getattr(Store, method_name, None)
        if fn is None:
            continue
        params = set(inspect.signature(fn).parameters)
        # update_match 是 **fields，参数名固定为 fields；只查 create_match 的显式参数
        if method_name == "create_match":
            leaked = _CONFIG_RESULT_PARAM_NAMES & params
            assert not leaked, (
                f"Store.{method_name} 签名不得含 {leaked}——"
                "应用 match_config: dict（取代 total_hands/n_dots 具名 + 固定列）"
            )


def test_matches_table_no_dead_columns():
    """matches 表不得含已删的 6 个游戏专属死列（应 match_config + result 双 JSON 列）。

    守护 schema.py 三表 DDL + db.py 建表模板，防止死列回退。
    """
    schema = (_ROOT / "store/schema.py").read_text(encoding="utf-8")
    db_tmpl = (_ROOT / "store/db.py").read_text(encoding="utf-8")
    # 提取建表模板段（_CREATE_MATCHES_TABLE_SQL 赋值到下一个空行定义）
    for text, label in ((schema, "schema.py"), (db_tmpl, "db.py")):
        # 找所有 matches_<game>/CREATE TABLE matches_ 段，检查列定义
        # 简单粗暴：整个文件里不应出现 "<dead_col>    INTEGER/REAL" 列定义模式
        for col in _DEAD_MATCH_COLUMNS:
            # 匹配列定义：列名 + 空白 + 类型（排除注释里的提及）
            import re
            pattern = re.compile(rf"^\s*{col}\s+(INTEGER|REAL|TEXT)", re.MULTILINE)
            matches = pattern.findall(text)
            # db.py 的 _migrate 段会引用旧列名做 DROP/UPDATE（合法），用注释/字符串上下文区分：
            # 只在 CREATE TABLE 段（schema.py 全文 + db.py 模板）里算违规。
            if label == "db.py":
                # 只检查 _CREATE_MATCHES_TABLE_SQL 模板内的列定义（DROP/UPDATE 在模板外）
                tmpl_match = re.search(
                    r'_CREATE_MATCHES_TABLE_SQL\s*=\s*"""(.*?)"""',
                    db_tmpl, re.DOTALL,
                )
                text = tmpl_match.group(1) if tmpl_match else ""
            bad = re.findall(rf"^\s*{col}\s+(INTEGER|REAL|TEXT)", text, re.MULTILINE)
            assert not bad, (
                f"{label} 的 matches 表定义不得含死列 '{col}'——"
                f"应进 match_config/result JSON 列（彻底重构删 6 死列）"
            )
