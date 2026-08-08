"""裁判规则参数（settings → 引擎）贯通测试。

验证：settings 写入后 `_judge_params` 能读到、`run_session` 用新参数构造 Session、
缺失/非法 settings 时用引擎常量兜底；admin 端点 GET/PATCH 的鉴权与范围校验。
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from bzplat.backend.crypto import hash_password
from bzplat.backend.games.gomoku.engine import BOARD_SIZE
from bzplat.backend.games import run_session
from bzplat.backend.main import create_app
from bzplat.backend.matches.orchestrator import MatchOrchestrator
from bzplat.backend.matches.runner import MatchRunner
from bzplat.backend.runtime.binary_runner import BinaryRunner
from bzplat.backend.store import Store
from bzplat.backend.store.schema import (
    SETTING_JUDGE_HOLDEM_BB,
    SETTING_JUDGE_HOLDEM_SB,
    SETTING_JUDGE_HOLDEM_STACK,
)


@pytest.fixture()
def store(tmp_path):
    return Store(str(tmp_path / "judge.db"))


@pytest.fixture()
def orch(store):
    runner = MatchRunner(BinaryRunner(prefer_local=True))
    return MatchOrchestrator(store, runner=runner, max_concurrent=1)


# ── _judge_params 读取与兜底（per-game，PR2 起 _judge_params(gid)）─────────

def test_judge_params_defaults_when_unset(orch):
    """未写 settings 时该游戏字段返回 None（交由引擎常量兜底）。

    游戏规则参数（手数/棋盘/点阵）已钉死，不在 judge_params 声明；
    holdem 仅 stack/sb/bb 三个字段。
    """
    # holdem：3 个字段全 None（手数已钉死，移除 num_hands）
    jp_h = orch._judge_params("holdem")
    assert jp_h == {"starting_stack": None, "sb": None, "bb": None}
    # gomoku：棋盘边长已钉死，无 judge 参数
    jp_g = orch._judge_params("gomoku")
    assert jp_g == {}
    # pencil：无 judge 参数
    jp_p = orch._judge_params("pencil")
    assert jp_p == {}


def test_judge_params_reads_settings(orch, store):
    """写入合法 settings 后 _judge_params(gid) 读到对应值（按游戏分组）。"""
    store.set_setting(SETTING_JUDGE_HOLDEM_STACK, "5000")
    store.set_setting(SETTING_JUDGE_HOLDEM_SB, "25")
    store.set_setting(SETTING_JUDGE_HOLDEM_BB, "50")
    h = orch._judge_params("holdem")
    assert h["starting_stack"] == 5000 and h["sb"] == 25 and h["bb"] == 50
    # num_hands 已钉死，不再作为 judge 参数返回


def test_judge_params_bad_values_fall_back(orch, store):
    """非法值（负数/非数字/空串）返回 None，由引擎兜底。"""
    store.set_setting(SETTING_JUDGE_HOLDEM_STACK, "0")
    store.set_setting(SETTING_JUDGE_HOLDEM_BB, "-5")
    store.set_setting(SETTING_JUDGE_HOLDEM_SB, "")
    h = orch._judge_params("holdem")
    assert h["starting_stack"] is None and h["sb"] is None and h["bb"] is None


# ── run_session 用新参数构造 Session ──────────────────────────────

def _run_callables(game_id, **kw):
    """两个「总下第一空点」的 callable bot 自打自一局。"""
    import random

    rng = random.Random(0)

    async def make_decide(board_state):
        async def decide(player_idx, request):
            return board_state[player_idx].play(request)
        return decide

    # gomoku: 每方维护本地棋盘，随机/顺序下空点
    if game_id == "gomoku":
        class GState:
            def __init__(self):
                self.b = [[-1] * 99 for _ in range(99)]
                self.i = 0
            def play(self, req):
                me = int(req.get("me", 0))
                ox, oy = int(req.get("x", -1)), int(req.get("y", -1))
                if ox >= 0:
                    self.b[ox][oy] = 1 - me
                # 顺序找一个空点
                x = self.i
                self.i += 1
                self.b[x][0] = me
                return {"x": x, "y": 0}
        sa, sb = GState(), GState()

        async def decide(player_idx, request):
            return sa.play(request) if player_idx == 0 else sb.play(request)

        return asyncio.run(run_session(game_id, decide, rng=rng, **kw))

    raise ValueError(f"unsupported game in helper: {game_id}")


def test_gomoku_board_size_pinned_to_default():
    """棋盘边长已钉死 BOARD_SIZE（15），即使传 board_size=9 也被忽略，仍用 15×15。"""
    result = _run_callables("gomoku", board_size=9)  # 传 9 但 spec 忽略，用固定 15
    assert result.reason in ("five", "illegal", "draw")
    # 15×15 固定，回合数上限 15*15+1
    assert result.rounds_played <= 15 * 15 + 1


def test_gomoku_default_board_size():
    """不传 board_size 时用默认 15×15。"""
    result = _run_callables("gomoku")
    assert result.reason in ("five", "illegal", "draw")
    assert result.rounds_played <= 15 * 15 + 1


# ── runner 透传 ───────────────────────────────────────────────────

def test_runner_passes_judge_params_to_session(monkeypatch):
    """run_callables 把 board_size 透传给 run_session（patch run_session 捕获）。"""
    captured: dict = {}

    async def fake_run_session(game_id, decide, **kw):
        captured.update(kw)
        from bzplat.backend.games.base import MatchResult
        return MatchResult(rounds_played=0)

    runner = MatchRunner(BinaryRunner(prefer_local=True))
    monkeypatch.setattr("bzplat.backend.matches.runner.run_session", fake_run_session)

    async def decide_a(req):
        return {"x": 0, "y": 0}

    async def decide_b(req):
        return {"x": 0, "y": 0}

    asyncio.run(
        runner.run_callables(
            decide_a, decide_b, game_id="gomoku",
            board_size=13, starting_stack=10000, sb=25, bb=50,
        )
    )
    assert captured["board_size"] == 13
    assert captured["starting_stack"] == 10000
    assert captured["sb"] == 25
    assert captured["bb"] == 50


def test_gomoku_check_win_respects_smaller_board():
    """回归：check_win 必须用实际 size 判定边界，而非默认 15。

    历史 bug：check_win 默认 size=15，9×9 棋盘下沿 4 方向扫描到 [9,15) 时
    越界访问 board[cx][cy] 抛 IndexError。这里显式传 size=9 验证不再越界。
    """
    from bzplat.backend.games.gomoku.engine import check_win

    size = 9
    board = [[-1] * size for _ in range(size)]
    # 在右下角附近横排成五：x=4..8, y=8（贴边，触发向右扫描到 size 边界）
    for x in range(4, 9):
        board[x][8] = 0
    assert check_win(board, 8, 8, 0, size) is True
    # 未成五的一手不应误报
    board2 = [[-1] * size for _ in range(size)]
    for x in range(4, 8):  # 仅 4 连
        board2[x][8] = 0
    assert check_win(board2, 7, 8, 0, size) is False


def test_gomoku_small_board_full_game_via_runner():
    """回归：board_size=9 时两合法 bot 能跑完整局不崩（曾因 check_win 越界崩溃）。"""
    runner = MatchRunner(BinaryRunner(prefer_local=True))
    size = 9

    def make_bot():
        b = [[-1] * size for _ in range(size)]

        async def decide(req):
            me = int(req.get("me", 0))
            ox, oy = int(req.get("x", -1)), int(req.get("y", -1))
            if 0 <= ox < size and 0 <= oy < size:
                b[ox][oy] = 1 - me
            for x in range(size):
                for y in range(size):
                    if b[x][y] < 0:
                        b[x][y] = me
                        return {"x": x, "y": y}
            return {"x": -1, "y": -1}

        return decide

    result = asyncio.run(
        runner.run_callables(make_bot(), make_bot(), game_id="gomoku", board_size=size)
    )
    assert result.reason in ("five", "draw")
    assert result.rounds_played <= size * size + 1


# ── admin 端点：鉴权 + 范围校验 + 热生效 ──────────────────────────

def _admin_client(tmp_path):
    db = str(tmp_path / "judge_http.db")
    app = create_app(db_path=db, max_concurrent=1)
    store: Store = app.state.store
    u = store.create_user("admin", "a@ex.com", hash_password("password12"), role="admin")
    store.update_user(u["id"], email_verified=1)
    _, token = app.state.auth.authenticate("admin", "password12")
    client = TestClient(app)
    client.headers["Authorization"] = f"Bearer {token}"
    return client, app


def _plain_client(tmp_path):
    """普通用户客户端（用于验证 403）。"""
    db = str(tmp_path / "judge_plain.db")
    app = create_app(db_path=db, max_concurrent=1)
    store: Store = app.state.store
    u = store.create_user("plain", "p@ex.com", hash_password("password12"), role="user")
    store.update_user(u["id"], email_verified=1)
    _, token = app.state.auth.authenticate("plain", "password12")
    client = TestClient(app)
    client.headers["Authorization"] = f"Bearer {token}"
    return client


def test_get_judges_returns_all_games(tmp_path):
    client, _ = _admin_client(tmp_path)
    r = client.get("/api/admin/judges")
    assert r.status_code == 200
    data = r.json()
    gids = [g["game_id"] for g in data["games"]]
    assert gids == ["holdem", "gomoku", "pencil"]
    # 每款游戏带代码位置与 docstring
    for g in data["games"]:
        assert g["code_path"].endswith(".py")
        assert isinstance(g["docstring"], str)
    # holdem 有 4 个参数，gomoku 1 个，pencil/reversi 0 个
    holdem = next(g for g in data["games"] if g["game_id"] == "holdem")
    gomoku = next(g for g in data["games"] if g["game_id"] == "gomoku")
    pencil = next(g for g in data["games"] if g["game_id"] == "pencil")
    # holdem 3 个（stack/sb/bb；手数已钉死）；gomoku 0 个（棋盘已钉死）；pencil 0 个
    assert len(holdem["params"]) == 3
    assert len(gomoku["params"]) == 0
    assert pencil["params"] == []


def test_get_judges_non_admin_forbidden(tmp_path):
    client = _plain_client(tmp_path)
    r = client.get("/api/admin/judges")
    assert r.status_code == 403


def test_patch_judge_params_updates_and_hot(tmp_path):
    client, app = _admin_client(tmp_path)
    # gomoku 棋盘边长已钉死，不再可调；仅 patch holdem 的 sb
    r = client.patch(
        "/api/admin/judges/params",
        json={"params": {"judge_holdem_sb": 25}},
    )
    assert r.status_code == 200, r.text
    updated = r.json()["updated"]
    assert updated["judge_holdem_sb"] == 25
    # 热生效：_judge_params(gid) 立即读到新值
    jp_holdem = app.state.orch._judge_params("holdem")
    assert jp_holdem["sb"] == 25
    # 返回的 judges 总览也反映新值
    holdem = next(g for g in r.json()["judges"]["games"] if g["game_id"] == "holdem")
    sb_param = next(p for p in holdem["params"] if p["key"] == "judge_holdem_sb")
    assert sb_param["value"] == 25


def test_patch_judge_params_out_of_bounds_rejected(tmp_path):
    client, _ = _admin_client(tmp_path)
    r = client.patch(
        "/api/admin/judges/params",
        json={"params": {"judge_holdem_bb": 1}},  # 下限 2
    )
    assert r.status_code == 400


def test_patch_judge_params_bb_le_sb_rejected(tmp_path):
    client, _ = _admin_client(tmp_path)
    r = client.patch(
        "/api/admin/judges/params",
        json={"params": {"judge_holdem_sb": 100, "judge_holdem_bb": 100}},
    )
    assert r.status_code == 400
    assert "盲" in r.json()["detail"]


def test_patch_judge_params_unknown_key_rejected(tmp_path):
    client, _ = _admin_client(tmp_path)
    r = client.patch(
        "/api/admin/judges/params",
        json={"params": {"judge_unknown": 1}},
    )
    assert r.status_code == 400


def test_patch_judge_params_non_admin_forbidden(tmp_path):
    client = _plain_client(tmp_path)
    r = client.patch(
        "/api/admin/judges/params",
        json={"params": {"judge_holdem_sb": 50}},
    )
    assert r.status_code == 403


def test_time_budget_only_pencil():
    """仅点格棋有 time_budget_per_side（象棋钟）；gomoku/holdem 为 None。"""
    from bzplat.backend.games import registry
    assert registry.get("pencil").time_budget_per_side == 900.0
    assert registry.get("gomoku").time_budget_per_side is None
    assert registry.get("holdem").time_budget_per_side is None
