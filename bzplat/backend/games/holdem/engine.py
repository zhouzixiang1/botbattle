"""Heads-up No-Limit Texas Hold'em engine (national-competition semantics).

协议：Botzone TexasHoldem2p 标准（response 裸整数；raise=「需要额外下注的筹码」=
raise_to_total - 加注方加注前的 street_bet）。引擎内部仍用 raise-to-total 语义
（min_raise_to = 2×current_bet），与 Bot 协议的 delta 互转发生在 protocol.py 边界。

Rules summary:
- HU NLHE; default 70 hands; SB/BB alternate each hand
- Starting stack 20000; SB=50; BB=100
- raise = raise-to-total（内部）；min re-raise-to >= 2× previous raise-to（exact 2× legal）
- Preflop: SB acts first; postflop: BB (non-SB) acts first
- Illegal action → fold
- All-in runout: deal remaining board, then settle
"""

from __future__ import annotations

import asyncio
import inspect
import random
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from bzplat.backend.games.holdem.cards import Card, Deck, compare_hands
from bzplat.backend.games.holdem.result import HandResult, MatchResult, RoundResult
from bzplat.backend.games.holdem import protocol as proto
from bzplat.backend.runtime.binary_runner import BotCrashedError

STARTING_STACK = 20_000
SMALL_BLIND = 50
BIG_BLIND = 100
DEFAULT_HANDS = 70

DecideFn = Callable[[int, dict[str, Any]], Any]  # sync or async → response dict
EventFn = Callable[[str, dict[str, Any]], Any]


class Action(str, Enum):
    FOLD = "fold"
    CHECK = "check"
    CALL = "call"
    RAISE = "raise"
    ALLIN = "allin"


class Street(str, Enum):
    PREFLOP = "preflop"
    FLOP = "flop"
    TURN = "turn"
    RIVER = "river"
    SHOWDOWN = "showdown"


@dataclass
class PlayerState:
    chips: int
    hole: list[Card] = field(default_factory=list)
    # chips committed this street
    street_bet: int = 0
    # chips committed this hand (all streets)
    hand_contrib: int = 0
    folded: bool = False
    all_in: bool = False


class GameEngine:
    """Single-hand / multi-hand HU NLHE. Prefer MatchSession for full matches."""

    def __init__(
        self,
        *,
        starting_stack: int = STARTING_STACK,
        sb: int = SMALL_BLIND,
        bb: int = BIG_BLIND,
        rng: random.Random | None = None,
    ) -> None:
        self.starting_stack = starting_stack
        self.sb = sb
        self.bb = bb
        self.rng = rng or random.Random()


def generate_deal_sequence(num_hands: int, seed: int) -> list[list[int]]:
    """P4 duplicate：用 seed 确定性生成 num_hands 手的牌序。

    每手是 52 张牌的洗牌序列（rank*4+suit 编码）。同 seed → 同序列，两 leg
    （A-vs-B / B-vs-A）用同 deal_sequence 复现同牌局，净筹码相加判胜负（消除运气）。
    """
    rng = random.Random(seed)
    out: list[list[int]] = []
    for _ in range(num_hands):
        cards = list(range(52))
        rng.shuffle(cards)
        out.append(cards)
    return out


