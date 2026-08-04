import json
import random

N_PLAYERS = 2
SMALL_BLIND = 50
BIG_BLIND = 100

def card_suit(card): return card % 4
def card_number(card): return card // 4 + 2
def next_player(player, offset): return (player + offset) % N_PLAYERS
def get_action(input):
    N_PLAYERS = input["num_players"]  # 比赛中一共有多少玩家, 固定为2
    my_id = input["my_id"]  # 我的玩家ID, 0或1

    # ==============================================================================
    # 恢复当前牌面信息
    round = 0  # 当前轮次 0: preflop, 1: flop, 2: turn, 3: river
    round_bet = BIG_BLIND  # 当前轮次的最大下注
    round_raise = 2 * round_bet  # 当前轮次最小加注到的筹码数
    round_action_count = 0  # 当前轮次已经叫注的玩家数
    player_bets = [0] * N_PLAYERS  # 当前轮次每个玩家已经下注的筹码数
    player_bets[next_player(input["dealer_id"], 1)] = SMALL_BLIND
    player_bets[next_player(input["dealer_id"], 2)] = BIG_BLIND

    for h in input["history"]:
        id, action, type = h["player_id"], h["action"], h["action_type"]
        if type == "fold":
            player_bets[id] = -1  # 标记已弃牌
        elif type == "allin":
            round_bet = -2  # 标记本轮只能全押或弃牌
            player_bets[id] = -2  # 标记已全押
        elif type == "call" or type == "check":
            player_bets[id] = round_bet  # 跟注
            round_action_count += 1
            round_bet_left = [bet for bet in player_bets if bet >= 0]
            # 在场玩家均等注时进入下一轮
            if round_action_count >= len(round_bet_left) and \
                round_bet_left.count(round_bet) == len(round_bet_left):
                round += 1  # 轮次+1，进入下一轮
                round_bet = 0  # 重置当前轮次的最大下注
                round_raise = BIG_BLIND  # 每轮开始最小加注的筹码数均为大盲注
                round_action_count = 0  # 重置当前轮次已经叫注的玩家数
                player_bets = [0 if bet >= 0 else bet for bet in player_bets]
        elif type == "raise":
            player_bets[id] += action  # 加注
            round_bet = max(round_bet, player_bets[id])  # 更新当前轮次的最大下注
            round_raise = max(round_raise, 2 * action)  # 下次加注至少为当前加注的2倍
            round_action_count += 1

    # ==============================================================================
    # 生成可能的动作: (动作, 概率)
    # 动作包括 -1: 弃牌, -2: 全押, 0: 过牌或跟注, >0: 加注的数量
    possible_actions = []
    possible_actions.append((-1, 0.15))  # 弃牌, 概率0.15
    possible_actions.append((-2, 0.05))  # 全押, 概率0.05

    if round_bet >= 0:
        if round_bet - player_bets[my_id] < input["my_chips"]: # 剩余筹码可跟注
            possible_actions.append((0, 0.6))  # 过牌或跟注, 概率0.6
        if round_raise < input["my_chips"]:  # 剩余筹码足够加注
            max_raise_amount = min(round_raise * 4, input["my_chips"] - 1)
            raise_amount = random.randint(round_raise, max_raise_amount)
            possible_actions.append((raise_amount, 0.2))  # 加注, 概率0.2
    else:  # 有人全押, 本轮只能全押或弃牌
        # 如果对面最后一手牌翻牌前allin，且我目前丢掉盲注不能稳赢，就直接全押
        if round == 0 and input["hand"] == input["max_hand"] - 1 and \
            input["total_win_chips"][my_id] - player_bets[my_id] < 0:
            possible_actions.append((-2, 0.8))  # 直接全押, 概率0.8
        else:
            possible_actions.append((-1, 0.8))  # 弃牌, 概率0.8

    actions = [p[0] for p in possible_actions]
    weights = [p[1] for p in possible_actions]
    return random.choices(actions, weights)[0]

requests = json.loads(input())["requests"]
action = get_action(requests[-1])
print(json.dumps({"response": action}))
