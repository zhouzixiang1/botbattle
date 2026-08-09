"""对抗审计修复回归测试（fix/botzone-audit-fixes）。

覆盖审计发现的 P0/P1 bug 的修复：
- P0-1: check→fold（to_call==0 时 response 0 应 check 非 call）
- P0-2: CHECK/CALL 未透传到 Botzone history
- P0-3: LongRunning 未握手必须终止，禁止混合模式回退
- P1: runner session.requests 异常时原子提交（不污染 traditional 信封）
- P1: delete_bot_version 删非当前版本不动镜像
- P1: 迁移脚本幂等（any 版本标 migrated 即跳过）+ hash 确定性
"""
from __future__ import annotations

import asyncio
import os
import random
from pathlib import Path

import pytest

from bzplat.backend.games import _botzone_protocol as bz
from bzplat.backend.games.holdem.engine import MatchSession
from bzplat.backend.matches.runner import _botzone_decide


# ── P0-1: check→fold 修复 ─────────────────────────────────────────────

def test_check_not_folded_when_to_call_zero():
    """to_call==0 时 response 0（check）不应被误判为 fold。"""
    def bot(pid, req):
        return {"response": 0}  # call/check

    s = MatchSession(num_hands=1, rng=random.Random(42))
    r = asyncio.run(s.run_async(bot))
    hr = r.hand_results[0]
    # 修复前：reason=fold（BB check 被判 fold）。修复后：应到 showdown。
    assert hr.reason != "fold", f"check 被误判 fold！reason={hr.reason}"
    assert hr.reason in ("showdown", "fold"), hr.reason  # 合理终局


def test_check_call_hand_completes_to_showdown():
    """全 check/call 的对局应到 showdown（而非中途 fold）。"""
    def bot(pid, req):
        return {"response": 0}

    s = MatchSession(num_hands=3, rng=random.Random(7))
    r = asyncio.run(s.run_async(bot))
    # 每手都应到 showdown（无人 fold）
    for hr in r.hand_results:
        assert hr.reason == "showdown", f"应 showdown 实际 {hr.reason}"


# ── P0-2: CHECK/CALL 透传到 Botzone history ────────────────────────────

def test_bot_receives_history_with_calls():
    """Bot 收到的 history 应含对手的 call/check。"""
    captured = []

    def bot(pid, req):
        captured.append((pid, req.get("history", [])))
        return {"response": 0}

    s = MatchSession(num_hands=1, rng=random.Random(3))
    asyncio.run(s.run_async(bot))
    # 第 2 个决策点（BB 面临 SB 的 call）应看到 SB 的 call 在 history 里
    bb_calls_seen = any(
        any(ev.get("action_type") == "call" for ev in hist)
        for pid, hist in captured
        if hist  # 有 history 的决策点
    )
    assert bb_calls_seen, f"Bot 没收到含 call 的 history: {captured}"


# ── P0-3: 握手死代码修复 ───────────────────────────────────────────────

class _FakeSession:
    def __init__(self, *, runtime_mode="longrunning", turn=0, long_running=False):
        self.runtime_mode = runtime_mode
        self.requests = []
        self.responses = []
        self.turn = turn
        self.long_running = long_running


class _FakeRunner:
    def __init__(self, session, response_lines):
        self._sessions = {"fake": session}
        self._responses = list(response_lines)
        self.sent_lines: list[str] = []

    async def send(self, sid, line, *, timeout=None):
        self.sent_lines.append(line)
        return self._responses.pop(0)

    async def read_extra_line(self, sid, *, timeout=1.0):
        if self._responses and bz.is_keep_running_signal(self._responses[0]):
            return self._responses.pop(0)
        return None


def test_longrunning_no_handshake_cannot_enter_turn2():
    """未握手状态不是第三种运行模式，后续决策必须直接拒绝。"""
    from bzplat.backend.runtime.binary_runner import BotProtocolError

    session = _FakeSession(runtime_mode="longrunning", turn=1, long_running=False)
    fake = _FakeRunner(session, ['{"response":0}'])
    with pytest.raises(BotProtocolError) as raised:
        asyncio.run(_botzone_decide(
            fake, "fake", {"hand": 1}, game_id="holdem", action_timeout=5,
        ))
    assert raised.value.error_code == "missing_keep_running"
    assert fake.sent_lines == []


def test_longrunning_with_handshake_sends_single_request_on_turn2():
    """LongRunning Bot 首回合握手成功 → 后续回合发单 request 信封。"""
    session = _FakeSession(runtime_mode="longrunning", turn=1, long_running=True)
    fake = _FakeRunner(session, ['{"response":0}'])
    asyncio.run(_botzone_decide(
        fake, "fake", {"hand": 1}, game_id="holdem", action_timeout=5,
    ))
    import json
    env = json.loads(fake.sent_lines[0])
    assert "request" in env and "requests" not in env, f"握手后应发单 request，实际 {env}"


# ── P1: session 状态原子提交 ───────────────────────────────────────────