class MatchSession:
    """Run N hands between two seats via decide(player_idx, request) → response."""

    def __init__(
        self,
        *,
        num_hands: int = DEFAULT_HANDS,
        starting_stack: int = STARTING_STACK,
        sb: int = SMALL_BLIND,
        bb: int = BIG_BLIND,
        rng: random.Random | None = None,
        on_event: EventFn | None = None,
        deal_sequence: list[list[int]] | None = None,
    ) -> None:
        if num_hands < 1:
            raise ValueError("num_hands must be >= 1")
        self.num_hands = num_hands
        self.starting_stack = starting_stack
        self.sb = sb
        self.bb = bb
        self.rng = rng or random.Random()
        self.on_event = on_event
        # P4 duplicate：预生成的每手牌序（[hand_idx] → list[Card 编码 rank*4+suit]）。
        # 提供时 _play_hand 用它发牌（绕开 rng 漂移，两 leg 同 deal_sequence 复现同牌局）；
        # 不提供时走 Deck(rng).shuffle()（旧行为）。
        self.deal_sequence = deal_sequence

        # Botzone 计分：每手筹码复位为 starting_stack（不跨手累积），
        # 最终比的是 self.net（各手净输赢累加），不是最终累积筹码。
        self.chips = [starting_stack, starting_stack]
        self.net = [0, 0]  # 累计净输赢（= 各手 deltas 之和），赛事/编排层据此排名
        self.hand_results: list[HandResult] = []  # 兼容旧名；即 rounds
        self.events: list[dict[str, Any]] = []

        # per-hand mutable state
        self._hand: int = 0
        self._sb_idx: int = 0
        self._players: list[PlayerState] = []
        self._board: list[Card] = []
        self._deck: Deck | None = None
        self._street: Street = Street.PREFLOP
        self._pot: int = 0
        self._current_bet: int = 0  # raise-to level this street
        self._last_raise_to: int = 0  # last raise-to (for 2× rule); BB counts
        self._min_raise_to: int = 0
        self._to_act: int = 0
        self._pending_actors: set[int] = set()
        # Botzone history 对象数组：[{round, player_id, action, action_type}, ...]
        # （raise 的 action 字段是「额外下注筹码」=raise_to - 该玩家加注前的 street_bet）
        self._hist: list[dict[str, Any]] = []
        self._hand_over: bool = False
        self._aggressor: int | None = None

    # ------------------------------------------------------------------ events
    def _emit(self, kind: str, payload: dict[str, Any] | None = None) -> None:
        ev = {"type": kind, **(payload or {})}
        self.events.append(ev)
        if self.on_event is not None:
            self.on_event(kind, ev)

    async def _call_decide(
        self, decide: DecideFn, player_idx: int, request: dict[str, Any]
    ) -> dict[str, Any]:
        result = decide(player_idx, request)
        if inspect.isawaitable(result):
            result = await result  # type: ignore[assignment]
        if not isinstance(result, dict):
            return {}
        return result

    # ------------------------------------------------------------------ public
    async def run_async(self, decide: DecideFn) -> MatchResult:
        crash_loser: int | None = None
        for h in range(self.num_hands):
            # Botzone 计分：每手筹码复位为 starting_stack（不跨手累积，不因归零提前结束）
            self.chips = [self.starting_stack, self.starting_stack]
            try:
                await self._play_hand(h, decide)
            except BotCrashedError:
                # 对齐权威裁判：bot 崩溃不可恢复 → 判负（本手全筹码输给对手），不中止整场。
                # _call_decide 抛 BotCrashedError 时，_to_act 是崩溃方的对手视角；
                # 但 decide(player_idx) 的 player_idx 才是崩溃方。用 _to_act 推断：
                # _call_decide 在 _betting_round 里被 _to_act 调用，崩溃方 = _to_act。
                crash_loser = self._to_act
                # 本手判负：崩溃方 net 扣全筹码，对手得全筹码（与 botzone 一致）
                self.net[crash_loser] -= self.starting_stack
                self.net[1 - crash_loser] += self.starting_stack
                break
        self._emit(
            "match_end",
            {
                "hands_played": len(self.hand_results),
                "final_chips": list(self.net),  # Botzone 计分：累计净输赢
                "winner": (1 - crash_loser) if crash_loser is not None else None,
                "reason": "crash" if crash_loser is not None else None,
            },
        )
        return MatchResult(
            rounds_played=len(self.hand_results),
            rounds=list(self.hand_results),
            final_chips=list(self.net),  # Botzone 计分：累计净输赢（编排层 sum(deltas) 与之等价）
            events=list(self.events),
        )

    def run(self, decide: DecideFn) -> MatchResult:
        """Sync entrypoint. Prefer ``await run_async`` inside an event loop."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.run_async(decide))
        raise RuntimeError("use await MatchSession.run_async(...) inside async context")

    # -------------------------------------------------------------- hand setup
    async def _play_hand(self, hand_index: int, decide: DecideFn) -> None:
        self._hand = hand_index
        self._sb_idx = hand_index % 2
        self._bb_idx = 1 - self._sb_idx
        self._board = []
        self._pot = 0
        self._street = Street.PREFLOP
        self._hist = []
        self._hand_over = False
        self._aggressor = None

        self._players = [
            PlayerState(chips=self.chips[0]),
            PlayerState(chips=self.chips[1]),
        ]

        self._deck = Deck(self.rng)
        # P4 duplicate：若有预生成牌序，用它发牌（两 leg 同 deal_sequence 复现同牌局）
        if self.deal_sequence is not None and hand_index < len(self.deal_sequence):
            self._deck._cards = [Card(c // 4, c % 4) for c in self.deal_sequence[hand_index]]
        else:
            self._deck.shuffle()

        self._emit(
            "hand_start",
            {
                "hand": hand_index,
                "sb": self._sb_idx,
                "bb": self._bb_idx,
                "chips": [p.chips for p in self._players],
            },
        )

        # deal hole cards
        for i in (0, 1):
            self._players[i].hole = self._deck.deal(2)
        self._emit(
            "deal_hole",
            {
                "hand": hand_index,
                "holes": [[str(c) for c in p.hole] for p in self._players],
            },
        )

        # post blinds
        self._post_blind(self._sb_idx, self.sb)
        self._post_blind(self._bb_idx, self.bb)

        self._current_bet = max(
            self._players[0].street_bet, self._players[1].street_bet
        )
        self._last_raise_to = self._current_bet  # BB level
        self._min_raise_to = 2 * self._last_raise_to
        # Preflop: SB acts first
        self._to_act = self._sb_idx
        self._pending_actors = {0, 1}
        # If someone already all-in from blinds, may skip betting
        if self._both_all_in_or_matched_and_done():
            await self._runout_and_settle(decide)
            return

        await self._betting_round(decide)
        if self._hand_over:
            return

        for street, n_cards in (
            (Street.FLOP, 3),
            (Street.TURN, 1),
            (Street.RIVER, 1),
        ):
            if self._hand_over:
                return
            if self._players[0].all_in or self._players[1].all_in:
                # one or both all-in with matched bets → runout
                await self._runout_and_settle(decide)
                return
            self._deal_board(street, n_cards)
            self._start_postflop_round()
            if self._both_all_in_or_matched_and_done():
                await self._runout_and_settle(decide)
                return
            await self._betting_round(decide)
            if self._hand_over:
                return

        await self._showdown_settle()

    def _post_blind(self, idx: int, amount: int) -> None:
        p = self._players[idx]
        pay = min(amount, p.chips)
        p.chips -= pay
        p.street_bet += pay
        p.hand_contrib += pay
        self._pot += pay
        if p.chips == 0:
            p.all_in = True

    def _deal_board(self, street: Street, n: int) -> None:
        assert self._deck is not None
        cards = self._deck.deal(n)
        self._board.extend(cards)
        self._street = street
        self._emit(
            "deal_board",
            {
                "hand": self._hand,
                "street": street.value,
                "board": [str(c) for c in self._board],
                "dealt": [str(c) for c in cards],
            },
        )

    def _start_postflop_round(self) -> None:
        for p in self._players:
            p.street_bet = 0
        self._current_bet = 0
        self._last_raise_to = 0
        self._min_raise_to = self.bb  # first bet: min raise-to is BB (open to BB)
        # National style for first bet on street: treat like opening to BB,
        # so min raise-to = 2 * BB when opening? Oracle: facing BB 100, min 200.
        # Postflop with current_bet=0, opening bet/raise-to min is BB, but
        # "raise" from 0: we use min_raise_to = bb for a bet, and 2*bb for raise
        # after a bet. Simpler national rule used preflop: min_raise_to = 2 * last.
        # Postflop open: last_raise_to conceptually 0; min open-to = bb.
        self._min_raise_to = self.bb
        # Postflop: BB acts first (non-SB)
        self._to_act = self._bb_idx
        self._pending_actors = {i for i, p in enumerate(self._players) if not p.folded and not p.all_in}
        self._aggressor = None

    def _both_all_in_or_matched_and_done(self) -> bool:
        live = [i for i, p in enumerate(self._players) if not p.folded]
        if len(live) < 2:
            return True
        a, b = self._players[0], self._players[1]
        matched = a.street_bet == b.street_bet
        if matched and (a.all_in or b.all_in):
            return True
        return False

    # ----------------------------------------------------------- betting round
    async def _betting_round(self, decide: DecideFn) -> None:
        # Safety cap
        for _ in range(64):
            if self._hand_over:
                return
            live = [i for i, p in enumerate(self._players) if not p.folded]
            if len(live) == 1:
                self._settle_fold(live[0])
                return

            actors = [
                i
                for i in live
                if not self._players[i].all_in
            ]
            if not actors:
                return  # all-in runout next

            # Round complete: everyone who can act has acted and bets matched
            if not self._pending_actors and self._bets_matched():
                return

            actor = self._to_act
            if self._players[actor].folded or self._players[actor].all_in:
                self._advance_to_act()
                continue

            legal = self.legal_actions(actor)
            req = self.build_request(actor, legal)
            raw = await self._call_decide(decide, actor, req)
            action, amount = self._parse_and_validate(actor, raw, legal)
            self._apply_action(actor, action, amount)
            if self._hand_over:
                return
            if self._bets_matched() and not self._pending_actors:
                return
            self._advance_to_act()
        # Too many actions — treat as stuck; fold current
        if not self._hand_over:
            self._apply_action(self._to_act, Action.FOLD, 0)

    def _bets_matched(self) -> bool:
        live = [p for p in self._players if not p.folded]
        if len(live) < 2:
            return True
        # All-in player may have less than current_bet
        bets = []
        for p in live:
            if p.all_in:
                continue
            bets.append(p.street_bet)
        if not bets:
            return True
        target = self._current_bet
        return all(b == target for b in bets)

    def _advance_to_act(self) -> None:
        other = 1 - self._to_act
        self._to_act = other

    # --------------------------------------------------------- legal / apply
    def legal_actions(self, player_idx: int) -> dict[str, Any]:
        """Return legal action set and raise bounds for player_idx."""
        p = self._players[player_idx]
        opp = self._players[1 - player_idx]
        to_call = max(0, self._current_bet - p.street_bet)
        can_check = to_call == 0
        can_call = to_call > 0 and p.chips > 0
        # call that exhausts stack is still "call" if chips == to_call, else allin
        actions: list[str] = [Action.FOLD.value]
        if can_check:
            actions.append(Action.CHECK.value)
        if can_call:
            if p.chips <= to_call:
                actions.append(Action.ALLIN.value)
            else:
                actions.append(Action.CALL.value)

        min_to = self._compute_min_raise_to(player_idx)
        max_to = p.street_bet + p.chips  # all-in raise-to
        can_raise = False
        if p.chips > to_call and max_to > self._current_bet:
            # Can put in more than a call
            if min_to <= max_to:
                can_raise = True
            elif max_to > self._current_bet:
                # short all-in raise allowed even below min
                can_raise = True
                min_to = max_to  # only all-in

        if can_raise:
            actions.append(Action.RAISE.value)
            actions.append(Action.ALLIN.value)
        elif Action.ALLIN.value not in actions and p.chips > 0 and not can_check:
            # already covered
            pass
        elif p.chips > 0 and to_call == 0 and max_to > self._current_bet:
            # open shove
            actions.append(Action.RAISE.value)
            actions.append(Action.ALLIN.value)

        # Deduplicate preserve order
        seen: set[str] = set()
        uniq: list[str] = []
        for a in actions:
            if a not in seen:
                seen.add(a)
                uniq.append(a)

        return {
            "actions": uniq,
            "to_call": to_call,
            "min_raise_to": min_to,
            "max_raise_to": max_to,
            "chips": p.chips,
            "opp_chips": opp.chips,
            "street_bet": p.street_bet,
            "current_bet": self._current_bet,
        }

    def _compute_min_raise_to(self, player_idx: int) -> int:
        """National rule: min raise-to >= 2 × previous raise-to (exact 2× OK).

        Preflop facing BB: previous raise-to = BB → min = 2*BB.
        After raise-to X: min = 2*X.
        Postflop open (current_bet=0): min open-to = BB.
        After a bet to X: min re-raise-to = 2*X.
        """
        if self._current_bet == 0:
            return self.bb
        # previous raise-to is current_bet level
        return 2 * self._current_bet

    def build_request(
        self, player_idx: int, legal: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if legal is None:
            legal = self.legal_actions(player_idx)
        p = self._players[player_idx]
        # total_win_chips = 各手净输赢累加（Botzone 字段语义）；total_win_games = 各方累计赢手数。
        total_win_games = [0, 0]
        for hr in self.hand_results:
            if hr.winners:
                for w in hr.winners:
                    if 0 <= w < 2:
                        total_win_games[w] += 1
        return proto.build_act_request(
            hand=self._hand,
            total_hands=self.num_hands,
            my_id=player_idx,
            dealer_id=self._sb_idx,
            my_cards=p.hole,
            board=self._board,
            history=list(self._hist),
            my_chips=p.chips,
            total_win_chips=list(self.net),
            total_win_games=total_win_games,
        )

    def _parse_and_validate(
        self,
        player_idx: int,
        raw: dict[str, Any],
        legal: dict[str, Any],
    ) -> tuple[Action, int]:
        try:
            action, x = proto.parse_response(raw)
        except Exception:
            return Action.FOLD, 0

        allowed = set(legal["actions"])
        if action == Action.FOLD:
            return Action.FOLD, 0
        if action == Action.CHECK:
            if Action.CHECK.value not in allowed:
                return Action.FOLD, 0
            return Action.CHECK, 0
        if action == Action.CALL:
            if Action.CALL.value not in allowed:
                # Botzone response 0 既是 call 也是 check（歧义码）。若 call 不合法但
                # check 合法（to_call==0），降级为 check 而非 fold——否则 to_call==0 时
                # 所有标准 Botzone Bot（用 0 表示 check）会被误判 fold。
                if Action.CHECK.value in allowed:
                    return Action.CHECK, 0
                # maybe only allin is allowed for the call amount
                if Action.ALLIN.value in allowed and legal["to_call"] >= self._players[player_idx].chips:
                    return Action.ALLIN, 0
                return Action.FOLD, 0
            return Action.CALL, 0
        if action == Action.ALLIN:
            if Action.ALLIN.value not in allowed and Action.RAISE.value not in allowed:
                # all-in as call
                if legal["to_call"] > 0 and self._players[player_idx].chips > 0:
                    return Action.ALLIN, 0
                return Action.FOLD, 0
            return Action.ALLIN, 0
        if action == Action.RAISE:
            if Action.RAISE.value not in allowed and Action.ALLIN.value not in allowed:
                return Action.FOLD, 0
            p = self._players[player_idx]
            max_to = legal["max_raise_to"]
            min_to = legal["min_raise_to"]
            if x is None:
                return Action.FOLD, 0
            # Botzone 协议：x 是「需要额外下注的筹码」=raise_to - 玩家加注前的 street_bet。
            # 转回引擎的 raise-to-total 语义：raise_to = street_bet + delta。
            delta = int(x)
            if delta <= 0:
                return Action.FOLD, 0
            raise_to = p.street_bet + delta
            if raise_to >= max_to:
                # treat as all-in
                return Action.ALLIN, 0
            if raise_to < min_to:
                return Action.FOLD, 0
            if raise_to <= self._current_bet:
                return Action.FOLD, 0
            # must have chips beyond call
            need = raise_to - p.street_bet
            if need <= 0 or need > p.chips:
                return Action.FOLD, 0
            return Action.RAISE, raise_to
        return Action.FOLD, 0

    def _round_for_history(self) -> int:
        """当前街道 → Botzone history.round（0=preflop 1=flop 2=turn 3=river）。"""
        return proto.STREET_TO_ROUND.get(self._street.value, 0)

    def _record_history(
        self, player_idx: int, action: Action, raise_extra: int | None
    ) -> None:
        """追加一条 Botzone history 对象。

        raise_extra：仅 raise 时为「该玩家加注前 street_bet → 加注后 street_bet 的增量」
        （=Bot 返回的 delta）。fold/check/call/allin 时为 None（action 整数码自含语义）。
        """
        self._hist.append({
            "round": self._round_for_history(),
            "player_id": player_idx,
            "action": proto.action_to_history_int(action.value, raise_extra),
            "action_type": proto.ACTION_TO_ATYPE[action.value],
        })

    def _apply_action(self, player_idx: int, action: Action, raise_to: int) -> None:
        p = self._players[player_idx]
        amount = 0

        if action == Action.FOLD:
            p.folded = True
            self._record_history(player_idx, action, None)
            self._emit(
                "action",
                {"hand": self._hand, "player": player_idx, "action": "fold", "amount": 0},
            )
            winner = 1 - player_idx
            self._settle_fold(winner)
            return

        if action == Action.CHECK:
            amount = 0
            self._pending_actors.discard(player_idx)
            # if no aggressor and both checked, round ends when pending empty
            self._record_history(player_idx, action, None)
        elif action == Action.CALL:
            to_call = self._current_bet - p.street_bet
            pay = min(to_call, p.chips)
            self._put_chips(player_idx, pay)
            amount = pay
            self._pending_actors.discard(player_idx)
            self._record_history(player_idx, action, None)
        elif action == Action.RAISE:
            # raise_extra（Botzone delta）= raise_to - 玩家加注前的 street_bet。
            # 必须在 _put_chips 更新 street_bet 之前算。
            raise_extra = raise_to - p.street_bet
            need = raise_to - p.street_bet
            self._put_chips(player_idx, need)
            amount = raise_to
            self._record_history(player_idx, action, raise_extra)
            self._current_bet = raise_to
            self._last_raise_to = raise_to
            self._min_raise_to = 2 * raise_to
            self._aggressor = player_idx
            # opponent must respond
            opp = 1 - player_idx
            self._pending_actors = {opp} if not self._players[opp].all_in and not self._players[opp].folded else set()
            self._pending_actors.discard(player_idx)
        elif action == Action.ALLIN:
            pay = p.chips
            new_bet = p.street_bet + pay
            self._put_chips(player_idx, pay)
            amount = new_bet
            self._record_history(player_idx, action, None)
            p.all_in = True
            if new_bet > self._current_bet:
                # raise (possibly short)
                prev = self._current_bet
                self._current_bet = new_bet
                # short all-in below min does not reopen full min for opp in
                # some rule sets; national: if raise_to >= min, update min.
                if prev == 0 or new_bet >= self._compute_min_raise_to_from(prev):
                    self._last_raise_to = new_bet
                    self._min_raise_to = 2 * new_bet
                self._aggressor = player_idx
                opp = 1 - player_idx
                if not self._players[opp].all_in and not self._players[opp].folded:
                    # opp still needs to call if behind
                    if self._players[opp].street_bet < self._current_bet:
                        self._pending_actors = {opp}
                    else:
                        self._pending_actors = set()
                else:
                    self._pending_actors = set()
            else:
                # all-in call (or short call)
                self._pending_actors.discard(player_idx)
            self._pending_actors.discard(player_idx)
        else:
            p.folded = True
            self._settle_fold(1 - player_idx)
            return

        self._emit(
            "action",
            {
                "hand": self._hand,
                "player": player_idx,
                "action": action.value,
                "amount": amount,
            },
        )

    def _compute_min_raise_to_from(self, current_bet: int) -> int:
        if current_bet == 0:
            return self.bb
        return 2 * current_bet

    def _put_chips(self, player_idx: int, amount: int) -> None:
        p = self._players[player_idx]
        if amount < 0:
            raise ValueError("negative put")
        if amount > p.chips:
            amount = p.chips
        p.chips -= amount
        p.street_bet += amount
        p.hand_contrib += amount
        self._pot += amount
        if p.chips == 0:
            p.all_in = True

    # -------------------------------------------------------------- settlement
    async def _runout_and_settle(self, decide: DecideFn) -> None:
        del decide  # decisions not needed during runout
        assert self._deck is not None
        while len(self._board) < 5:
            n = 3 if len(self._board) == 0 else 1
            street = {
                0: Street.FLOP,
                3: Street.TURN,
                4: Street.RIVER,
            }[len(self._board)]
            self._deal_board(street, n)
        await self._showdown_settle()

    def _settle_fold(self, winner: int) -> None:
        loser = 1 - winner
        # Return uncalled bets if any? HU fold: winner gets pot.
        # If aggressor bet and opp folds, uncalled portion returns — for
        # simplicity matching equal contributions: pot already has both.
        # If A raised and B folds without calling, A's excess over B's contrib
        # should return. Handle:
        self._return_uncalled()
        pot = self._pot
        deltas = [0, 0]
        deltas[winner] = pot
        # apply to match chips from hand contrib already deducted into pot
        # rebuild chips: starting hand chips - contrib + winnings
        self._finalize(deltas, winners=[winner], reason="fold")

    async def _showdown_settle(self) -> None:
        self._return_uncalled()
        self._street = Street.SHOWDOWN
        a, b = self._players[0], self._players[1]
        # If one folded somehow
        if a.folded and not b.folded:
            self._finalize([0, self._pot], winners=[1], reason="fold")
            return
        if b.folded and not a.folded:
            self._finalize([self._pot, 0], winners=[0], reason="fold")
            return

        # Side pot for HU: main pot = 2 * min(contrib)
        c0, c1 = a.hand_contrib, b.hand_contrib
        matched = min(c0, c1)
        main_pot = matched * 2
        # Excess already returned by _return_uncalled; pot should equal main_pot
        self._pot = main_pot

        cmp = compare_hands(a.hole + self._board, b.hole + self._board)
        if cmp > 0:
            winners = [0]
            deltas = [main_pot, 0]
        elif cmp < 0:
            winners = [1]
            deltas = [0, main_pot]
        else:
            # split — odd chip to SB (common convention) or split evenly
            half = main_pot // 2
            rem = main_pot - 2 * half
            deltas = [half, half]
            if rem:
                deltas[self._sb_idx] += rem
            winners = [0, 1]
        self._finalize(deltas, winners=winners, reason="showdown")

    def _return_uncalled(self) -> None:
        a, b = self._players[0], self._players[1]
        if a.folded or b.folded:
            # uncalled: winner's street excess over loser's total contrib
            if a.folded and not b.folded:
                excess = max(0, b.hand_contrib - a.hand_contrib)
                if excess:
                    b.chips += excess
                    b.hand_contrib -= excess
                    self._pot -= excess
            elif b.folded and not a.folded:
                excess = max(0, a.hand_contrib - b.hand_contrib)
                if excess:
                    a.chips += excess
                    a.hand_contrib -= excess
                    self._pot -= excess
            return
        # both live: return excess over matched
        if a.hand_contrib > b.hand_contrib:
            excess = a.hand_contrib - b.hand_contrib
            a.chips += excess
            a.hand_contrib -= excess
            self._pot -= excess
        elif b.hand_contrib > a.hand_contrib:
            excess = b.hand_contrib - a.hand_contrib
            b.chips += excess
            b.hand_contrib -= excess
            self._pot -= excess

    def _finalize(
        self, pot_awards: list[int], winners: list[int], reason: str
    ) -> None:
        """pot_awards: chips taken from pot by each player. Update match chips."""
        # Current player.chips already have remaining stack (contrib removed).
        # Award pot shares.
        for i, award in enumerate(pot_awards):
            self._players[i].chips += award

        before = list(self.chips)
        after = [self._players[0].chips, self._players[1].chips]
        deltas = [after[0] - before[0], after[1] - before[1]]
        self.chips = after
        # Botzone 计分：本手净输赢累加进整场 net（每手复位筹码，比累计净输赢）
        self.net[0] += deltas[0]
        self.net[1] += deltas[1]

        result = HandResult(
            hand_index=self._hand,
            winners=list(winners),
            deltas=deltas,
            pot=sum(pot_awards),
            board=list(self._board),
            holes=[list(p.hole) for p in self._players],
            folded=[p.folded for p in self._players],
            reason=reason,
        )
        self.hand_results.append(result)
        self._hand_over = True
        self._emit(
            "settle",
            {
                "hand": self._hand,
                "winners": winners,
                "deltas": deltas,
                "chips": list(self.chips),       # 本手复位后的筹码（手内显示用）
                "net": list(self.net),           # Botzone 计分：累计净输赢（赛事排名用）
                "pot": result.pot,
                "board": [str(c) for c in self._board],
                "reason": reason,
            },
        )


# Re-export constants for callers
__all__ = [
    "Action",
    "Street",
    "PlayerState",
    "HandResult",
    "MatchResult",
    "GameEngine",
    "MatchSession",
    "STARTING_STACK",
    "SMALL_BLIND",
    "BIG_BLIND",
    "DEFAULT_HANDS",
]
