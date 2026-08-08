"""Botzone TexasHoldem2p 官方裁判参考实现（玩家提供的代码，70 手适配）。

本模块是 Botzone 官方参考裁判的忠实移植，包含：
- Card / Suit / HandType 牌型定义与评估
- Holdem 下注状态机（player_action / deal_cards_and_blind / get_player_final_chips）
- compare_full_cards 七牌取最佳五牌比较

计分模型（与 Botzone 完全一致）：
- 每手筹码复位 20000（不跨手累积）
- win_chips = final_chips - mean_chips（零和）
- total_win_chips 累加，比累计净输赢定胜负
- score = total_win_chips / big_blind

引擎适配层（MatchSession）在 engine.py 中包一层，发出平台契约事件。
"""
from __future__ import annotations

from enum import IntEnum
from itertools import combinations
import random

__all__ = [
    "Suit", "HandType", "Card", "hand_type_of_cards",
    "compare_cards_for_hand_type", "find_max_hand_type", "compare_full_cards",
    "Holdem",
]


class Suit(IntEnum):
    HEART = 0    # 红桃
    DIAMOND = 1  # 方块
    SPADE = 2    # 黑桃
    CLUB = 3     # 梅花


class HandType(IntEnum):
    HIGH_CARD = 1
    PAIR = 2
    TWO_PAIR = 3
    THREE_OF_A_KIND = 4
    STRAIGHT = 5
    FLUSH = 6
    FULL_HOUSE = 7
    FOUR_OF_A_KIND = 8
    STRAIGHT_FLUSH = 9


