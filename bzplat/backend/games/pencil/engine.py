"""点格棋引擎（适配层）——对齐 Botzone Pencil 官方 C++ 裁判。

本文件是**平台协议适配层**：调 decide → 经 protocol 构造请求/解析响应 → 驱动纯裁判
（pencil_judge.py 的 PencilBoard）做规则判定 → emit 平台事件 → 返回 MatchResult。

纯游戏规则（交错网格/占边/成格连走/多数胜/归属追踪）在 pencil_judge.py，0 平台依赖。
"""
from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from typing import Any, Callable

from bzplat.backend.games.pencil.result import MatchResult, RoundResult
from bzplat.backend.games.pencil import protocol as proto
from bzplat.backend.games.pencil.pencil_judge import (
    DEFAULT_N,
    PencilBoard,
)
from bzplat.backend.runtime.binary_runner import (
    BotCrashedError,
    BotTechnicalError,
    PlatformRunnerError,
)

DecideFn = Callable[[int, dict[str, Any]], Any]
EventFn = Callable[[str, dict[str, Any]], Any]


@dataclass
class PencilSession:
    n_dots: int = DEFAULT_N
    on_event: EventFn | None = None
    events: list[dict[str, Any]] = field(default_factory=list)

    async def _emit_async(self, kind: str, **payload: Any) -> None:
        ev = {"type": kind, **payload}
        self.events.append(ev)
        if self.on_event is not None:
            out = self.on_event(kind, ev)
            if inspect.isawaitable(out):
                await out

    async def _decide(
        self, decide: DecideFn, player: int, request: dict[str, Any]
    ) -> dict[str, Any]:
        out = decide(player, request)
        if inspect.isawaitable(out):
            out = await out
        return out if isinstance(out, dict) else {}

    async def run_async(self, decide: DecideFn) -> MatchResult:
        g = PencilBoard(self.n_dots)
        await self._emit_async(
            "match_start",
            game_id="pencil",
            n_dots=self.n_dots,
            size=g.size,
            first=0,
            scores=[0, 0],
        )

        to_move = 0
        last_x, last_y = -1, -1
        pass_flag = 0
        winner: int | None = None
        reason = "completed"
        moves_n = 0
        max_boxes = (self.n_dots - 1) ** 2
        min_win = g.min_win()
        # 最终 scores（正常终局用实时分；非法/崩溃归一化 2-0）
        final_scores: list[int] | None = None

        while g.remaining_edges() > 0 or pass_flag == 1:
            # 若边已尽且非 pass 回合，结束
            if pass_flag == 0 and g.remaining_edges() == 0:
                break

            req = proto.build_pencil_request(
                x=last_x,
                y=last_y,
                pass_=pass_flag,
                me=to_move,
                scores=list(g.scores),
            )
            await self._emit_async(
                "turn",
                player=to_move,
                pass_=pass_flag,
                last={"x": last_x, "y": last_y},
                scores=list(g.scores),
            )
            try:
                raw = await self._decide(decide, to_move, req)
            except PlatformRunnerError:
                raise
            except BotTechnicalError:
                raise
            except BotCrashedError:
                # 对齐裁判：bot 崩溃不可恢复 → 判负 2-0（不再中止整场）。
                winner = 1 - to_move
                reason = "crash"
                await self._emit_async(
                    "illegal", player=to_move, move={"x": None, "y": None}, why="crash"
                )
                break
            except Exception:
                winner = 1 - to_move
                reason = "error"
                await self._emit_async(
                    "illegal", player=to_move, move={"x": None, "y": None}, why="error"
                )
                break

            mx, my = proto.parse_xy(raw)

            if pass_flag == 1:
                if mx != -1 or my != -1:
                    winner = 1 - to_move
                    reason = "illegal"
                    await self._emit_async(
                        "illegal", player=to_move, move={"x": mx, "y": my}, why="pass"
                    )
                    break
                # 对方连走：pass 后把回合交回连走方
                await self._emit_async("pass", player=to_move)
                to_move = 1 - to_move
                pass_flag = 0
                last_x, last_y = -1, -1
                continue

            if mx is None or my is None or not g.is_legal_edge(mx, my):
                winner = 1 - to_move
                reason = "illegal"
                await self._emit_async(
                    "illegal",
                    player=to_move,
                    move={"x": mx, "y": my},
                    why="illegal_move",
                )
                break

            g.curr_player = to_move
            closed = g.do_action(mx, my)
            scored = len(closed) > 0
            moves_n += 1
            await self._emit_async(
                "move",
                player=to_move,
                x=mx,
                y=my,
                scored=scored,
                scores=list(g.scores),
                move_index=moves_n,
                closed_boxes=[{"x": bx, "y": by, "owner": g.box_owner[(bx, by)]} for bx, by in closed],
            )

            # 多数胜提前结束（对齐裁判 hasPlayerWon）
            if g.scores[to_move] >= min_win:
                winner = to_move
                reason = "majority"
                break

            if sum(g.scores) >= max_boxes:
                # 全部格子已占完
                break

            if scored:
                # 通知对方 pass，再由本方连走
                last_x, last_y = mx, my
                pass_flag = 1
                to_move = 1 - to_move
            else:
                last_x, last_y = mx, my
                pass_flag = 0
                to_move = 1 - to_move

        # 计算最终 scores（非法/崩溃归一化 2-0，对齐裁判）
        if winner is not None and reason in ("illegal", "error", "crash"):
            final_scores = [2, 0] if winner == 0 else [0, 2]
        else:
            final_scores = list(g.scores)

        if winner is None:
            sa, sb = final_scores
            if sa > sb:
                winner = 0
                reason = "score"
            elif sb > sa:
                winner = 1
                reason = "score"
            else:
                winner = None
                reason = "draw"

        deltas = [final_scores[0] - final_scores[1], final_scores[1] - final_scores[0]]
        round_result = RoundResult(
            winners=[winner] if winner is not None else [],
            deltas=deltas,
        )
        await self._emit_async(
            "match_end",
            game_id="pencil",
            winner=winner,
            reason=reason,
            scores=list(final_scores),
            moves=moves_n,
            box_owners=g.box_owners_grid(),
        )
        return MatchResult(
            rounds_played=moves_n,
            rounds=[round_result],
            events=self.events,
            winner=winner,
            reason=reason,
            scores=list(final_scores),
            moves=moves_n,
        )

    def run(self, decide: DecideFn) -> MatchResult:
        return asyncio.run(self.run_async(decide))
