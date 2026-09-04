"""全国机器博弈竞赛五子棋适配层。

状态机严格执行：指定开局 → 三手交换 → 白4 → 五手二打 → 正常行棋。
纯棋规（开局几何、棋盘、禁手）留在 ``gomoku_judge.py``/``forbidden.py``；
本模块只负责协议、参赛座位与棋色映射、事件和平台结果。
"""
from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from typing import Any, Callable

from bzplat.backend.games.gomoku import protocol as proto
from bzplat.backend.games.gomoku.forbidden import BlackMoveKind, classify_black_move
from bzplat.backend.games.gomoku.gomoku_judge import (
    BLACK,
    BLACK5_CANDIDATE_COUNT,
    BOARD_SIZE,
    CENTER,
    WHITE,
    board_full,
    check_win,
    compute_deltas,
    compute_scores,
    in_board,
    is_legal_move,
    new_board,
    validate_black5_candidates,
    validate_opening,
)
from bzplat.backend.games.gomoku.result import MatchResult, RoundResult
from bzplat.backend.runtime.binary_runner import (
    BotCrashedError,
    BotTechnicalError,
    PlatformRunnerError,
)

DecideFn = Callable[[int, dict[str, Any]], Any]
EventFn = Callable[[str, dict[str, Any]], Any]


_FORBIDDEN_REASONS = {
    BlackMoveKind.OVERLINE: "forbidden_overline",
    BlackMoveKind.DOUBLE_FOUR: "forbidden_double_four",
    BlackMoveKind.DOUBLE_THREE: "forbidden_double_three",
}