class Card:
    __slots__ = ("suit", "number")

    def __init__(self, suit, number):
        self.suit = suit
        self.number = number

    @staticmethod
    def from_int(i):
        return Card(Suit(i % 4), i // 4 + 2)

    def to_int(self):
        return (self.number - 2) * 4 + self.suit.value

    def __lt__(self, other):
        return self.number < other.number

    def __repr__(self):
        _s = {0: "h", 1: "d", 2: "s", 3: "c"}
        _r = {10: "T", 11: "J", 12: "Q", 13: "K", 14: "A"}
        return f"{_r.get(self.number, str(self.number))}{_s[int(self.suit)]}"

    def __str__(self):
        return repr(self)


def hand_type_of_cards(cards):
    cards = sorted(cards, reverse=True)

    # 同花顺
    if all(c.suit == cards[0].suit for c in cards) and \
            all(cards[i].number == cards[i + 1].number + 1 for i in range(4)):
        return HandType.STRAIGHT_FLUSH
    # 四条
    if (cards[0].number == cards[1].number == cards[2].number == cards[3].number or
            cards[1].number == cards[2].number == cards[3].number == cards[4].number):
        return HandType.FOUR_OF_A_KIND
    # 葫芦
    if (cards[0].number == cards[1].number == cards[2].number and cards[3].number == cards[4].number) or \
            (cards[2].number == cards[3].number == cards[4].number and cards[0].number == cards[1].number):
        return HandType.FULL_HOUSE
    # 同花
    if all(c.suit == cards[0].suit for c in cards):
        return HandType.FLUSH
    # 顺子
    if all(cards[i].number == cards[i + 1].number + 1 for i in range(4)):
        return HandType.STRAIGHT
    # 三条
    if (cards[0].number == cards[1].number == cards[2].number or
            cards[1].number == cards[2].number == cards[3].number or
            cards[2].number == cards[3].number == cards[4].number):
        return HandType.THREE_OF_A_KIND
    # 两对
    if (cards[0].number == cards[1].number and cards[2].number == cards[3].number) or \
            (cards[0].number == cards[1].number and cards[3].number == cards[4].number) or \
            (cards[1].number == cards[2].number and cards[3].number == cards[4].number):
        return HandType.TWO_PAIR
    # 一对
    if (cards[0].number == cards[1].number or cards[1].number == cards[2].number or
            cards[2].number == cards[3].number or cards[3].number == cards[4].number):
        return HandType.PAIR
    return HandType.HIGH_CARD


def compare_cards_for_hand_type(cards1, cards2, hand_type):
    cards1 = sorted(cards1, reverse=True)
    cards2 = sorted(cards2, reverse=True)

    if hand_type == HandType.STRAIGHT_FLUSH:
        return cards1[0].number - cards2[0].number
    if hand_type == HandType.FOUR_OF_A_KIND:
        if cards1[1].number != cards2[1].number:
            return cards1[1].number - cards2[1].number
        high1 = cards1[4 if cards1[0].number == cards1[1].number else 0].number
        high2 = cards2[4 if cards2[0].number == cards2[1].number else 0].number
        return high1 - high2
    if hand_type == HandType.FULL_HOUSE:
        if cards1[2].number != cards2[2].number:
            return cards1[2].number - cards2[2].number
        pair1 = cards1[4 if cards1[0].number == cards1[2].number else 0].number
        pair2 = cards2[4 if cards2[0].number == cards2[2].number else 0].number
        return pair1 - pair2
    if hand_type == HandType.FLUSH:
        for i in range(5):
            if cards1[i].number != cards2[i].number:
                return cards1[i].number - cards2[i].number
        return 0
    if hand_type == HandType.STRAIGHT:
        return cards1[0].number - cards2[0].number
    if hand_type == HandType.THREE_OF_A_KIND:
        if cards1[2].number != cards2[2].number:
            return cards1[2].number - cards2[2].number
        r1 = [c for c in cards1 if c.number != cards1[2].number]
        r2 = [c for c in cards2 if c.number != cards2[2].number]
        for i in range(2):
            if r1[i].number != r2[i].number:
                return r1[i].number - r2[i].number
        return 0
    if hand_type == HandType.TWO_PAIR:
        if cards1[1].number != cards2[1].number:
            return cards1[1].number - cards2[1].number
        if cards1[3].number != cards2[3].number:
            return cards1[3].number - cards2[3].number

        def get_single(cs):
            if cs[0].number == cs[1].number and cs[2].number == cs[3].number:
                return cs[4].number
            elif cs[0].number == cs[1].number and cs[3].number == cs[4].number:
                return cs[2].number
            else:
                return cs[0].number

        return get_single(cards1) - get_single(cards2)
    if hand_type == HandType.PAIR:
        def get_pair(cs):
            if cs[0].number == cs[1].number:
                return cs[0].number, cs[2:]
            elif cs[1].number == cs[2].number:
                return cs[1].number, cs[:1] + cs[3:]
            elif cs[2].number == cs[3].number:
                return cs[2].number, cs[:2] + cs[4:]
            else:
                return cs[3].number, cs[:3]

        pair1, cards1 = get_pair(cards1)
        pair2, cards2 = get_pair(cards2)
        if pair1 != pair2:
            return pair1 - pair2
    # HIGH_CARD + PAIR tiebreak
    for i in range(len(cards1)):
        if cards1[i].number != cards2[i].number:
            return cards1[i].number - cards2[i].number
    return 0


def find_max_hand_type(full_cards):
    if len(full_cards) < 5:
        return HandType.HIGH_CARD, list(full_cards)
    max_hand_type, max_cards = None, None
    for cards in combinations(full_cards, 5):
        cards = list(cards)
        ht = hand_type_of_cards(cards)
        if max_hand_type is None or max_hand_type < ht:
            max_hand_type, max_cards = ht, cards[:]
        elif ht == max_hand_type and compare_cards_for_hand_type(max_cards, cards, ht) < 0:
            max_cards = cards[:]
    return max_hand_type, max_cards


def compare_full_cards(full_cards1, full_cards2):
    ht1, cards1 = find_max_hand_type(full_cards1)
    ht2, cards2 = find_max_hand_type(full_cards2)
    if ht1 != ht2:
        return ht1.value - ht2.value
    return compare_cards_for_hand_type(cards1, cards2, ht1)


class Holdem:
    """Botzone TexasHoldem2p 官方下注状态机（忠实移植）。"""
    PRE_FLOP = 0
    FLOP = 1
    TURN = 2
    RIVER = 3

    CALL = 0    # 跟注/过牌
    FOLD = -1   # 弃牌
    ALLIN = -2  # 全下

    def __init__(self, player_chips, dealer_idx=0, small_blind=50, big_blind=100):
        self.num_players = len(player_chips)
        self.player_chips = list(player_chips)
        self.dealer_idx = dealer_idx
        self.small_blind = small_blind
        self.big_blind = big_blind
        self.player_cards = [[] for _ in range(self.num_players)]
        self.public_cards = []
        self.pot = 0
        self.round = Holdem.PRE_FLOP
        self.round_idx = dealer_idx
        self.round_bet = 0
        self.round_raise = 0
        self.round_action_left = self.num_players + 2
        self.round_player_bet = [0 for _ in range(self.num_players)]
        self.history = []
        self.deck = [Card(suit, number) for number in range(2, 15) for suit in Suit]
        random.shuffle(self.deck)

    def set_deck_array(self, deck_array):
        self.deck = [Card.from_int(card_int) for card_int in deck_array]

    def set_deck_from_str(self, card_strs):
        """用 'Ts','Ah' 字符串列表设置牌序（适配层用）。"""
        self.deck = [_parse_card_str(s) for s in card_strs]

    def _next_player(self):
        self.round_idx = (self.round_idx + 1) % self.num_players
        while self.round_player_bet[self.round_idx] < 0:
            self.round_idx = (self.round_idx + 1) % self.num_players

    def _next_round(self):
        if self.round == Holdem.PRE_FLOP:
            self.round = Holdem.FLOP
            self.public_cards = [self.deck.pop() for _ in range(3)]
        elif self.round == Holdem.FLOP:
            self.round = Holdem.TURN
            self.public_cards.append(self.deck.pop())
        elif self.round == Holdem.TURN:
            self.round = Holdem.RIVER
            self.public_cards.append(self.deck.pop())
        else:
            players_left = [i for i, b in enumerate(self.round_player_bet) if b != Holdem.FOLD]
            players_win = []
            for idx in players_left:
                if not players_win:
                    players_win.append(idx)
                else:
                    cards1 = self.player_cards[idx] + self.public_cards
                    cards2 = self.player_cards[players_win[0]] + self.public_cards
                    cr = compare_full_cards(cards1, cards2)
                    if cr > 0:
                        players_win = [idx]
                    elif cr == 0:
                        players_win.append(idx)
            return players_win

        self.round_idx = self.dealer_idx
        self.round_bet = 0
        self.round_raise = self.big_blind // 2
        self.round_action_left = sum(1 for b in self.round_player_bet if b >= 0)
        self.round_player_bet = [b if b < 0 else 0 for b in self.round_player_bet]
        if self.round_action_left > 0:
            self._next_player()
        else:
            return self._next_round()

    def player_action(self, bet):
        """bet: call/check(0)/raise(>0)/fold(-1)/allin(-2).
        返回获胜玩家 id 列表, 或空列表表示下一玩家, 或 None 表示下一阶段（_next_round 内部递归处理）。"""
        if bet == Holdem.FOLD:
            self.round_player_bet[self.round_idx] = Holdem.FOLD
            players_left = [i for i, b in enumerate(self.round_player_bet) if b != Holdem.FOLD]
            action_type = "fold"
            if len(players_left) == 1:
                self.history.append({
                    "player_id": self.round_idx,
                    "action": Holdem.FOLD,
                    "action_type": action_type,
                })
                return players_left
        elif bet == Holdem.ALLIN:
            self.round_bet = Holdem.ALLIN
            self.pot += self.player_chips[self.round_idx]
            self.player_chips[self.round_idx] = 0
            self.round_player_bet[self.round_idx] = Holdem.ALLIN
            action_type = "allin"
        elif self.round_bet >= 0 and bet == Holdem.CALL:
            inc = self.round_bet - self.round_player_bet[self.round_idx]
            if self.player_chips[self.round_idx] <= inc:
                raise ValueError("INSUFFICIENT_CHIPS")
            self.pot += inc
            self.player_chips[self.round_idx] -= inc
            self.round_player_bet[self.round_idx] = self.round_bet
            action_type = "check" if inc == 0 else "call"
        elif self.round_bet >= 0 and bet + self.round_player_bet[self.round_idx] >= self.round_raise * 2:
            if self.player_chips[self.round_idx] <= bet:
                raise ValueError("INSUFFICIENT_CHIPS")
            self.round_raise = max(self.round_raise, bet)
            self.round_player_bet[self.round_idx] += bet
            self.round_bet = max(self.round_bet, self.round_player_bet[self.round_idx])
            self.pot += bet
            self.player_chips[self.round_idx] -= bet
            action_type = "raise"
        else:
            raise ValueError("INVALID_BET")

        self.round_action_left -= 1
        self.history.append({
            "round": self.round,
            "player_id": self.round_idx,
            "action": bet,
            "action_type": action_type,
        })

        if self.round_action_left <= 0:
            round_bet_left = [b for b in self.round_player_bet if b != Holdem.FOLD]
            if round_bet_left.count(self.round_bet) == len(round_bet_left):
                return self._next_round()
        self._next_player()
        return []

    def deal_cards_and_blind(self):
        """发牌并下盲注。"""
        for pc in self.player_cards:
            pc.append(self.deck.pop())
            pc.append(self.deck.pop())
        self._next_player()
        self.player_action(self.small_blind)  # 小盲注
        self.player_action(self.big_blind)    # 大盲注
        self.history.clear()  # 盲注不算在历史里
        # round_idx 回到 SB（preflop SB 先行动）
        self.round_idx = self.dealer_idx

    def get_player_cards(self, player_idx, with_public=True):
        if with_public:
            return self.player_cards[player_idx] + self.public_cards
        return self.player_cards[player_idx]

    def get_player_final_chips(self, players_win):
        return [
            chips + (self.pot / len(players_win) if i in players_win else 0)
            for i, chips in enumerate(self.player_chips)
        ]


def _parse_card_str(s: str) -> Card:
    """'Ts' → Card(Suit.SPADE, 10)；适配层把 cards.py 的字符串编码转成参考代码的 Card。"""
    _rank = {"2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8, "9": 9,
             "T": 10, "J": 11, "Q": 12, "K": 13, "A": 14}
    _suit = {"h": Suit.HEART, "d": Suit.DIAMOND, "s": Suit.SPADE, "c": Suit.CLUB}
    return Card(_suit[s[1]], _rank[s[0]])
