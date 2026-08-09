"""二进制分类与样例 bot 对局冒烟。"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from bzplat.backend.bots.classify import BinaryInfo, classify_binary
from bzplat.backend.matches.runner import MatchRunner
from bzplat.backend.games.holdem import protocol as proto
from bzplat.backend.runtime.binary_runner import (
    BinaryRunner,
    BotCrashedError,
    BotSession,
    PlatformRunnerError,
)

SAMPLES = Path(__file__).resolve().parents[3] / "samples"
ELF = SAMPLES / "callbot_linux_amd64"


def test_classify_elf():
    data = ELF.read_bytes()
    info = classify_binary(data)
    assert info.format == "elf"
    assert info.runnable
    assert info.arch in ("amd64", "arm64", "i386")


def test_classify_macho_rejected():
    data = b"\xfe\xed\xfa\xce" + b"\x00" * 20
    info = classify_binary(data)
    assert info.format == "macho"
    assert not info.runnable


def test_missing_docker_fails_closed_unless_local_mode_is_explicit():
    """Production may never execute an uploaded ELF directly on the host."""
    info = BinaryInfo("elf", "linux", "amd64", True)

    production = BinaryRunner(docker_bin="definitely-no-such-docker", prefer_local=False)
    assert production._docker_ok is False
    with pytest.raises(PlatformRunnerError, match="Docker 沙箱"):
        production._select_mode(info)

    explicit_test_mode = BinaryRunner(
        docker_bin="definitely-no-such-docker", prefer_local=True
    )
    assert explicit_test_mode._select_mode(info) == "local"


def test_docker_exit_125_is_platform_fault_not_bot_crash(tmp_path):
    """docker run exit 125 is an infrastructure failure and must not rate a Bot."""

    class FakeStdin:
        def write(self, _data):
            return None

        async def drain(self):
            return None

    class FakeStdout:
        async def readline(self):
            return b""

    class FakeProc:
        def __init__(self, returncode: int):
            self.returncode = returncode
            self.stdin = FakeStdin()
            self.stdout = FakeStdout()

        async def wait(self):
            return self.returncode

    path = tmp_path / "bot"
    path.write_bytes(b"unused")
    info = BinaryInfo("elf", "linux", "amd64", True)

    infra = BinaryRunner(prefer_local=False)
    infra._sessions["infra"] = BotSession(
        "infra", info, path, proc=FakeProc(125), mode="docker",
    )
    with pytest.raises(PlatformRunnerError, match="docker exit 125"):
        asyncio.run(infra.send("infra", "{}"))

    bot_fault = BinaryRunner(prefer_local=False)
    bot_fault._sessions["bot"] = BotSession(
        "bot", info, path, proc=FakeProc(126), mode="docker",
    )
    with pytest.raises(BotCrashedError):
        asyncio.run(bot_fault.send("bot", "{}"))


def test_decision_timeout_log_does_not_expose_bot_stderr_paths(tmp_path, caplog):
    class FakeStdin:
        def write(self, _data):
            return None

        async def drain(self):
            return None

    class SlowStdout:
        async def readline(self):
            await asyncio.sleep(1)
            return b""

    class FakeProc:
        returncode = None
        stdin = FakeStdin()
        stdout = SlowStdout()

    path = tmp_path / "bot"
    path.write_bytes(b"unused")
    info = BinaryInfo("elf", "linux", "amd64", True)
    runner = BinaryRunner(prefer_local=False)
    session = BotSession("timeout-session", info, path, proc=FakeProc(), mode="docker")
    session._stderr_tail.extend(b"/private/bot_uploads/secret-version")
    runner._sessions[session.session_id] = session

    with caplog.at_level("WARNING"), pytest.raises(TimeoutError):
        asyncio.run(runner.send(session.session_id, "{}", timeout=0.001))
    assert "timeout-session" in caplog.text
    assert "/private" not in caplog.text
    assert "secret-version" not in caplog.text


def test_container_exit_126_127_require_trusted_runtime_marker(tmp_path):
    """Bot 可自行返回 126/127；普通 stderr 不得用于逃避判负。"""

    class FakeProc:
        def __init__(self, returncode: int):
            self.returncode = returncode

        async def wait(self):
            return self.returncode

    path = tmp_path / "bot.exe"
    path.write_bytes(b"unused")
    info = BinaryInfo("pe", "windows", "amd64", True)

    async def classify(returncode: int, stderr: str, *, trusted: bool):
        runner = BinaryRunner(prefer_local=False)
        session = BotSession(
            "wine-exit", info, path, proc=FakeProc(returncode), mode="wine"
        )
        token = session.runtime_error_token if trusted else "forged-token"
        session._stderr_tail.extend(
            f"BZPLAT_RUNTIME_ERROR:{token}:{stderr}".encode()
        )
        return await runner._process_exit_error(session, "stdout EOF")

    for code in (126, 127):
        forged = asyncio.run(classify(code, "wine_not_found", trusted=False))
        assert isinstance(forged, BotCrashedError)

    trusted = asyncio.run(classify(127, "wine_not_found", trusted=True))
    assert isinstance(trusted, PlatformRunnerError)
    assert "wine_not_found" in str(trusted)

    unknown = asyncio.run(classify(127, "bot_claimed_runtime_error", trusted=True))
    assert isinstance(unknown, BotCrashedError)

    # 可信标记也不得把普通 Bot 退出码 1 升格为平台错误。
    ordinary = asyncio.run(classify(1, "wine_not_found", trusted=True))
    assert isinstance(ordinary, BotCrashedError)


def test_platform_exit_waits_for_stderr_drain_race(tmp_path):
    """proc 先退出、stderr drain 稍后到达时仍须识别可信诊断。"""

    class FakeProc:
        returncode = 126

        async def wait(self):
            return self.returncode

    path = tmp_path / "bot.exe"
    path.write_bytes(b"unused")
    info = BinaryInfo("pe", "windows", "amd64", True)

    async def classify_after_delayed_stderr():
        runner = BinaryRunner(prefer_local=False)
        session = BotSession(
            "wine-race", info, path, proc=FakeProc(), mode="wine"
        )

        async def delayed_drain():
            await asyncio.sleep(0.01)
            marker = (
                f"BZPLAT_RUNTIME_ERROR:{session.runtime_error_token}:"
                "wine_not_executable"
            )
            session._stderr_tail.extend(marker.encode())

        session._stderr_task = asyncio.create_task(delayed_drain())
        return await runner._process_exit_error(session, "stdout EOF")

    failure = asyncio.run(classify_after_delayed_stderr())
    assert isinstance(failure, PlatformRunnerError)
    assert "wine_not_executable" in str(failure)


def test_docker_and_wine_argv_enforce_same_sandbox_baseline(tmp_path, monkeypatch):
    """ELF/PE 两条生产容器路径都必须在 argv 层强制硬隔离。"""
    captured: list[tuple[str, ...]] = []

    class FakeProc:
        returncode = None

    async def capture_spawn(*args, **_kwargs):
        captured.append(args)
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture_spawn)
    runner = BinaryRunner(prefer_local=False)

    elf_path = tmp_path / "bot"
    pe_path = tmp_path / "bot.exe"
    elf_path.write_bytes(b"unused")
    pe_path.write_bytes(b"unused")
    elf_session = BotSession(
        "elf-argv", BinaryInfo("elf", "linux", "amd64", True), elf_path,
        mode="docker",
    )
    wine_session = BotSession(
        "wine-argv", BinaryInfo("pe", "windows", "amd64", True), pe_path,
        mode="wine",
    )

    asyncio.run(runner._start_docker(elf_session))
    asyncio.run(runner._start_wine(wine_session))
    assert len(captured) == 2

    for args in captured:
        assert "--network=none" in args
        assert "--read-only" in args
        assert "--cap-drop=ALL" in args
        assert ("--security-opt", "no-new-privileges") == (
            args[args.index("--security-opt")],
            args[args.index("--security-opt") + 1],
        )
        assert ("--user", "65534:65534") == (
            args[args.index("--user")], args[args.index("--user") + 1]
        )
        mounts = [args[i + 1] for i, arg in enumerate(args[:-1]) if arg == "-v"]
        assert mounts and all(mount.endswith(":ro") for mount in mounts)

    wine_args = captured[1]
    tmpfs_values = [
        wine_args[i + 1]
        for i, arg in enumerate(wine_args[:-1])
        if arg == "--tmpfs"
    ]
    assert any(value.startswith("/tmp:") for value in tmpfs_values)
    assert any(value.startswith("/winehome:") for value in tmpfs_values)
    assert "HOME=/winehome" in wine_args
    assert "WINEPREFIX=/winehome/prefix" in wine_args
    assert "XDG_RUNTIME_DIR=/winehome/runtime" in wine_args
    assert ("--entrypoint", "/bin/sh") == (
        wine_args[wine_args.index("--entrypoint")],
        wine_args[wine_args.index("--entrypoint") + 1],
    )
    assert wine_session.runtime_error_token in wine_args[-1]


def test_broken_stdin_uses_same_docker_exit_classification(tmp_path):
    """docker can exit before stdout is read; BrokenPipe must not be swallowed."""

    class BrokenStdin:
        def write(self, _data):
            return None

        async def drain(self):
            raise BrokenPipeError("connection lost")

    class UnusedStdout:
        async def readline(self):
            raise AssertionError("stdout must not be read after broken stdin")

    class FakeProc:
        def __init__(self, returncode):
            self.returncode = returncode
            self.stdin = BrokenStdin()
            self.stdout = UnusedStdout()

        async def wait(self):
            return self.returncode

    path = tmp_path / "bot"
    path.write_bytes(b"unused")
    info = BinaryInfo("elf", "linux", "amd64", True)

    infra = BinaryRunner(prefer_local=False)
    infra._sessions["infra-pipe"] = BotSession(
        "infra-pipe", info, path, proc=FakeProc(125), mode="docker"
    )
    with pytest.raises(PlatformRunnerError, match="docker exit 125"):
        asyncio.run(infra.send("infra-pipe", "{}"))

    bot_fault = BinaryRunner(prefer_local=False)
    bot_fault._sessions["bot-pipe"] = BotSession(
        "bot-pipe", info, path, proc=FakeProc(126), mode="docker"
    )
    with pytest.raises(BotCrashedError, match="stdin"):
        asyncio.run(bot_fault.send("bot-pipe", "{}"))


def test_docker_spawn_oserror_is_sanitized_platform_fault(monkeypatch):
    if not ELF.is_file():
        pytest.skip("sample ELF missing")
    runner = BinaryRunner(prefer_local=False)
    runner._docker_ok = True

    async def fail_spawn(_session):
        raise OSError("/private/server/path: too many open files")

    monkeypatch.setattr(runner, "_start_docker", fail_spawn)
    with pytest.raises(PlatformRunnerError) as failure:
        asyncio.run(runner.start_session(ELF))
    assert "OSError" in str(failure.value)
    assert "/private/server/path" not in str(failure.value)


def test_match_runner_does_not_swallow_platform_fault_as_default_move():
    class FailingTransport:
        def __init__(self):
            self._sessions = {}
            self._next = 0

        async def start_session(self, binary_path, *, runtime_mode="longrunning", **_kwargs):
            from types import SimpleNamespace

            self._next += 1
            sid = f"s{self._next}"
            self._sessions[sid] = SimpleNamespace(
                binary_path=Path(binary_path),
                runtime_mode=runtime_mode,
                requests=[],
                responses=[],
                turn=0,
                long_running=False,
            )
            return sid

        async def send(self, *_args, **_kwargs):
            raise PlatformRunnerError("docker daemon unavailable")

        async def read_extra_line(self, *_args, **_kwargs):
            return None

        async def stop_session(self, session_id):
            self._sessions.pop(session_id, None)

    runner = MatchRunner(FailingTransport())
    with pytest.raises(PlatformRunnerError, match="daemon unavailable"):
        asyncio.run(runner.run_binaries("/bot/a", "/bot/b", game_id="gomoku"))


def test_match_two_callbots_short():
    if not ELF.is_file():
        pytest.skip("sample ELF missing")
    runner = MatchRunner(BinaryRunner(prefer_local=True))
    # 手数已钉死 DEFAULT_HANDS（70，#123 游戏参数固定）——num_hands 参数被忽略
    result = asyncio.run(runner.run_binaries(str(ELF), str(ELF), seed=1))
    from bzplat.backend.games.holdem.engine import DEFAULT_HANDS
    assert result.hands_played == DEFAULT_HANDS
    # Botzone 计分：final_chips = 累计净输赢 net（零和），不再守恒于 2*STARTING_STACK
    assert sum(result.final_chips) == 0


def test_protocol_roundtrip():
    from bzplat.backend.games.holdem.holdem_judge import Card, Suit

    req = proto.build_act_request(
        hand=0,
        total_hands=70,
        my_id=0,
        dealer_id=0,
        my_cards=[Card(Suit.SPADE, 14), Card(Suit.HEART, 13)],
        board=[],
        history=[],
        my_chips=20000,
    )
    line = proto.dumps_request(req)
    # Botzone 全名字段
    assert '"num_players":2' in line
    assert '"dealer_id":0' in line
    assert '"my_cards":[' in line
    # 裸整数 response 解析（信封 + 裸 int 两种）
    action, _x = proto.parse_response({"response": 0})
    assert action == "call"
    action, _x = proto.parse_response(0)
    assert action == "call"


def test_orchestrator_resolves_holdem_winner_non_null():
    """回归：holdem 多手对局 winner 不得被 match_end.winner=None 覆盖成平局。

    场景：foldbot vs callbot（foldbot 每手弃牌输盲注，70 手后 callbot 净筹码远高）。
    orchestrator 应按 ea/eb 判 winner=1（callbot 胜），而非 None（平局）。
    根因修复（L298-303）：仅当 match_end.winner 非 None 才覆盖兜底判定的 winner。
    """
    import os
    os.environ.setdefault("BZ_BOT_LOCAL", "1")
    foldbot = SAMPLES / "holdem_bots" / "foldbot"
    callbot = ELF
    if not foldbot.is_file() or not callbot.is_file():
        pytest.skip("foldbot/callbot binary missing")

    from bzplat.backend.matches.orchestrator import MatchOrchestrator
    from bzplat.backend.store import Store
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        store = Store(str(td + "/w.db"))
        u = store.create_user("wintest", "w@e.com", "x")
        ba = store.create_bot(u["id"], "foldbotA", binary_path=str(foldbot), format="elf", game_id="holdem")
        bb = store.create_bot(u["id"], "callbotB", binary_path=str(callbot), format="elf", game_id="holdem")
        store.ensure_rating(ba["id"]); store.ensure_rating(bb["id"])
        orch = MatchOrchestrator(store, runner=MatchRunner(BinaryRunner(prefer_local=True)), max_concurrent=1)
        import asyncio

        async def _run():
            mid = await orch.challenge(ba["id"], bb["id"], u["id"], match_config={"hands": 10}, game_id="holdem")
            task = orch._tasks.get(mid)
            if task:
                await asyncio.wait_for(task, timeout=60)
            return mid

        mid = asyncio.run(_run())
        m = store.get_match(mid)
        # foldbot 每手弃 → callbot 净筹码高 → winner 应是 1（非 None 平局）
        assert m["winner"] is not None, (
            f"holdem 多手 winner 不应是 None（平局）；result={m.get('result')}"
        )
        assert m["winner"] == 1, f"callbot 应胜（foldbot 每手弃），winner={m['winner']}"
        store.close()


def test_challenge_validates_match_params_per_game():
    """#123：游戏规则参数（手数等）已钉死固定值，challenge 的 match_config 不再控制手数。

    旧测试断言 hands=999/hands=0 触发 ValueError；现手数固定 DEFAULT_HANDS（70），
    match_config 里的 hands 字段被忽略（不校验、不生效）。本测试验证：传任意 hands
    不再触发校验错误（challenge 成功建 match 或因 /dev/null 崩，但非校验失败）。
    """
    import os
    import tempfile

    from bzplat.backend.matches.orchestrator import MatchOrchestrator
    from bzplat.backend.store import Store

    os.environ.setdefault("BZ_BOT_LOCAL", "1")
    with tempfile.TemporaryDirectory() as td:
        store = Store(str(td + "/v.db"))
        u = store.create_user("vtest", "v@e.com", "x")
        ba = store.create_bot(u["id"], "vbotA", binary_path="/dev/null", format="elf", game_id="holdem")
        bb = store.create_bot(u["id"], "vbotB", binary_path="/dev/null", format="elf", game_id="holdem")
        store.ensure_rating(ba["id"]); store.ensure_rating(bb["id"])
        orch = MatchOrchestrator(store, runner=MatchRunner(BinaryRunner(prefer_local=True)), max_concurrent=1)

        # hands 参数被忽略（手数固定），不触发校验错误
        for cfg in ({"hands": 999}, {"hands": 0}, {"hands": 100}, {}):
            try:
                asyncio.run(orch.challenge(ba["id"], bb["id"], u["id"], match_config=cfg, game_id="holdem"))
            except ValueError as e:
                assert "match 参数非法" not in str(e), f"cfg={cfg} 不应触发校验失败（手数固定）"
            except Exception:
                pass  # /dev/null 跑不起来是预期的，校验已过即可
        store.close()
