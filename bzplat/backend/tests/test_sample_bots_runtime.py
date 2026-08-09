"""可发布样例 Bot 的源码构建、双运行模式与完整对局回归。"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from bzplat.backend.games.holdem.engine import DEFAULT_HANDS
from bzplat.backend.matches.runner import MatchRunner
from bzplat.backend.runtime.binary_runner import BinaryRunner


ROOT = Path(__file__).resolve().parents[3]
SAMPLES = ROOT / "samples"


@pytest.fixture(scope="module")
def built_samples(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if shutil.which("cc") is None:
        pytest.skip("C compiler unavailable")
    out = tmp_path_factory.mktemp("sample-bots")
    subprocess.run(
        ["bash", str(SAMPLES / "build_sample.sh")],
        cwd=ROOT,
        env={"PATH": os.environ["PATH"], "OUT_DIR": str(out)},
        check=True,
        capture_output=True,
        text=True,
    )
    for name in ("callbot_linux_amd64", "gomokubot_linux_amd64", "pencilbot_linux_amd64"):
        raw = (out / name).read_bytes()
        assert raw.startswith(b"\x7fELF")
    return out


@pytest.fixture(scope="module")
def built_python_samples(tmp_path_factory: pytest.TempPathFactory) -> Path:
    probe = subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--version"],
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        pytest.skip("PyInstaller unavailable")

    out = tmp_path_factory.mktemp("pyinstaller-sample-bots")
    work_root = tmp_path_factory.mktemp("pyinstaller-work")
    for stem in ("callbot", "gomokubot", "pencilbot"):
        work = work_root / stem
        work.mkdir()
        target = f"{stem}_py_linux_amd64"
        subprocess.run(
            [
                sys.executable,
                "-m",
                "PyInstaller",
                "--noconfirm",
                "--clean",
                "--onefile",
                "--name",
                target,
                "--distpath",
                str(out),
                "--workpath",
                str(work),
                "--specpath",
                str(work),
                str(SAMPLES / f"{stem}.py"),
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        description = subprocess.run(
            ["file", "-b", str(out / target)],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert "ELF 64-bit" in description
        assert "x86-64" in description
    return out


@pytest.mark.parametrize("mode", ["traditional", "longrunning"])
def test_compiled_holdem_sample_completes_70_hands(built_samples: Path, mode: str) -> None:
    bot = str(built_samples / "callbot_linux_amd64")
    result = asyncio.run(
        MatchRunner(BinaryRunner(prefer_local=True)).run_binaries(
            bot,
            bot,
            game_id="holdem",
            runtime_modes=(mode, mode),
            seed=20260809,
        )
    )
    assert result.rounds_played == DEFAULT_HANDS
    assert not [e for e in result.events if e.get("reason") == "crash"]


@pytest.mark.parametrize("mode", ["traditional", "longrunning"])
def test_pyinstaller_holdem_wiki_sample_completes_70_hands(
    built_samples: Path, built_python_samples: Path, mode: str
) -> None:
    python_bot = str(built_python_samples / "callbot_py_linux_amd64")
    c_bot = str(built_samples / "callbot_linux_amd64")
    result = asyncio.run(
        MatchRunner(BinaryRunner(prefer_local=True)).run_binaries(
            python_bot,
            c_bot,
            game_id="holdem",
            runtime_modes=(mode, mode),
            seed=20260809,
        )
    )
    assert result.rounds_played == DEFAULT_HANDS
    assert not [e for e in result.events if e.get("reason") == "crash"]


@pytest.mark.parametrize("mode", ["traditional", "longrunning"])
def test_compiled_pencil_sample_finishes_without_illegal_move(
    built_samples: Path, mode: str
) -> None:
    bot = str(built_samples / "pencilbot_linux_amd64")
    result = asyncio.run(
        MatchRunner(BinaryRunner(prefer_local=True)).run_binaries(
            bot,
            bot,
            game_id="pencil",
            runtime_modes=(mode, mode),
            seed=20260809,
            time_budget_per_side=900.0,
        )
    )
    assert result.reason in {"majority", "score"}
    assert not [e for e in result.events if e.get("type") in {"illegal", "time_out"}]
    moves = [e for e in result.events if e.get("type") == "move"]
    coords = [(e["x"], e["y"]) for e in moves]
    assert coords
    assert len(coords) == len(set(coords))
    assert all(0 <= x < 11 and 0 <= y < 11 and (x + y) % 2 == 1 for x, y in coords)
    assert result.events[-1]["type"] == "match_end"


@pytest.mark.parametrize("mode", ["traditional", "longrunning"])
def test_pyinstaller_pencil_wiki_sample_finishes_without_illegal_move(
    built_samples: Path, built_python_samples: Path, mode: str
) -> None:
    python_bot = str(built_python_samples / "pencilbot_py_linux_amd64")
    c_bot = str(built_samples / "pencilbot_linux_amd64")
    result = asyncio.run(
        MatchRunner(BinaryRunner(prefer_local=True)).run_binaries(
            python_bot,
            c_bot,
            game_id="pencil",
            runtime_modes=(mode, mode),
            seed=20260809,
            time_budget_per_side=900.0,
        )
    )
    assert result.reason in {"majority", "score"}
    assert not [e for e in result.events if e.get("type") in {"illegal", "time_out"}]
    assert result.events[-1]["type"] == "match_end"


@pytest.mark.parametrize("mode", ["traditional", "longrunning"])
@pytest.mark.parametrize("artifact", ["fresh_build", "checked_in"])
def test_compiled_gomoku_sample_finishes_without_illegal_move(
    built_samples: Path, mode: str, artifact: str
) -> None:
    sample_dir = built_samples if artifact == "fresh_build" else SAMPLES
    bot = str(sample_dir / "gomokubot_linux_amd64")
    result = asyncio.run(
        MatchRunner(BinaryRunner(prefer_local=True)).run_binaries(
            bot,
            bot,
            game_id="gomoku",
            runtime_modes=(mode, mode),
            seed=20260809,
        )
    )
    assert result.reason in {"five", "draw"}
    assert not [e for e in result.events if e.get("type") == "illegal"]
    assert not [e for e in result.events if e.get("reason") == "crash"]
    assert result.events[-1]["type"] == "match_end"


@pytest.mark.parametrize("mode", ["traditional", "longrunning"])
def test_pyinstaller_gomoku_wiki_sample_finishes_without_illegal_move(
    built_samples: Path, built_python_samples: Path, mode: str
) -> None:
    python_bot = str(built_python_samples / "gomokubot_py_linux_amd64")
    c_bot = str(built_samples / "gomokubot_linux_amd64")
    result = asyncio.run(
        MatchRunner(BinaryRunner(prefer_local=True)).run_binaries(
            python_bot,
            c_bot,
            game_id="gomoku",
            runtime_modes=(mode, mode),
            seed=20260809,
        )
    )
    assert result.reason in {"five", "draw"}
    assert not [e for e in result.events if e.get("type") == "illegal"]
    assert not [e for e in result.events if e.get("reason") == "crash"]
    assert result.events[-1]["type"] == "match_end"


def test_python_pencil_sample_replays_full_history_and_keeps_running() -> None:
    legal = [(x, y) for x in range(11) for y in range(11) if (x + y) % 2 == 1]
    used, only_free = legal[:-1], legal[-1]
    requests = [
        {"x": x, "y": y, "pass": 0, "me": 0, "scores": [0, 0]}
        for x, y in used[::2]
    ]
    responses = [{"x": x, "y": y} for x, y in used[1::2]]
    envelope = {"requests": requests, "responses": responses}
    follow_up = {"request": {"x": -1, "y": -1, "pass": 1, "me": 0, "scores": [12, 12]}}

    proc = subprocess.run(
        [sys.executable, str(SAMPLES / "pencilbot.py")],
        input=json.dumps(envelope) + "\n" + json.dumps(follow_up) + "\n",
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    lines = proc.stdout.splitlines()
    assert json.loads(lines[0]) == {"response": {"x": only_free[0], "y": only_free[1]}}
    assert lines[1] == ">>>BOTZONE_REQUEST_KEEP_RUNNING<<<"
    assert json.loads(lines[2]) == {"response": {"x": -1, "y": -1}}


def test_python_gomoku_sample_replays_full_history_and_keeps_running() -> None:
    legal = [(x, y) for x in range(15) for y in range(15)]
    used, only_free = legal[:-1], legal[-1]
    requests = [{"x": -1, "y": -1, "me": 0}]
    requests.extend({"x": x, "y": y, "me": 0} for x, y in used[::2])
    responses = [{"x": x, "y": y} for x, y in used[1::2]]
    envelope = {"requests": requests, "responses": responses}

    proc = subprocess.run(
        [sys.executable, str(SAMPLES / "gomokubot.py")],
        input=json.dumps(envelope) + "\n",
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    lines = proc.stdout.splitlines()
    assert json.loads(lines[0]) == {"response": {"x": only_free[0], "y": only_free[1]}}
    assert lines[1] == ">>>BOTZONE_REQUEST_KEEP_RUNNING<<<"


def test_holdem_strategy_samples_use_standard_history_fields(tmp_path: Path) -> None:
    if shutil.which("cc") is None:
        pytest.skip("C compiler unavailable")
    names = ("foldbot", "allinbot", "raisebot", "randombot", "tightbot", "loosebot")
    built: dict[str, Path] = {}
    for name in names:
        target = tmp_path / name
        subprocess.run(
            ["cc", "-O2", "-o", str(target), str(SAMPLES / "holdem_bots" / f"{name}.c")],
            check=True,
            capture_output=True,
            text=True,
        )
        built[name] = target

    # Current request is second in the Traditional history. Opponent (seat 0/SB)
    # raised by delta 250 to 300; seat 1 has 100 posted, so minimum re-raise-to is
    # 600 and the response delta must be 600-100=500.
    old = {
        "num_players": 2, "dealer_id": 1, "my_id": 1, "my_chips": 19950,
        "my_cards": [0, 1], "public_cards": [], "history": [], "hand": 0,
        "max_hand": 70, "total_win_chips": [0, 0], "total_win_games": [0, 0],
    }
    current = {
        "num_players": 2, "dealer_id": 0, "my_id": 1, "my_chips": 19900,
        "my_cards": [48, 49], "public_cards": [],
        "history": [{"round": 0, "player_id": 0, "action": 250, "action_type": "raise"}],
        "hand": 1, "max_hand": 70, "total_win_chips": [0, 0],
        "total_win_games": [0, 0],
    }
    envelope = json.dumps({"requests": [old, current], "responses": [0]}) + "\n"
    responses: dict[str, int] = {}
    for name, binary in built.items():
        proc = subprocess.run(
            [str(binary)], input=envelope, capture_output=True, text=True, timeout=5
        )
        first = proc.stdout.splitlines()[0]
        responses[name] = json.loads(first)["response"]
        assert proc.stdout.splitlines()[1] == ">>>BOTZONE_REQUEST_KEEP_RUNNING<<<"
    assert responses["foldbot"] == -1
    assert responses["allinbot"] == -2
    assert responses["raisebot"] == 500
