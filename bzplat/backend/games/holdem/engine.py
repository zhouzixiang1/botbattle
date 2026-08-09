"""Heads-up No-Limit Texas Hold'em 引擎（适配层——驱动纯裁判 holdem_judge.Holdem）。

本文件是**平台协议适配层**：调 decide → 经 protocol 构造请求/解析响应 → 翻译成
裁判动作 → 驱动纯裁判（holdem_judge.py 的 Holdem 状态机）做规则判定 → emit 平台
契约事件 → 返回 MatchResult。

纯游戏规则（牌型评估/下注状态机/边池结算/showdown）在 holdem_judge.py，0 平台依赖。
本层只管平台特有：事件序列（6 类事件 dict 键不变）、decide 调用、跨手 Botzone 计分
（每手复位筹码，比累计净输赢 net）、BotCrashedError 处理、P4 duplicate（deal_sequence）。

协议：唯一响应信封为 ``{"response": int}``；其中 response 字段的整数采用
TexasHoldem2p 动作编码，raise=「额外加注量」= delta。

Rules summary（对齐 holdem_judge）：
- HU NLHE; default 70 hands; 庄家=SB 交替（hand_index % 2）
- Starting stack 20000; SB=50; BB=100
- min re-raise-to >= 2× previous raise（裁判 round_raise 机制）
- 非法动作 → fold（裁判抛 ValueError，适配层 catch 后改发 FOLD）
- All-in runout: 裁判内部递归 _next_round 自动发完剩余板子，适配层 diff public_cards 补 emit
"""

from __future__ import annotations

import asyncio
import inspect
import random
from enum import Enum
from typing import Any, Callable

from bzplat.backend.games.holdem.holdem_judge import Holdem
from bzplat.backend.games.holdem.result import HandResult, MatchResult
from bzplat.backend.games.holdem import protocol as proto
from bzplat.backend.runtime.binary_runner import (
    BotCrashedError,
    BotTechnicalError,
    PlatformRunnerError,
)

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


def generate_deal_sequence(num_hands: int, seed: int) -> list[list[int]]:
    """P4 duplicate：用 seed 确定性生成 num_hands 手的牌序。

    每手是 52 张牌的洗牌序列，使用唯一的 Botzone 整数编码：
    ``card = (rank - 2) * 4 + suit``，``suit = 0♥1♦2♠3♣``。
    同 seed → 同序列，两 leg（A-vs-B / B-vs-A）用同 deal_sequence 复现同牌局，
    净筹码相加判胜负（消除运气）。
    """
    rng = random.Random(seed)
    out: list[list[int]] = []
    for _ in range(num_hands):
        cards = list(range(52))
        rng.shuffle(cards)
        out.append(cards)
    return out
