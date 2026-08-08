"""循环依赖加固 + games/ 自治守护测试。

全面解耦后，引擎/协议/结果/段位物理迁入 games/<game>/ 子包（自包含），三层冗余
shim（engine/ + protocol/ + _compat/）已删除。本测试在**独立子进程**里从多种 import
顺序断言无 ImportError——若有人在新模块加了反向 import（如通用层 import 具体游戏），
此测试会捕获。

**为什么用子进程**：直接在 pytest 进程里清空 sys.modules 会污染后续测试（它们依赖
已加载的模块对象/monkeypatch）。子进程完全隔离，测完即弃。

守护的不变量（文档化于 AGENTS.md）：
- games/<game>/ 子包（engine/protocol/result/tiers/cards/spec/templates）**不得**反向
  import bzplat.backend.engine / bzplat.backend._compat / bzplat.backend.protocol（已删的
  shim，保留为"防回退"哨兵）或通用层（matches/contests/store/api_routes）——只能 import
  同包 / bzplat.backend.games.base / bzplat.backend.games._board_protocol /
  bzplat.backend.store.schema（纯常量）。
"""
from __future__ import annotations

import subprocess
import sys
import textwrap


def _assert_imports_clean(imports: list[str]) -> None:
    """在独立子进程里 import 给定模块列表，断言无 ImportError。"""
    code = textwrap.dedent(f"""
        import sys
        try:
            {"; ".join(f"import {m}" for m in imports)}
            print("OK")
        except Exception as e:
            print("FAIL:", type(e).__name__, e)
            sys.exit(1)
    """)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, (
        f"import 顺序 {imports} 失败：\nstdout: {r.stdout}\nstderr: {r.stderr}"
    )


def test_import_games_first():
    """先 import games（注册表），再 import 编排层——无 ImportError。"""
    _assert_imports_clean([
        "bzplat.backend.games",
        "bzplat.backend.matches.orchestrator",
        "bzplat.backend.matches.runner",
    ])


def test_import_orchestrator_first():
    """先 import 编排层（orchestrator import games），再 import games——无 ImportError。"""
    _assert_imports_clean([
        "bzplat.backend.matches.orchestrator",
        "bzplat.backend.games",
        "bzplat.backend.contests.manager",
    ])


def test_import_store_first():
    """先 import store（db.py 延迟 import games），再 import games——无 ImportError。"""
    _assert_imports_clean([
        "bzplat.backend.store",
        "bzplat.backend.games",
        "bzplat.backend.store.db",
    ])


def test_import_all_main_modules():
    """import 全部主模块——无 ImportError（综合顺序）。"""
    _assert_imports_clean([
        "bzplat.backend.games",
        "bzplat.backend.games.base",
        "bzplat.backend.games.holdem.spec",
        "bzplat.backend.games.gomoku.spec",
        "bzplat.backend.games.pencil.spec",
        "bzplat.backend.games.holdem.engine",
        "bzplat.backend.games.holdem.holdem_judge",
        "bzplat.backend.games.holdem.result",
        "bzplat.backend.games.holdem.tiers",
        "bzplat.backend.games.gomoku.engine",
        "bzplat.backend.games.pencil.engine",
        "bzplat.backend.matches.orchestrator",
        "bzplat.backend.matches.runner",
        "bzplat.backend.contests.manager",
        "bzplat.backend.contests.templates",
        "bzplat.backend.contests.validation",
        "bzplat.backend.store",
        "bzplat.backend.api_routes",
    ])


def test_game_subpackages_dont_import_engine_top():
    """守护不变量：games/<game>/ 子包不反向 import 已删的 shim / 通用层。

    若有人在新 games/<game>/ 模块加了 `from bzplat.backend.engine import X`
    （重建 shim）或反向 import 通用层，此源码扫描会标记（比运行时 import 错误更早定位）。
    forbidden 元组含已删的 engine/_compat/protocol 作为"防回退"哨兵。
    """
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2] / "bzplat" / "backend" / "games"
    forbidden = (
        "bzplat.backend.engine",        # 已删 shim（防回退哨兵）
        "bzplat.backend._compat",       # 已删 shim（防回退哨兵）
        "bzplat.backend.protocol",      # 已删 shim（防回退哨兵）
        "bzplat.backend.matches",       # 通用层（games 不得反向依赖）
        "bzplat.backend.contests",      # 通用层
        "bzplat.backend.api_routes",    # 通用层
    )
    violations = []
    for py in root.rglob("*.py"):
        rel = py.relative_to(root)
        text = py.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"""') or stripped.startswith("'"):
                continue
            for bad in forbidden:
                if f"import {bad}" in line or f"from {bad}" in line:
                    violations.append(f"{rel}: {line.strip()}")
    assert not violations, (
        "games/<game>/ 子包不得反向 import 已删 shim 或通用层（会引入循环依赖 / 破坏解耦）：\n"
        + "\n".join(violations)
    )
