"""德州扑克纯裁判程序（游戏规则，0 平台依赖）。

源自玩家提供的 Botzone TexasHoldem2p 官方参考裁判，本模块在忠实保留其评估/下注
状态机骨架的基础上，**修复了参考代码的 5 处会改变对局结果的 bug**（详见各修复点
注释），使裁判规则正确。本模块只管游戏规则：牌型评估、下注状态机、边池结算、
showdown 判胜。不 import protocol/result/engine/orchestrator/runner —— 可独立
审计/复用/单测。

适配层（engine.py MatchSession）调用本模块的 Holdem 状态机驱动一手牌，自己做
协议/事件/decide/跨手计分。

计分模型（与 Botzone 完全一致）：
- 每手筹码复位 starting_stack（不跨手累积）
- 适配层累计各手净输赢（win_chips = final_chips - mean_chips）定胜负
- score = total_win_chips / big_blind

== 修复点（相对原始 Botzone 参考代码）==
1. **wheel 顺子**：原代码严格连续检测，A(14)-5-4-3-2 判高牌；修正为 wheel=5-high
   straight（标准规则）。见 _is_straight / _straight_high。
2. **庄家/盲注反演**：原 deal_cards_and_blind 先 _next_player 使非庄家当 SB、
   庄家当 BB 且庄家翻前先动（与标准 HU 相反）；修正为庄家=SB、庄家翻前先动。
   见 deal_cards_and_blind。
3. **不等额 all-in 结算**：原 get_player_final_chips 把整个 pot 按胜者均分，无边池/
   退未跟注（短筹码赢会多拿）；修正为 HU main_pot=2×min(contrib)，超额退还大筹码方。
   见 get_player_final_chips。
4. **allin 毒化 round_bet**：原 player_action ALLIN 分支设 round_bet=-2，导致后续
   CALL 分支 round_bet>=0 永不命中（只能 ALLIN/FOLD）；修正为 allin 不毒化 round_bet
   （水位保持当前最高下注额），对手可正常 CALL。见 player_action。
5. **split pot 浮点除**：原 pot/len(winners) 浮点除（奇筹码归属错）；修正为整数除
   + 奇筹码给 SB（HU 约定）。见 get_player_final_chips。
6. **(额外) 终端 fold history 缺 round 键**：补 round 键对齐 Botzone 协议。
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
    """一张扑克牌。suit 见 Suit（0♥1♦2♠3♣，恰与 Botzone 线协议编码一致）；
    number 2..14（2=2,…,10=T,11=J,12=Q,13=K,14=A）。

    str(Card) → "Ts"/"Ah"（rank 字符 + suit 字符），与平台原 cards.py 输出一致，
    事件 payload / 前端 reducer 都依赖此格式。
    """
    __slots__ = ("suit", "number")

    def __init__(self, suit, number):
        self.suit = suit
        self.number = number

    @staticmethod
    def from_int(i):
        """Botzone 0-51 整数 → Card。i%4=suit(0♥1♦2♠3♣)，i//4+2=number(2..14)。"""
        return Card(Suit(i % 4), i // 4 + 2)

    def to_int(self):
        """Card → Botzone 0-51 整数（与 from_int 互逆）。"""
        return (self.number - 2) * 4 + int(self.suit)

    def __lt__(self, other):
        return self.number < other.number

    def __eq__(self, other):
        if not isinstance(other, Card):
            return NotImplemented
        return self.number == other.number and int(self.suit) == int(other.suit)

    def __hash__(self):
        return hash((self.number, int(self.suit)))

    def __repr__(self):
        _s = {0: "h", 1: "d", 2: "s", 3: "c"}
        _r = {10: "T", 11: "J", 12: "Q", 13: "K", 14: "A"}
        return f"{_r.get(self.number, str(self.number))}{_s[int(self.suit)]}"

    def __str__(self):
        return repr(self)


def _straight_high(numbers):
    """5 个不同点数 → 顺子的高牌点数；非顺子返回 None。

    wheel A-2-3-4-5（点数 {14,2,3,4,5}）= 5-high straight（A 当 1）。
    修复点 1：原参考代码严格连续检测漏了 wheel。
    """
    uniq = sorted(set(numbers))
    if len(uniq) != 5:
        return None
    # wheel: A(14)-5-4-3-2
    if uniq == [2, 3, 4, 5, 14]:
        return 5
    # 普通顺子
    if uniq[-1] - uniq[0] == 4:
        return uniq[-1]
    return None


def hand_type_of_cards(cards):
    """评估 5 张牌的牌型（HandType）。"""
    cards = sorted(cards, reverse=True)
    numbers = [c.number for c in cards]
    is_flush = all(c.suit == cards[0].suit for c in cards)
    straight_hi = _straight_high(numbers)

    # 同花顺（含 wheel 同花顺）
    if is_flush and straight_hi is not None:
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
    if is_flush:
        return HandType.FLUSH
    # 顺子（含 wheel）
    if straight_hi is not None:
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
    """同牌型下比较两组 5 张牌；>0 cards1 胜，<0 cards2 胜，0 平。"""
    cards1 = sorted(cards1, reverse=True)
    cards2 = sorted(cards2, reverse=True)

    if hand_type == HandType.STRAIGHT_FLUSH:
        # 修复点 1：用 _straight_high 比（wheel=5 正确处理）
        return _straight_high([c.number for c in cards1]) - _straight_high([c.number for c in cards2])
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
        # 修复点 1：用 _straight_high 比（wheel=5 正确处理）
        return _straight_high([c.number for c in cards1]) - _straight_high([c.number for c in cards2])
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
    """7 选 5 最佳牌型。返回 (HandType, 最佳五牌列表)。"""
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
    """比较两组 5-7 张牌（各取最佳五牌）；>0 cards1 胜，<0 cards2 胜，0 平。"""
    ht1, cards1 = find_max_hand_type(full_cards1)
    ht2, cards2 = find_max_hand_type(full_cards2)
    if ht1 != ht2:
        return ht1.value - ht2.value
    return compare_cards_for_hand_type(cards1, cards2, ht1)


class Holdem:
    """德州扑克下注状态机（单手）。适配层逐个调用 player_action 驱动一手牌。

    标准 HU 约定（修复点 2）：庄家(dealer_idx)=SB，庄家翻前先动；非庄家=BB。
    """
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
        self.round_bet = 0       # 当前街道最高下注额（raise-to 水位）
        self.round_raise = 0     # 上一次 raise 的大小（min 下一 raise = 2×round_raise）
        self.round_action_left = self.num_players + 2
        # 每玩家本街道已投入（正数）；-1=FOLD，-2=ALLIN 哨兵
        self.round_player_bet = [0 for _ in range(self.num_players)]
        # 每玩家本手总投入（跨街道，边池结算用）
        self.hand_contrib = [0 for _ in range(self.num_players)]
        self.history = []
        self.deck = [Card(suit, number) for number in range(2, 15) for suit in Suit]
        random.shuffle(self.deck)

    def set_deck_array(self, deck_array):
        """用 0-51 整数列表设置牌序（Card.from_int 解码；LIFO pop）。"""
        self.deck = [Card.from_int(card_int) for card_int in deck_array]

    def set_deck_from_str(self, card_strs):
        """用 'Ts','Ah' 字符串列表设置牌序（适配层用；LIFO pop）。

        字符串路径绕开内部花色编码差异——同物理牌在两种 Card 模型里都是同一字符串。
        """
        self.deck = [_parse_card_str(s) for s in card_strs]

    def _next_player(self):
        self.round_idx = (self.round_idx + 1) % self.num_players
        while self.round_player_bet[self.round_idx] < 0:
            self.round_idx = (self.round_idx + 1) % self.num_players

    def _next_round(self):
        """街道前进 / showdown。返回 None（前进一街）/ winners 列表（showdown 结束）。"""
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
            # showdown：比较所有未弃牌玩家
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
            return self._next_round()  # all-in runout：无人可动，递归到下一街

    def player_action(self, bet):
        """处理一个玩家动作。

        bet: CALL/check(0) / raise(>0, **额外加注量=delta**) / fold(-1) / allin(-2)
        返回:
        - [] 空列表 → 同街道下一玩家继续
        - None → 街道前进（_next_round 内部处理；可能递归 all-in runout）
        - 非空列表 → 本手结束，元素为获胜玩家 id（fold 单胜 / showdown 可能多胜 split）

        非法动作（筹码不足/加注不达 min/无效码）→ 抛 ValueError，适配层 catch 后
        改发 FOLD（裁判不自动 fold）。
        """
        if bet == Holdem.FOLD:
            self.round_player_bet[self.round_idx] = Holdem.FOLD
            players_left = [i for i, b in enumerate(self.round_player_bet) if b != Holdem.FOLD]
            action_type = "fold"
            if len(players_left) == 1:
                # 修复点 6：补 round 键对齐 Botzone 协议（原代码此处缺 round）
                self.history.append({
                    "round": self.round,
                    "player_id": self.round_idx,
                    "action": Holdem.FOLD,
                    "action_type": action_type,
                })
                return players_left
        elif bet == Holdem.ALLIN:
            # 修复点 4：不毒化 round_bet。allin 方把剩余筹码全投入，水位取 max。
            shove = self.player_chips[self.round_idx]
            self.pot += shove
            self.player_chips[self.round_idx] = 0
            self.round_player_bet[self.round_idx] = Holdem.ALLIN
            self.hand_contrib[self.round_idx] += shove
            # allin 方的「等效下注额」= 加注前 street_bet + shove（用于判定是否构成 raise）
            effective_bet = self._effective_street_bet(self.round_idx, shove)
            if effective_bet > self.round_bet:
                # 这是一次 all-in raise：更新水位 + min-raise 基准（round_raise 取总额）
                self.round_raise = effective_bet
                self.round_bet = effective_bet
            action_type = "allin"
        elif self.round_bet >= 0 and bet == Holdem.CALL:
            inc = self.round_bet - self._street_bet_of(self.round_idx)
            if inc < 0:
                inc = 0
            if self.player_chips[self.round_idx] < inc:
                raise ValueError("INSUFFICIENT_CHIPS")
            self.pot += inc
            self.player_chips[self.round_idx] -= inc
            self.round_player_bet[self.round_idx] = self.round_bet \
                if self.round_player_bet[self.round_idx] != Holdem.ALLIN else Holdem.ALLIN
            self.hand_contrib[self.round_idx] += inc
            action_type = "check" if inc == 0 else "call"
        elif self.round_bet >= 0 and bet + self._street_bet_of(self.round_idx) >= self.round_raise * 2:
            # raise（bet 是 delta）。min raise-to = 2 × round_raise（round_raise = 上次
            # raise 后的总额，非 delta——标准规则：min re-raise = 2× 当前 bet）。
            if self.player_chips[self.round_idx] < bet:
                raise ValueError("INSUFFICIENT_CHIPS")
            self.round_player_bet[self.round_idx] += bet
            new_total = self.round_player_bet[self.round_idx]
            # round_raise 取 raise 后的总额（下次 min re-raise = 2 × 此值）
            self.round_raise = new_total
            self.round_bet = max(self.round_bet, new_total)
            self.pot += bet
            self.player_chips[self.round_idx] -= bet
            self.hand_contrib[self.round_idx] += bet
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
            # 本街道无人可动 → 检查是否所有未弃牌玩家都已「下注匹配或全下」→ 进下一街。
            # 轮结束条件：每个未弃牌玩家要么已 allin（不能再动），要么 street_bet 已匹配
            # 当前 round_bet（修复点 4 配套：避免全 allin 但下注额不同时 _next_player 死循环）。
            left = [i for i in range(self.num_players)
                    if self.round_player_bet[i] != Holdem.FOLD]
            if left:
                all_done = all(
                    self.round_player_bet[i] == Holdem.ALLIN
                    or self._street_bet_of(i) >= self.round_bet
                    for i in left
                )
                if all_done:
                    return self._next_round()
        # 仍有玩家可动 → 下一玩家（跳过 fold/allin 哨兵）
        self._next_player()
        return []

    def _street_bet_of(self, idx):
        """玩家 idx 本街道已投入额（ALLIN 哨兵 -2 → 用 hand_contrib 推算等效额）。

        修复点 4 配套：allin 不再毒化 round_bet，但 round_player_bet 仍记 -2 哨兵
        标记该玩家不能再动。比较下注匹配时需把 -2 还原成实际投入额。
        """
        b = self.round_player_bet[idx]
        if b == Holdem.ALLIN:
            # allin 方等效 street_bet = 该街所有投入（从 hand_contrib 无法直接拆街道，
            # 但 _next_round 重置时 allin 方 round_player_bet 保持 -2、其余归 0，
            # 所以 allin 方的「相对投入」需单独追踪——这里用 _allin_street_bet 字典）
            return self._allin_street_bets.get(idx, 0)
        return b

    def _effective_street_bet(self, idx, shove):
        """allin 方投入 shove 后的等效 street_bet（用于判定是否构成 raise / 更新水位）。"""
        prev = self.round_player_bet[idx]
        base = self._allin_street_bets.get(idx, 0) if prev == Holdem.ALLIN else (prev if prev >= 0 else 0)
        total = base + shove
        # 记录 allin 方本街等效投入（供后续 _street_bet_of 读取）
        if not hasattr(self, "_allin_street_bets"):
            self._allin_street_bets = {}
        self._allin_street_bets[idx] = total
        return total

    def deal_cards_and_blind(self):
        """发底牌 + 下盲注（修复点 2：庄家=SB、庄家翻前先动；标准 HU）。

        修复点 2：原代码先 _next_player 使非庄家当 SB（反了）。标准 HU：
        - dealer = SB（翻前先动，翻后后动）
        - 非 dealer = BB
        """
        if not hasattr(self, "_allin_street_bets"):
            self._allin_street_bets = {}
        # 发底牌：每人 2 张（LIFO pop）
        for pc in self.player_cards:
            pc.append(self.deck.pop())
            pc.append(self.deck.pop())
        # 修复点 2：庄家(dealer_idx)当 SB，非庄家当 BB
        # SB（庄家）投入 small_blind；BB（非庄家）投入 big_blind
        self.round_idx = self.dealer_idx
        self._post_blind(self.dealer_idx, self.small_blind)       # 庄家 = SB
        self._post_blind((self.dealer_idx + 1) % self.num_players, self.big_blind)  # 非庄家 = BB
        self.history.clear()  # 盲注不算在历史里
        # round_bet 在 _post_blind 里更新（BB 构成当前水位 = big_blind）。
        # round_raise 设为 big_blind：标准 HU 规则，翻前 min re-raise-to = 2×BB。
        # （参考代码 _post_blind 算 round_raise = BB-SB = 50 → min 2×50=100 = raise-to150，
        #  非标准；现显式设 round_raise=BB → min raise-to = 2×100 = 200。）
        self.round_bet = self.big_blind
        self.round_raise = self.big_blind
        # round_action_left 已被两次 _post_blind 各减 1（4→2）
        # 翻前庄家(SB)先行动——round_idx 已在 _post_blind 后回到庄家
        self.round_idx = self.dealer_idx

    def _post_blind(self, idx, amount):
        """玩家 idx 强制下盲注 amount（通过 raise 路径走，更新水位/action_left/history）。"""
        pay = min(amount, self.player_chips[idx])
        self.pot += pay
        self.player_chips[idx] -= pay
        self.round_player_bet[idx] = pay
        self.hand_contrib[idx] += pay
        if pay > self.round_bet:
            # 盲注构成 raise（BB 的 big_blind 是初始 raise 基准）
            if self.round_bet > 0:
                self.round_raise = max(self.round_raise, pay - self.round_bet)
            else:
                self.round_raise = pay
            self.round_bet = pay
        if self.player_chips[idx] == 0:
            # 盲注即全下（极短筹码）
            self.round_player_bet[idx] = Holdem.ALLIN
            self._allin_street_bets[idx] = pay
        self.round_action_left -= 1

    def get_player_cards(self, player_idx, with_public=True):
        if with_public:
            return self.player_cards[player_idx] + self.public_cards
        return self.player_cards[player_idx]

    def get_player_final_chips(self, players_win):
        """结算最终筹码（修复点 3+5：边池 + 整数 split）。

        修复点 3：原代码 pot 按胜者均分，无边池/退未跟注——短筹码赢会多拿大筹码超额。
                 修正：HU main_pot = 2 × min(contrib)，超额退还投入多的一方。
        修复点 5：原代码 pot/len(winners) 浮点除；修正整数除 + 奇筹码给 SB（庄家）。

        players_win 必须来自 player_action 的返回值（fold 单胜 / showdown 列表）。
        """
        c = list(self.hand_contrib)
        main_pot = 2 * min(c)  # HU 匹配底池
        # 退未跟注超额：投入多的一方拿回差额
        excess = max(0, max(c) - min(c))
        chips = list(self.player_chips)
        # 退超额给投入多的一方（其筹码加回）
        for i in range(self.num_players):
            if c[i] == max(c) and excess > 0:
                chips[i] += excess
                break
        # main_pot 按胜者分（整数除 + 奇筹码给 SB=dealer_idx）
        n = len(players_win)
        if n == 0:
            return chips
        share = main_pot // n
        rem = main_pot - share * n
        for i in players_win:
            chips[i] += share
        if rem:
            # 奇筹码给 SB（庄家）——若庄家是胜者则归他，否则给胜者列表里最小的座位
            # HU 约定：odd chip 归 SB。若 SB 不在 winners，仍给 winners[0]（罕见）
            if self.dealer_idx in players_win:
                chips[self.dealer_idx] += rem
            else:
                chips[players_win[0]] += rem
        return chips


def _parse_card_str(s: str) -> Card:
    """'Ts' → Card(Suit.SPADE, 10)（标准扑克记法：rank 字符 + suit 字符）。"""
    _rank = {"2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8, "9": 9,
             "T": 10, "J": 11, "Q": 12, "K": 13, "A": 14}
    _suit = {"h": Suit.HEART, "d": Suit.DIAMOND, "s": Suit.SPADE, "c": Suit.CLUB}
    return Card(_suit[s[1]], _rank[s[0]])
