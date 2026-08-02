"""全面解耦 PR-E：循环依赖加固测试。

解耦后 games/ ↔ engine/ ↔ _compat/ 之间存在 import 顺序敏感性（用 __getattr__ 延迟
import 打破循环）。本测试在**独立子进程**里从多种 import 顺序断言无 ImportError——
若有人在新模块加了反向 import（如 games/<game>/result.py import engine），此测试会捕获。

**为什么用子进程**：直接在 pytest 进程里清空 sys.modules 会污染后续测试（它们依赖
已加载的模块对象/monkeypatch）。子进程完全隔离，测完即弃。

守护的不变量（文档化于 AGENTS.md）：
- games/<game>/ 子包（engine/protocol/result/tiers/cards/spec/templates）**不得**反向
  import bzplat.backend.engine（顶层包）或 bzplat.backend._compat——只能 import 同包 /
  bzplat.backend.games.base / bzplat.backend.games._board_protocol / bzplat.backend.store。
- engine/ 与 protocol/ 旧文件是 shim（re-export 自 _compat），不直接含游戏逻辑。
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
    """先 import games（注册表），再 import engine——无 ImportError。"""
    _assert_imports_clean([
        "bzplat.backend.games",
        "bzplat.backend.engine",
        "bzplat.backend.matches.orchestrator",
        "bzplat.backend.matches.runner",
    ])


def test_import_engine_first():
    """先 import engine（触发 __init__ → _compat），再 import games——无 ImportError。"""
    _assert_imports_clean([
        "bzplat.backend.engine",
        "bzplat.backend.games",
        "bzplat.backend.contests.manager",
    ])


def test_import_engine_game_deep_first():
    """先 import 具体引擎模块（engine.game shim），再 import games——无 ImportError。"""
    _assert_imports_clean([
        "bzplat.backend.engine.game",
        "bzplat.backend.games",
        "bzplat.backend.engine.registry",
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
        "bzplat.backend.engine",
        "bzplat.backend.engine.registry",
        "bzplat.backend.engine.result",
        "bzplat.backend.engine.tiers",
        "bzplat.backend._compat.engine_game",
        "bzplat.backend._compat.engine_tiers",
        "bzplat.backend.matches.orchestrator",
        "bzplat.backend.matches.runner",
        "bzplat.backend.contests.manager",
        "bzplat.backend.contests.templates",
        "bzplat.backend.contests.validation",
        "bzplat.backend.store",
        "bzplat.backend.api_routes",
    ])


def test_registry_symbols_accessible_from_engine_package():
    """engine 包的延迟符号（GAME_LABELS/run_session 等）可访问——__getattr__ 工作。"""
    code = textwrap.dedent("""
        import bzplat.backend.engine as e
        assert e.GAME_LABELS["holdem"] == "德州扑克"
        assert callable(e.run_session)
        assert e.GAME_HOLDEM == "holdem"
        assert e.is_registered("gomoku")
        print("OK")
    """)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, f"engine 延迟符号访问失败:\nstdout: {r.stdout}\nstderr: {r.stderr}"


def test_game_subpackages_dont_import_engine_top():
    """守护不变量：games/<game>/ 子包不反向 import engine/ 顶层。

    若有人在新 games/<game>/ 模块加了 `from bzplat.backend.engine import X`，
    此源码扫描会标记（比运行时 import 错误更早定位）。
    """
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2] / "bzplat" / "backend" / "games"
    forbidden = ("bzplat.backend.engine", "bzplat.backend._compat", "bzplat.backend.protocol")
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
        "games/<game>/ 子包不得反向 import engine/_compat/protocol（会引入循环依赖）：\n"
        + "\n".join(violations)
    )
