"""固定裁判规则回归：旧 settings/admin 入口不再影响现行引擎。"""
from __future__ import annotations

import asyncio
from pathlib import Path

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


@pytest.fixture()
def store(tmp_path):
    return Store(str(tmp_path / "judge.db"))


@pytest.fixture()
def orch(store):
    runner = MatchRunner(BinaryRunner(prefer_local=True))
    return MatchOrchestrator(store, runner=runner, max_concurrent=1)


def test_removed_judge_setting_rows_are_not_read_by_orchestrator(store):
    """数据库中的历史键只是无效数据，不得再注入任何对局。"""
    store.set_setting("judge_holdem_starting_stack", "5000")
    store.set_setting("judge_holdem_sb", "25")
    store.set_setting("judge_holdem_bb", "50")
    fixture_dir = Path(store.path).resolve().parent / "bot-fixtures"
    fixture_dir.mkdir(parents=True, exist_ok=True)
    path_a = fixture_dir / "fixed-a"
    path_b = fixture_dir / "fixed-b"
    path_a.write_bytes(b"test fixture")
    path_b.write_bytes(b"test fixture")
    user = store.create_user("fixedrules", "fixed@example.com", "x")
    a = store.create_bot(user["id"], "fixed_a", binary_path=str(path_a), format="elf", game_id="holdem")
    b = store.create_bot(user["id"], "fixed_b", binary_path=str(path_b), format="elf", game_id="holdem")
    store.ensure_rating(a["id"])
    store.ensure_rating(b["id"])
    captured: dict = {}

    class FixedRunner:
        runner = None

        def __init__(self):
            self.runner = self

        async def run_binaries(self, *args, **kwargs):
            captured.update(kwargs)

            class Result:
                rounds_played = 0
                rounds = []
                winner = None
                events = []
            return Result()

        async def cleanup_execution(self, scope):
            scope.mark_cleanup_confirmed()

    orchestrator = MatchOrchestrator(store, runner=FixedRunner(), max_concurrent=1)

    async def exercise():
        store.executions.resume()
        request_id = await orchestrator.challenge(
            a["id"], b["id"], user["id"], game_id="holdem"
        )
        job = store.executions.claim_next(
            max_match_slots=1,
            max_sandbox_units=2,
            aging_seconds=60,
            user_active_limit=1,
            contest_share_slots=1,
        )
        assert job is not None and job["public_id"] == request_id
        mid = str(job["current_match_id"])
        orchestrator.start_execution_job(job)
        await orchestrator._tasks[mid]
        assert store.executions.finalize_ready() == 1

    asyncio.run(exercise())
    assert not {"starting_stack", "sb", "bb"}.intersection(captured)
    assert not hasattr(orchestrator, "_judge_params")


# ── run_session 用新参数构造 Session ──────────────────────────────

def _run_callables(game_id, **kw):
    """两个「总下第一空点」的 callable bot 自打自一局。"""
    import random

    rng = random.Random(0)

    async def make_decide(board_state):
        async def decide(player_idx, request):
            return {"response": board_state[player_idx].play(request)}
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
            payload = sa.play(request) if player_idx == 0 else sb.play(request)
            return {"response": payload}

        return asyncio.run(run_session(game_id, decide, rng=rng, **kw))

    raise ValueError(f"unsupported game in helper: {game_id}")


def test_gomoku_board_size_override_is_rejected():
    """平台入口不接受第二套棋盘规则。"""
    with pytest.raises(TypeError, match="Session 不接受参数: board_size"):
        _run_callables("gomoku", board_size=9)


def test_gomoku_default_board_size():
    """不传 board_size 时用默认 15×15。"""
    result = _run_callables("gomoku")
    assert result.reason in ("five", "illegal", "draw")
    assert result.rounds_played <= 15 * 15 + 1


# ── runner 规则入口 ───────────────────────────────────────────────

def test_runner_rejects_removed_rule_params():
    """通用 runner 透传后由目标 spec 统一拒绝旧规则键。"""
    runner = MatchRunner(BinaryRunner(prefer_local=True))

    async def decide_a(req):
        return {"x": 0, "y": 0}

    async def decide_b(req):
        return {"x": 0, "y": 0}

    with pytest.raises(TypeError, match="Session 不接受参数"):
        asyncio.run(
            runner.run_callables(
                decide_a, decide_b, game_id="gomoku", board_size=13,
            )
        )


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


def test_gomoku_small_board_cannot_be_selected_via_runner():
    """纯裁判支持边界单测，不代表平台允许选择 9×9 规则。"""
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

    with pytest.raises(TypeError, match="Session 不接受参数: board_size"):
        asyncio.run(
            runner.run_callables(
                make_bot(), make_bot(), game_id="gomoku", board_size=size,
            )
        )


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


def test_public_judges_is_the_only_rules_listing(tmp_path):
    client, app = _admin_client(tmp_path)
    r = client.get("/api/judges")
    assert r.status_code == 200
    data = r.json()
    gids = [g["game_id"] for g in data["games"]]
    assert gids == ["holdem", "gomoku", "pencil"]
    for g in data["games"]:
        assert g["code_path"].endswith(".py")
        assert "params" not in g
    assert client.get("/api/admin/judges").status_code == 404
    assert client.patch(
        "/api/admin/judges/params",
        json={"params": {"judge_holdem_sb": 25}},
    ).status_code == 404
    assert app.state.store.get_setting("judge_holdem_starting_stack") is None
    assert app.state.store.get_setting("judge_holdem_sb") is None
    assert app.state.store.get_setting("judge_holdem_bb") is None


def test_time_budget_only_pencil():
    """仅点格棋有 time_budget_per_side（象棋钟）；gomoku/holdem 为 None。"""
    from bzplat.backend.games import registry
    assert registry.get("pencil").time_budget_per_side == 900.0
    assert registry.get("gomoku").time_budget_per_side is None
    assert registry.get("holdem").time_budget_per_side is None