class MatchSession:
    """Run N hands between two seats via decide(player_idx, request) → response.

    适配层：每手建一个裁判 Holdem 实例驱动，emit 6 类平台事件，累计跨手 net。
    """

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
        # P4 duplicate：预生成的每手牌序（平台编码）；提供时用它发牌（绕开 rng 漂移）。
        self.deal_sequence = deal_sequence

        # Botzone 计分：每手筹码复位 starting_stack（不跨手累积），
        # 最终比 self.net（各手净输赢累加），不是最终累积筹码。
        self.net = [0, 0]  # 累计净输赢（= 各手 deltas 之和），赛事/编排层据此排名
        self.hand_results: list[HandResult] = []  # 兼容旧名；即 rounds
        self.events: list[dict[str, Any]] = []
        self._current_actor = 0  # BotCrashedError 时定位崩溃方

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
            # Botzone 计分：每手筹码复位 starting_stack（不跨手累积，不因归零提前结束）
            try:
                await self._play_hand(h, decide)
            except PlatformRunnerError:
                raise
            except BotTechnicalError:
                # 协议错误/超时由平台统一落技术判负，不能伪装成一手 fold。
                raise
            except BotCrashedError:
                # 对齐权威裁判：bot 崩溃不可恢复 → 判负（本手全筹码输给对手），不中止整场。
                # _call_decide 抛 BotCrashedError 时，_current_actor 是崩溃方。
                crash_loser = self._current_actor
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
            final_chips=list(self.net),  # Botzone 计分：累计净输赢
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
        sb_idx = hand_index % 2
        bb_idx = 1 - sb_idx

        # 建裁判：庄家=SB（dealer_idx=sb_idx），筹码复位
        judge = Holdem(
            player_chips=[self.starting_stack, self.starting_stack],
            dealer_idx=sb_idx,
            small_blind=self.sb,
            big_blind=self.bb,
        )
        # 注入牌序：duplicate 用预生成 deal_sequence（绕开 rng 漂移），
        # 否则用 rng 生成。两条路径都直接交给 Card.from_int，整个引擎只有
        # 0♥1♦2♠3♣ 一套花色整数编码。
        if self.deal_sequence is not None and hand_index < len(self.deal_sequence):
            # 裁判 LIFO pop（deck.pop 取末尾），deal_sequence 按头部先发，故反转。
            judge.set_deck_array(list(reversed(self.deal_sequence[hand_index])))
        else:
            cards = list(range(52))
            self.rng.shuffle(cards)
            judge.set_deck_array(list(reversed(cards)))

        # 发牌 + 下盲注（修复后庄家=SB）
        judge.deal_cards_and_blind()

        # emit hand_start（post-blind chips，对齐 Botzone：fold 时筹码立即变化）
        self._emit("hand_start", {
            "hand": hand_index,
            "sb": sb_idx,
            "bb": bb_idx,
            "chips": list(judge.player_chips),
        })
        # emit deal_hole（底牌字符串，前端/canvas 依赖 "Ts"/"Ah" 格式）
        self._emit("deal_hole", {
            "hand": hand_index,
            "holes": [[str(c) for c in judge.player_cards[i]] for i in range(2)],
        })

        # 主循环：驱动裁判
        winners: list[int] = []
        reason = "showdown"
        prev_pub_len = 0

        for _ in range(200):  # 安全上限（单手最多约几十个动作）
            actor = judge.round_idx
            self._current_actor = actor

            # 构造请求 + 调 decide
            legal = self._legal_from_judge(judge, actor)
            req = self._build_request(judge, actor, legal)
            raw = await self._call_decide(decide, actor, req)

            # 翻译响应 → 裁判 bet（fold=-1/allin=-2/call=0/raise=delta）
            bet, action_name, action_amount = self._translate_response(judge, actor, raw, legal)

            # 驱动裁判；非法（筹码不足/raise 不达 min）→ 改发 FOLD
            try:
                result = judge.player_action(bet)
            except ValueError:
                bet = Holdem.FOLD
                action_name = "fold"
                action_amount = 0
                result = judge.player_action(Holdem.FOLD)

            # emit action
            self._emit("action", {
                "hand": hand_index,
                "player": actor,
                "action": action_name,
                "amount": action_amount,
            })

            # 处理 all-in runout：一次 player_action 可能连发多街板子
            cur_pub_len = len(judge.public_cards)
            if cur_pub_len > prev_pub_len:
                self._emit_runout_deal_boards(judge, hand_index, prev_pub_len, cur_pub_len)
                prev_pub_len = cur_pub_len

            # 判 result
            if isinstance(result, list) and len(result) > 0:
                # 本手结束（fold 单胜 / showdown 多胜 split）
                winners = result
                if any(judge.round_player_bet[i] == Holdem.FOLD for i in range(2)):
                    reason = "fold"
                else:
                    reason = "showdown"
                break
            # result is None（街道前进）或 []（同街道下一人）→ 继续循环
        else:
            # 安全上限耗尽——不应发生；按当前方弃权结算
            winners = [1 - judge.round_idx]
            reason = "fold"

        # 结算
        final_chips = judge.get_player_final_chips(winners)
        before = [self.starting_stack, self.starting_stack]
        deltas = [final_chips[0] - before[0], final_chips[1] - before[1]]
        self.net[0] += deltas[0]
        self.net[1] += deltas[1]

        hand_result = HandResult(
            hand_index=hand_index,
            winners=list(winners),
            deltas=deltas,
            pot=2 * min(judge.hand_contrib),
            board=list(judge.public_cards),
            holes=[list(judge.player_cards[i]) for i in range(2)],
            folded=[judge.round_player_bet[i] == Holdem.FOLD for i in range(2)],
            reason=reason,
        )
        self.hand_results.append(hand_result)

        self._emit("settle", {
            "hand": hand_index,
            "winners": winners,
            "deltas": deltas,
            "chips": final_chips,       # 本手复位后的筹码（手内显示用）
            "net": list(self.net),      # Botzone 计分：累计净输赢（赛事排名用）
            "pot": hand_result.pot,
            "board": [str(c) for c in judge.public_cards],
            "reason": reason,
        })

    def _emit_runout_deal_boards(
        self, judge: Holdem, hand_index: int, prev_len: int, cur_len: int
    ) -> None:
        """all-in runout 时按增量补 emit deal_board 事件（+3=flop, +1=turn/river）。"""
        idx = prev_len
        all_board = judge.public_cards
        while idx < cur_len:
            if idx == 0:
                street, dealt, idx_end = "flop", all_board[0:3], 3
            elif idx == 3:
                street, dealt, idx_end = "turn", all_board[3:4], 4
            elif idx == 4:
                street, dealt, idx_end = "river", all_board[4:5], 5
            else:
                break
            self._emit("deal_board", {
                "hand": hand_index,
                "street": street,
                "board": [str(c) for c in all_board[:idx_end]],
                "dealt": [str(c) for c in dealt],
            })
            idx = idx_end

    # ----------------------------------------------------------- legal / request
    def _legal_from_judge(self, judge: Holdem, actor: int) -> dict[str, Any]:
        """从裁判状态读 actor 的合法集 + raise 边界（供 _translate_response 校验）。"""
        street_bet = judge._street_bet_of(actor)
        to_call = max(0, judge.round_bet - street_bet)
        chips = judge.player_chips[actor]
        can_check = to_call == 0

        actions: list[str] = [Action.FOLD.value]
        if can_check:
            actions.append(Action.CHECK.value)
        if to_call > 0:
            if chips <= to_call:
                actions.append(Action.ALLIN.value)  # 筹码不够 call → 只能 allin/fold
            else:
                actions.append(Action.CALL.value)

        # raise 边界
        min_raise_to = 2 * judge.round_raise if judge.round_raise > 0 else self.bb
        max_raise_to = street_bet + chips  # all-in raise-to
        can_raise = chips > to_call and max_raise_to > judge.round_bet and min_raise_to <= max_raise_to
        if can_raise:
            actions.append(Action.RAISE.value)
            if Action.ALLIN.value not in actions:
                actions.append(Action.ALLIN.value)
        elif chips > to_call and max_raise_to > judge.round_bet:
            # short all-in raise（不足 min 但超过 current_bet）
            actions.append(Action.ALLIN.value)

        # dedupe
        seen: set[str] = set()
        uniq = [a for a in actions if not (a in seen or seen.add(a))]
        return {
            "actions": uniq,
            "to_call": to_call,
            "min_raise_to": min_raise_to,
            "max_raise_to": max_raise_to,
            "chips": chips,
            "street_bet": street_bet,
            "current_bet": judge.round_bet,
        }

    def _build_request(
        self, judge: Holdem, actor: int, legal: dict[str, Any]
    ) -> dict[str, Any]:
        """构造 Botzone 标准 act 请求（11 字段，严格对齐 Botzone wiki）。"""
        total_win_games = [0, 0]
        for hr in self.hand_results:
            if hr.winners:
                for w in hr.winners:
                    if 0 <= w < 2:
                        total_win_games[w] += 1
        return proto.build_act_request(
            hand=len(self.hand_results),
            total_hands=self.num_hands,
            my_id=actor,
            dealer_id=judge.dealer_idx,
            my_cards=judge.player_cards[actor],
            board=judge.public_cards,
            history=list(judge.history),
            my_chips=judge.player_chips[actor],
            total_win_chips=list(self.net),
            total_win_games=total_win_games,
        )

    def _translate_response(
        self, judge: Holdem, actor: int, raw: dict[str, Any], legal: dict[str, Any]
    ) -> tuple[int, str, int]:
        """Bot 响应 → (裁判 bet, action_name, action_amount_for_event)。

        裁判 bet: fold=-1 / allin=-2 / call=0 / raise=delta（额外加注量）
        action_amount_for_event: raise=raise_to_total / allin=new_bet / call=inc / check=0 / fold=0
        """
        try:
            action, x = proto.parse_response(raw)
        except Exception:
            return Holdem.FOLD, "fold", 0

        allowed = set(legal["actions"])
        street_bet = legal["street_bet"]
        chips = legal["chips"]
        to_call = legal["to_call"]

        if action == "fold":
            return Holdem.FOLD, "fold", 0

        if action == "check":
            if "check" not in allowed:
                # to_call>0 时 bot 用 0 表示 call（Botzone 歧义码）→ 降级 call
                if "call" in allowed:
                    return Holdem.CALL, "call", to_call
                if "allin" in allowed and to_call >= chips:
                    return Holdem.ALLIN, "allin", chips
                return Holdem.FOLD, "fold", 0
            return Holdem.CALL, "check", 0  # check = call(0) in judge

        if action == "call":
            if "call" not in allowed:
                if "check" in allowed:
                    return Holdem.CALL, "check", 0
                if "allin" in allowed and to_call >= chips:
                    return Holdem.ALLIN, "allin", chips
                return Holdem.FOLD, "fold", 0
            return Holdem.CALL, "call", to_call

        if action == "allin":
            if "allin" not in allowed and "raise" not in allowed:
                if to_call > 0 and chips > 0:
                    return Holdem.ALLIN, "allin", chips
                return Holdem.FOLD, "fold", 0
            return Holdem.ALLIN, "allin", chips

        if action == "raise":
            if x is None:
                return Holdem.FOLD, "fold", 0
            delta = int(x)
            if delta <= 0:
                return Holdem.FOLD, "fold", 0
            raise_to = street_bet + delta
            max_to = legal["max_raise_to"]
            min_to = legal["min_raise_to"]
            if raise_to >= max_to:
                return Holdem.ALLIN, "allin", chips  # treat as all-in
            if raise_to < min_to:
                return Holdem.FOLD, "fold", 0
            if raise_to <= legal["current_bet"]:
                return Holdem.FOLD, "fold", 0
            need = raise_to - street_bet
            if need <= 0 or need > chips:
                return Holdem.FOLD, "fold", 0
            return delta, "raise", raise_to

        return Holdem.FOLD, "fold", 0


__all__ = [
    "Action",
    "Street",
    "HandResult",
    "MatchResult",
    "MatchSession",
    "STARTING_STACK",
    "SMALL_BLIND",
    "BIG_BLIND",
    "DEFAULT_HANDS",
]