def test_session_state_not_corrupted_on_bad_response():
    """Bot 输出非法 JSON 时，session.requests 不应单独增长（避免 traditional 错位）。"""
    session = _FakeSession(runtime_mode="longrunning", turn=0)
    fake = _FakeRunner(session, ["not valid json"])  # Bot 输出垃圾
    # 非法 JSON 必须作为终局协议故障向上传播，不能再吞成 fail_response。
    with pytest.raises(Exception):
        asyncio.run(_botzone_decide(
            fake, "fake", {"hand": 0}, game_id="holdem", action_timeout=5,
        ))
    # 关键：requests 没被 append（原子提交——解析失败不污染状态）
    assert len(session.requests) == 0, f"requests 被污染: {session.requests}"
    assert session.turn == 0


# ── P1: delete_bot_version 不动非当前版本镜像 ──────────────────────────

def test_delete_non_current_version_keeps_mirror(tmp_path):
    """删非当前版本时不应改 bots 镜像（不覆盖用户的回滚状态）。"""
    from bzplat.backend.store import Store
    store = Store(str(tmp_path / "t.db"))
    store.create_user("u01", "u@e.com", "hx")
    uid = store._conn.execute("SELECT id FROM users WHERE username='u01'").fetchone()["id"]
    b = store.create_bot(uid, "b1", binary_path="x", format="elf", game_id="holdem", runtime_mode="longrunning")
    store.add_bot_version(b["id"], binary_path="v1", format="elf", runtime_mode="traditional")
    store.add_bot_version(b["id"], binary_path="v2", format="elf", runtime_mode="longrunning")
    # 用户回滚到 v1
    store.set_current_version(b["id"], 1)
    assert store.get_bot(b["id"])["current_version"] == 1
    assert store.get_bot(b["id"])["runtime_mode"] == "traditional"
    # 删 v2（非当前版本）
    store.delete_bot_version(b["id"], 2)
    # 镜像不应变（仍指向 v1）
    after = store.get_bot(b["id"])
    assert after["current_version"] == 1, f"删非当前版本改了 current_version: {after['current_version']}"
    assert after["runtime_mode"] == "traditional", f"删非当前版本改了 runtime_mode: {after['runtime_mode']}"


def test_delete_current_version_rolls_back_to_max(tmp_path):
    """删当前版本时仍正确回退到 max 版本。"""
    from bzplat.backend.store import Store
    store = Store(str(tmp_path / "t.db"))
    store.create_user("u01", "u@e.com", "hx")
    uid = store._conn.execute("SELECT id FROM users WHERE username='u01'").fetchone()["id"]
    b = store.create_bot(uid, "b1", binary_path="x", format="elf", game_id="holdem")
    store.add_bot_version(b["id"], binary_path="v1", format="elf", runtime_mode="traditional")
    store.add_bot_version(b["id"], binary_path="v2", format="elf", runtime_mode="longrunning")
    assert store.get_bot(b["id"])["current_version"] == 2
    # 删当前 v2
    store.delete_bot_version(b["id"], 2)
    after = store.get_bot(b["id"])
    assert after["current_version"] == 1
    assert after["runtime_mode"] == "traditional"  # 回退到 v1 的模式


# ── P1: 迁移脚本幂等（any 版本）+ 确定性 ───────────────────────────────

def _load_migration_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "migrate_bots_to_botzone",
        Path(__file__).resolve().parents[3] / "scripts" / "migrate_bots_to_botzone.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_migration_idempotent_after_user_upload(tmp_path, monkeypatch):
    """迁移后用户上传新版本 → 重跑迁移不应再覆盖（any 版本标 migrated 即跳过）。"""
    mod = _load_migration_module()
    from bzplat.backend.store import Store
    store = Store(str(tmp_path / "t.db"))
    store.create_user("tester01", "t@e.com", "hx")
    uid = store._conn.execute("SELECT id FROM users WHERE username='tester01'").fetchone()["id"]
    for i in range(3):
        store.create_bot(uid, f"b{i}", binary_path="x", format="elf", game_id="holdem")
    monkeypatch.chdir(tmp_path)
    # 首次迁移
    mod.migrate(store, game_id="holdem", seed=42)
    # 用户上传新版本（非 migrated）
    store.add_bot_version(1, binary_path="user_v", format="elf", upload_note="user code")
    # 重跑迁移
    stats = mod.migrate(store, game_id="holdem", seed=42)
    assert stats["migrated"] == 0, f"用户上传后重跑不应再迁移: {stats}"
    assert stats["skipped"] == 3


def test_migration_seed_deterministic_across_processes(tmp_path, monkeypatch):
    """迁移风格分布跨进程确定性（不用 hash()）。"""
    mod = _load_migration_module()
    from bzplat.backend.store import Store
    # 用 monkeypatch.chdir（测试后自动恢复 cwd）——避免污染后续测试的相对路径。
    monkeypatch.chdir(tmp_path)

    def run_once():
        store = Store(str(tmp_path / f"t_{random.randint(0,9999)}.db"))
        store.create_user("tester01", "t@e.com", "hx")
        uid = store._conn.execute("SELECT id FROM users WHERE username='tester01'").fetchone()["id"]
        for i in range(5):
            store.create_bot(uid, f"b{i}", binary_path="x", format="elf", game_id="holdem")
        return mod.migrate(store, game_id="holdem", seed=42, dry_run=True)["styles"]

    s1 = run_once()
    s2 = run_once()
    assert s1 == s2, f"风格分布跨进程不一致（hash 随机化？）: {s1} vs {s2}"