@dataclass
class GomokuSession:
    """一局 2025 竞赛规则五子棋；内部 seat 始终为 0/1，棋色可交换。"""

    size: int = BOARD_SIZE
    on_event: EventFn | None = None
    events: list[dict[str, Any]] = field(default_factory=list)

    async def _emit_async(self, kind: str, **payload: Any) -> None:
        event = {"type": kind, **payload}
        self.events.append(event)
        if self.on_event is not None:
            output = self.on_event(kind, event)
            if inspect.isawaitable(output):
                await output

    async def _decide(
        self,
        decide: DecideFn,
        seat: int,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        output = decide(seat, request)
        if inspect.isawaitable(output):
            output = await output
        return output if isinstance(output, dict) else {}

    async def _request_action(
        self,
        decide: DecideFn,
        *,
        phase: str,
        seat: int,
        color: int | None,
        board: list[list[int]],
        seat_colors: list[int],
        n: int | None = None,
        candidates: list[dict[str, int]] | None = None,
        last: dict[str, int] | None = None,
        pass_allowed: bool = False,
    ) -> tuple[dict[str, Any] | None, str | None]:
        request = proto.build_request(
            phase=phase,
            me=seat,
            color=color,
            board=board,
            seat_colors=seat_colors,
            n=n,
            candidates=candidates,
            last=last,
            pass_allowed=pass_allowed,
        )
        await self._emit_async(
            "turn",
            player=seat,
            color=color,
            phase=phase,
            pass_allowed=bool(pass_allowed),
        )
        try:
            raw = await self._decide(decide, seat, request)
        except PlatformRunnerError:
            raise
        except BotTechnicalError:
            raise
        except BotCrashedError:
            return None, "crash"
        except Exception:
            return None, "error"
        return proto.parse_action(raw), None

    async def _emit_illegal(
        self,
        *,
        seat: int,
        phase: str,
        action: dict[str, Any] | None,
        why: str,
    ) -> None:
        await self._emit_async(
            "illegal",
            player=seat,
            phase=phase,
            action=action,
            why=why,
        )

    async def run_async(self, decide: DecideFn) -> MatchResult:
        if self.size != BOARD_SIZE:
            raise ValueError("竞赛五子棋棋盘固定为 15×15")

        board = new_board(self.size)
        seat_colors = [BLACK, WHITE]
        moves_n = 0
        last: dict[str, int] | None = None
        # ``n`` 仍保留在 v2 wire/event 结构中供现有 Bot 与历史回放使用，
        # 但它是裁判常量，不再由开局 Bot 选择或覆盖。
        n = BLACK5_CANDIDATE_COUNT
        opening_code = ""
        candidates: list[dict[str, int]] = []
        winner: int | None = None
        reason = "draw"

        await self._emit_async(
            "match_start",
            game_id="gomoku",
            size=self.size,
            first=0,
            ruleset=proto.RULESET_ID,
            protocol_version=proto.PROTOCOL_VERSION,
        )

        # 1. 开局方（seat 0）提交白2、黑3，并确认固定二打；黑1固定天元。
        action, failure = await self._request_action(
            decide,
            phase=proto.PHASE_OPENING,
            seat=0,
            color=BLACK,
            board=board,
            seat_colors=seat_colors,
        )
        if failure is not None:
            winner, reason = 1, failure
        elif action is None or action.get("action") != proto.ACTION_OPENING:
            winner, reason = 1, "illegal_opening"
            await self._emit_illegal(
                seat=0,
                phase=proto.PHASE_OPENING,
                action=action,
                why="expected_opening",
            )
        else:
            white2 = (int(action["white2"]["x"]), int(action["white2"]["y"]))
            black3 = (int(action["black3"]["x"]), int(action["black3"]["y"]))
            proposed_n = int(action["n"])
            opening_code = validate_opening(white2, black3, proposed_n) or ""
            if not opening_code:
                winner, reason = 1, "illegal_opening"
                await self._emit_illegal(
                    seat=0,
                    phase=proto.PHASE_OPENING,
                    action=action,
                    why=(
                        "five_move_candidate_count_must_be_two"
                        if proposed_n != BLACK5_CANDIDATE_COUNT
                        else "not_one_of_26_openings"
                    ),
                )
            else:
                stones = (
                    (CENTER, CENTER, BLACK),
                    (white2[0], white2[1], WHITE),
                    (black3[0], black3[1], BLACK),
                )
                for x, y, color in stones:
                    board[x][y] = color
                moves_n = 3
                last = {"x": black3[0], "y": black3[1], "color": BLACK}
                await self._emit_async(
                    "opening",
                    player=0,
                    opening_code=opening_code,
                    n=n,
                    black1={"x": CENTER, "y": CENTER},
                    white2={"x": white2[0], "y": white2[1]},
                    black3={"x": black3[0], "y": black3[1]},
                )

        # 2. 另一参赛者决定是否交换棋色。
        if winner is None and reason == "draw":
            action, failure = await self._request_action(
                decide,
                phase=proto.PHASE_SWAP,
                seat=1,
                color=seat_colors[1],
                board=board,
                seat_colors=seat_colors,
                n=n,
                last=last,
            )
            if failure is not None:
                winner, reason = 0, failure
            elif action is None or action.get("action") != proto.ACTION_SWAP:
                winner, reason = 0, "illegal_swap"
                await self._emit_illegal(
                    seat=1,
                    phase=proto.PHASE_SWAP,
                    action=action,
                    why="expected_swap",
                )
            else:
                swapped = bool(action["swap"])
                if swapped:
                    seat_colors.reverse()
                await self._emit_async(
                    "swap",
                    player=1,
                    swapped=swapped,
                    seat_colors=list(seat_colors),
                )

        def seat_for(color: int) -> int:
            return seat_colors.index(color)

        # 3. 最终白方落白4。
        if winner is None and reason == "draw":
            white_seat = seat_for(WHITE)
            action, failure = await self._request_action(
                decide,
                phase=proto.PHASE_WHITE4,
                seat=white_seat,
                color=WHITE,
                board=board,
                seat_colors=seat_colors,
                n=n,
                last=last,
            )
            if failure is not None:
                winner, reason = 1 - white_seat, failure
            elif action is None or action.get("action") != proto.ACTION_MOVE:
                winner, reason = 1 - white_seat, "illegal"
                await self._emit_illegal(
                    seat=white_seat,
                    phase=proto.PHASE_WHITE4,
                    action=action,
                    why="expected_move",
                )
            else:
                x, y = int(action["x"]), int(action["y"])
                if not is_legal_move(board, x, y, self.size):
                    winner, reason = 1 - white_seat, "illegal"
                    await self._emit_illegal(
                        seat=white_seat,
                        phase=proto.PHASE_WHITE4,
                        action=action,
                        why="occupied_or_out_of_board",
                    )
                else:
                    board[x][y] = WHITE
                    moves_n = 4
                    last = {"x": x, "y": y, "color": WHITE}
                    await self._emit_async(
                        "move",
                        player=white_seat,
                        color=WHITE,
                        x=x,
                        y=y,
                        phase=proto.PHASE_WHITE4,
                        move_index=moves_n,
                    )

        # 4. 最终黑方提交两个不同形空点（候选不进入正式棋盘）。
        if winner is None and reason == "draw":
            black_seat = seat_for(BLACK)
            action, failure = await self._request_action(
                decide,
                phase=proto.PHASE_BLACK5_CANDIDATES,
                seat=black_seat,
                color=BLACK,
                board=board,
                seat_colors=seat_colors,
                n=n,
                last=last,
            )
            if failure is not None:
                winner, reason = 1 - black_seat, failure
            elif (
                action is None
                or action.get("action") != proto.ACTION_BLACK5_CANDIDATES
                or len(action.get("points") or []) != n
            ):
                winner, reason = 1 - black_seat, "illegal_candidates"
                await self._emit_illegal(
                    seat=black_seat,
                    phase=proto.PHASE_BLACK5_CANDIDATES,
                    action=action,
                    why="expected_n_distinct_candidates",
                )
            else:
                candidates = [dict(point) for point in action["points"]]
                candidate_points = [
                    (point["x"], point["y"]) for point in candidates
                ]
                if not validate_black5_candidates(
                    board, candidate_points, self.size
                ):
                    winner, reason = 1 - black_seat, "illegal_candidates"
                    await self._emit_illegal(
                        seat=black_seat,
                        phase=proto.PHASE_BLACK5_CANDIDATES,
                        action=action,
                        why="candidate_not_empty_distinct_shape",
                    )
                else:
                    await self._emit_async(
                        "black5_candidates",
                        player=black_seat,
                        n=n,
                        points=[dict(point) for point in candidates],
                    )

        # 5. 最终白方保留一个候选作为唯一真实黑5。
        if winner is None and reason == "draw":
            white_seat = seat_for(WHITE)
            action, failure = await self._request_action(
                decide,
                phase=proto.PHASE_BLACK5_SELECT,
                seat=white_seat,
                color=WHITE,
                board=board,
                seat_colors=seat_colors,
                n=n,
                candidates=candidates,
                last=last,
            )
            if failure is not None:
                winner, reason = 1 - white_seat, failure
            elif action is None or action.get("action") != proto.ACTION_BLACK5_SELECT:
                winner, reason = 1 - white_seat, "illegal_selection"
                await self._emit_illegal(
                    seat=white_seat,
                    phase=proto.PHASE_BLACK5_SELECT,
                    action=action,
                    why="candidate_index_out_of_range",
                )
            else:
                selected_index = int(action["index"])
                if not 0 <= selected_index < len(candidates):
                    winner, reason = 1 - white_seat, "illegal_selection"
                    await self._emit_illegal(
                        seat=white_seat,
                        phase=proto.PHASE_BLACK5_SELECT,
                        action=action,
                        why="candidate_index_out_of_range",
                    )
                else:
                    selected = candidates[selected_index]
                    x, y = int(selected["x"]), int(selected["y"])
                    board[x][y] = BLACK
                    moves_n = 5
                    last = {"x": x, "y": y, "color": BLACK}
                    await self._emit_async(
                        "black5_selected",
                        player=white_seat,
                        index=selected_index,
                        point={"x": x, "y": y},
                    )
                    await self._emit_async(
                        "move",
                        player=seat_for(BLACK),
                        color=BLACK,
                        x=x,
                        y=y,
                        phase=proto.PHASE_BLACK5_SELECT,
                        selected_by=white_seat,
                        move_index=moves_n,
                    )
                    verdict = classify_black_move(board, x, y, self.size)
                    if verdict is BlackMoveKind.EXACT_FIVE:
                        winner, reason = seat_for(BLACK), "five"
                    elif verdict in _FORBIDDEN_REASONS:
                        winner, reason = seat_for(WHITE), _FORBIDDEN_REASONS[verdict]
                        await self._emit_async(
                            "forbidden",
                            player=seat_for(BLACK),
                            color=BLACK,
                            x=x,
                            y=y,
                            forbidden_kind=verdict.value,
                        )

        # 6. 黑5后轮白方；此后允许 PASS，两方连续 PASS 判和。
        to_color = WHITE
        previous_pass_seat: int | None = None
        while winner is None and reason == "draw" and moves_n >= 5:
            seat = seat_for(to_color)
            action, failure = await self._request_action(
                decide,
                phase=proto.PHASE_NORMAL,
                seat=seat,
                color=to_color,
                board=board,
                seat_colors=seat_colors,
                n=n,
                last=last,
                pass_allowed=True,
            )
            if failure is not None:
                winner, reason = 1 - seat, failure
                break
            if action is None or action.get("action") not in {
                proto.ACTION_MOVE,
                proto.ACTION_PASS,
            }:
                winner, reason = 1 - seat, "illegal"
                await self._emit_illegal(
                    seat=seat,
                    phase=proto.PHASE_NORMAL,
                    action=action,
                    why="expected_move_or_pass",
                )
                break

            if action["action"] == proto.ACTION_PASS:
                await self._emit_async(
                    "pass",
                    player=seat,
                    color=to_color,
                    move_index=moves_n,
                )
                if previous_pass_seat is not None and previous_pass_seat != seat:
                    winner, reason = None, "double_pass"
                    break
                previous_pass_seat = seat
                to_color = WHITE if to_color == BLACK else BLACK
                continue

            previous_pass_seat = None
            x, y = int(action["x"]), int(action["y"])
            if not is_legal_move(board, x, y, self.size):
                winner, reason = 1 - seat, "illegal"
                await self._emit_illegal(
                    seat=seat,
                    phase=proto.PHASE_NORMAL,
                    action=action,
                    why="occupied_or_out_of_board",
                )
                break

            board[x][y] = to_color
            moves_n += 1
            last = {"x": x, "y": y, "color": to_color}
            await self._emit_async(
                "move",
                player=seat,
                color=to_color,
                x=x,
                y=y,
                phase=proto.PHASE_NORMAL,
                move_index=moves_n,
            )

            if to_color == BLACK:
                verdict = classify_black_move(board, x, y, self.size)
                if verdict is BlackMoveKind.EXACT_FIVE:
                    winner, reason = seat, "five"
                    break
                if verdict in _FORBIDDEN_REASONS:
                    winner, reason = seat_for(WHITE), _FORBIDDEN_REASONS[verdict]
                    await self._emit_async(
                        "forbidden",
                        player=seat,
                        color=BLACK,
                        x=x,
                        y=y,
                        forbidden_kind=verdict.value,
                    )
                    break
            elif check_win(board, x, y, WHITE, self.size):
                winner, reason = seat, "five"
                break

            if board_full(board):
                winner, reason = None, "board_full"
                break
            to_color = WHITE if to_color == BLACK else BLACK

        scores = compute_scores(winner)
        deltas = compute_deltas(winner)
        result = RoundResult(
            winners=[winner] if winner is not None else [],
            deltas=deltas,
        )
        board_grid = [list(column) for column in board]
        await self._emit_async(
            "match_end",
            game_id="gomoku",
            ruleset=proto.RULESET_ID,
            protocol_version=proto.PROTOCOL_VERSION,
            winner=winner,
            reason=reason,
            scores=scores,
            moves=moves_n,
            opening_code=opening_code or None,
            n=n,
            seat_colors=list(seat_colors),
            board=board_grid,
        )
        return MatchResult(
            rounds_played=moves_n,
            rounds=[result],
            events=self.events,
            winner=winner,
            reason=reason,
            scores=scores,
            moves=moves_n,
            board_grid=board_grid,
        )

    def run(self, decide: DecideFn) -> MatchResult:
        return asyncio.run(self.run_async(decide))


__all__ = ["BOARD_SIZE", "GomokuSession"]
