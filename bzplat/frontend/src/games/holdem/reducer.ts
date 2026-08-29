import { useMemo } from 'react'

import type { RawEvent } from '@/games/base'

/* ── 对局事件 → 可视化状态归约 ────────────────────────────────
 * 后端事件不含「下注中的 pot / 当前行动者 / 本街下注」，前端按事件序列
 * 自行累积重建。action.amount 语义：call=本次投入增量、raise/allin=本街
 * 累计下注总额（raise-to）。fold/check=0。
 */

export interface SeatState {
  chips: number // 当前剩余筹码
  hole: (string | null)[] // 手牌（可能为空）
  bet: number // 本街已下注
  folded: boolean
  allin: boolean
  isWinner: boolean
  lastAction?: { action: string; amount: number } | null
  // 当前计分场累计净盈亏（复式进入第二场时归零）
  net: number
}

export type Street = 'preflop' | 'flop' | 'turn' | 'river' | 'showdown'

export interface HoldemViewModel {
  events: RawEvent[]
  hand: number // 当前手号（0-based）
  totalHands: number
  /** 当前 duplicate leg（0-based）；普通对局固定为 0。 */
  leg: number
  /** Holdem duplicate 固定两场同副牌换座；普通对局为 1。 */
  totalLegs: number
  isDuplicate: boolean
  /** 当前 leg 是否已收到 hand_start；换场的 match_start 单帧为 false。 */
  legStarted: boolean
  /** 当前可见前缀已开始/已结算的全局手数（跨 leg）。 */
  handsStarted: number
  completedHands: number
  /** 当前计分场已开始/已结算手数，复式换场时归零。 */
  currentGameHandsStarted: number
  currentGameCompletedHands: number
  /** 全部计分场按物理 Bot 座位累计的分差，仅作复式次级摘要。 */
  combinedNets: [number, number]
  /** 当前街已处理的动作数，用于 snapshot 后继续严格推导行动权。 */
  streetActions: number
  sbSeat: number // 本手 SB（按钮）座位
  street: Street
  board: string[] // 公共牌
  pot: number // 当前底池
  seats: [SeatState, SeatState]
  toAct: number | null // 当前轮到谁（0/1）
  lastSettle: {
    hand: number
    winners: number[]
    deltas: number[]
    pot: number
    board: string[]
    reason: string
  } | null
  matchOver: boolean
  matchWinner: number | null
  status: 'live' | 'match_end' | 'error'
}

/** 公开 duplicate 事件以 leg=1 表示引擎座位已交换。 */
export function holdemEventLeg(event: RawEvent): number | null {
  if (event.leg === null || event.leg === undefined) return null
  const leg = Number(event.leg)
  return Number.isInteger(leg) && leg >= 0 ? leg : null
}

/** 把引擎座位投影回页面顶部的物理 Bot 座位。 */
export function holdemPhysicalSeatForEvent(value: unknown, event: RawEvent): number {
  const seat = Number(value)
  if (seat !== 0 && seat !== 1) return seat
  return holdemEventLeg(event) === 1 ? 1 - seat : seat
}

/** 把引擎座位顺序的二元数据投影回物理 Bot 顺序。 */
export function holdemPhysicalPairForEvent<T>(values: readonly T[], event: RawEvent): [T, T] {
  return holdemEventLeg(event) === 1
    ? [values[1], values[0]]
    : [values[0], values[1]]
}

/** 最后一个 hand_start 之后的最新动作，绝不跨手沿用旧动作。 */
export function latestHoldemHandAction(events: RawEvent[]): RawEvent | undefined {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    if (events[index]?.type === 'action') return events[index]
    // match_start 是 duplicate 换场边界；不得穿过它沿用上一场动作。
    if (events[index]?.type === 'hand_start' || events[index]?.type === 'match_start') {
      return undefined
    }
  }
  return undefined
}

const EMPTY_SEAT: SeatState = {
  chips: 20000,
  hole: [null, null],
  bet: 0,
  folded: false,
  allin: false,
  isWinner: false,
  lastAction: null,
  net: 0,
}

const STARTING_STACK = 20000

function freshSeats(chips: [number, number] = [20000, 20000]): [SeatState, SeatState] {
  return [
    { ...EMPTY_SEAT, chips: chips[0], hole: [null, null], lastAction: null },
    { ...EMPTY_SEAT, chips: chips[1], hole: [null, null], lastAction: null },
  ]
}

/** 从一串事件归约出当前对局状态 */
export function reduceHoldemEvents(events: RawEvent[]): HoldemViewModel {
  let hand = 0
  let totalHands = 70
  let leg = 0
  let isDuplicate = false
  let legStarted = false
  let handsStarted = 0
  let completedHands = 0
  let currentGameHandsStarted = 0
  let currentGameCompletedHands = 0
  let combinedNets: [number, number] = [0, 0]
  let streetActions = 0
  let sbSeat = 0
  let street: Street = 'preflop'
  let board: string[] = []
  let pot = 0
  let seats = freshSeats()
  let toAct: number | null = null
  let lastSettle: HoldemViewModel['lastSettle'] = null
  let matchOver = false
  let matchWinner: number | null = null
  let status: HoldemViewModel['status'] = 'live'

  for (const ev of events) {
    const eventLeg = holdemEventLeg(ev)
    if (eventLeg !== null) {
      isDuplicate = true
      if (eventLeg !== leg) {
        leg = eventLeg
        legStarted = false
        // Public replay strips every engine-level terminal marker, but older
        // or raw replays may still contain the previous scoring game's
        // match_end.  A new scoring-game boundary is authoritative: never let
        // that terminal state leak into the next game's live HUD/canvas.
        matchOver = false
        matchWinner = null
        status = 'live'
        currentGameHandsStarted = 0
        currentGameCompletedHands = 0
        streetActions = 0
        hand = 0
        sbSeat = 0
        street = 'preflop'
        board = []
        pot = 0
        toAct = null
        lastSettle = null
        // 换场后筹码/盲注由下一个 hand_start 权威覆盖；当前场净值
        // 必须归零，跨场合计只保存在 combinedNets 的次级摘要中。
        seats = freshSeats()
      }
    }
    switch (ev.type) {
      case 'snapshot': {
        // 快照：重置后重放其携带的历史事件
        const hist = (ev.events as RawEvent[] | undefined) ?? []
        if (hist.length) {
          const sub = reduceHoldemEvents(hist)
          hand = sub.hand
          totalHands = sub.totalHands
          leg = sub.leg
          isDuplicate = sub.isDuplicate
          legStarted = sub.legStarted
          handsStarted = sub.handsStarted
          completedHands = sub.completedHands
          currentGameHandsStarted = sub.currentGameHandsStarted
          currentGameCompletedHands = sub.currentGameCompletedHands
          combinedNets = sub.combinedNets
          streetActions = sub.streetActions
          sbSeat = sub.sbSeat
          street = sub.street
          board = sub.board
          pot = sub.pot
          seats = sub.seats
          toAct = sub.toAct
          lastSettle = sub.lastSettle
          matchOver = sub.matchOver
          matchWinner = sub.matchWinner
          status = sub.status
        }
        break
      }
      case 'hand_start': {
        legStarted = true
        handsStarted += 1
        currentGameHandsStarted += 1
        streetActions = 0
        hand = Number(ev.hand ?? hand)
        sbSeat = holdemPhysicalSeatForEvent(ev.sb ?? 0, ev)
        const chips = ev.chips as [number, number] | undefined
        // 每手复位：筹码、bet、folded、allin；net 保持累计
        const prevNet: [number, number] = [seats[0].net, seats[1].net]
        const physicalChips = chips
          ? holdemPhysicalPairForEvent(chips, ev).map(Number) as [number, number]
          : null
        seats = freshSeats(
          physicalChips ?? [20000, 20000],
        )
        seats[0].net = prevNet[0]
        seats[1].net = prevNet[1]
        // hand_start.chips is post-blind. Reconstruct each blind as the
        // difference from the fixed starting stack so pot/current bets are
        // already correct before the first voluntary action.
        seats[0].bet = Math.max(0, STARTING_STACK - seats[0].chips)
        seats[1].bet = Math.max(0, STARTING_STACK - seats[1].chips)
        street = 'preflop'
        board = []
        pot = seats[0].bet + seats[1].bet
        lastSettle = null
        // 翻前 SB 先行动
        toAct = sbSeat
        break
      }
      case 'deal_hole': {
        const holes = ev.holes as string[][] | undefined
        if (holes) {
          const physicalHoles = holdemPhysicalPairForEvent(holes, ev)
          seats[0].hole = [physicalHoles[0]?.[0] ?? null, physicalHoles[0]?.[1] ?? null]
          seats[1].hole = [physicalHoles[1]?.[0] ?? null, physicalHoles[1]?.[1] ?? null]
        }
        break
      }
      case 'deal_board': {
        const b = ev.board as string[] | undefined
        if (b) board = [...b]
        const st = ev.street as Street | undefined
        if (st) street = st
        // 新一条街：重置本街下注
        seats[0].bet = 0
        seats[1].bet = 0
        streetActions = 0
        // 翻后 BB（非 SB）先行动
        // 有人全押后裁判只会自动发完公共牌，新街不再产生决策。
        toAct = seats.some((seat) => seat.allin) ? null : 1 - sbSeat
        break
      }
      case 'action': {
        const player = holdemPhysicalSeatForEvent(ev.player ?? 0, ev)
        const action = String(ev.action ?? '')
        const amount = Number(ev.amount ?? 0)
        const p = seats[player]

        if (action === 'fold') {
          p.folded = true
          p.lastAction = { action, amount: 0 }
        } else if (action === 'check') {
          p.lastAction = { action, amount: 0 }
        } else if (action === 'call') {
          // amount = 本次投入增量
          const pay = Math.min(amount, p.chips)
          p.chips -= pay
          p.bet += pay
          pot += pay
          if (p.chips === 0) p.allin = true
          p.lastAction = { action, amount: pay }
        } else if (action === 'raise') {
          // amount = 本街累计下注总额（raise-to）
          const prevBet = p.bet
          const need = amount - prevBet
          const pay = Math.min(need, p.chips)
          p.chips -= pay
          p.bet = prevBet + pay
          pot += pay
          if (p.chips === 0) p.allin = true
          p.lastAction = { action, amount }
        } else if (action === 'allin') {
          // amount = 本街累计下注总额
          const target = amount
          const prevBet = p.bet
          const need = target > 0 ? target - prevBet : p.chips
          const pay = Math.min(Math.max(need, 0), p.chips)
          if (pay < p.chips) {
            // amount 不可靠时直接全推
            p.bet += p.chips
            pot += p.chips
            p.chips = 0
          } else {
            p.chips -= pay
            p.bet += pay
            pot += pay
          }
          p.allin = true
          p.lastAction = { action, amount: p.bet }
        }
        // Heads-up 一条街的决策权由动作序列推导：首个 check 轮给
        // 对手，第二个 check/面对加注的 call 闭合本街。唯一例外是
        // 翻前 SB 的首个补盲 call，BB 仍保留 check/raise 权。
        if (action === 'fold') {
          toAct = null
        } else if (action === 'raise') {
          toAct = 1 - player
        } else if (action === 'allin') {
          toAct = seats[1 - player].allin ? null : 1 - player
        } else if (action === 'call') {
          const openingBlindCall = street === 'preflop'
            && streetActions === 0
            && player === sbSeat
            && !seats[1 - player].allin
          toAct = openingBlindCall ? 1 - player : null
        } else if (action === 'check') {
          toAct = streetActions === 0 ? 1 - player : null
        } else {
          toAct = null
        }
        streetActions += 1
        break
      }
      case 'settle': {
        completedHands += 1
        currentGameCompletedHands += 1
        const rawWinners = (ev.winners as number[] | undefined) ?? []
        const winners = rawWinners.map((winner) => holdemPhysicalSeatForEvent(winner, ev))
        const deltas = holdemPhysicalPairForEvent(
          (ev.deltas as number[] | undefined) ?? [0, 0],
          ev,
        )
        const chips = ev.chips as [number, number] | undefined
        const reason = String(ev.reason ?? '')
        const b = ev.board as string[] | undefined
        if (b) board = [...b]
        // 同步筹码到权威值，清零本街下注
        if (chips) {
          const physicalChips = holdemPhysicalPairForEvent(chips, ev)
          seats[0].chips = Number(physicalChips[0])
          seats[1].chips = Number(physicalChips[1])
        }
        seats[0].bet = 0
        seats[1].bet = 0
        seats[0].isWinner = winners.includes(0)
        seats[1].isWinner = winners.includes(1)
        seats[0].net += Number(deltas[0] ?? 0)
        seats[1].net += Number(deltas[1] ?? 0)
        combinedNets[0] += Number(deltas[0] ?? 0)
        combinedNets[1] += Number(deltas[1] ?? 0)
        pot = 0
        toAct = null
        lastSettle = {
          hand: Number(ev.hand ?? hand),
          winners,
          deltas: [Number(deltas[0] ?? 0), Number(deltas[1] ?? 0)],
          pot: Number(ev.pot ?? 0),
          board: b ? [...b] : [],
          reason,
        }
        break
      }
      case 'match_end': {
        matchOver = true
        status = 'match_end'
        // 只有正常完成的 canonical delta 才是筹码合计。技术终局的 ±1
        // 是胜负哨兵，不能覆盖事件前缀已经归约出的本场/复式组合计筹码。
        const normalCompletion = String(ev.reason ?? '') === 'completed'
        if (normalCompletion && Array.isArray(ev.deltas) && ev.deltas.length >= 2) {
          const da = Number(ev.deltas[0])
          const db = Number(ev.deltas[1])
          combinedNets = [da, db]
          // 普通对局的当前场就是整场；复式的 canonical deltas 是两场
          // 合计，只能进入次级摘要，不能覆盖第二场已经归零重算的 net。
          if (!isDuplicate) {
            seats[0].net = da
            seats[1].net = db
          }
        }
        matchWinner = ev.winner === null || ev.winner === undefined
          ? null
          : Number(ev.winner)
        break
      }
      case 'error': {
        // 平台/管理员中止也是终态。它可能发生在首手开始前，或在首个
        // 决策尚未结算时；HUD 必须据此显示“未完成任何一手”，而不是
        // 把默认的 hand=0 误读成正在进行第 1 手。
        matchOver = true
        matchWinner = null
        toAct = null
        status = 'error'
        break
      }
      default:
        break
    }
  }

  // 末尾若仍有手进行中，清除上一手的 isWinner 标记
  if (!lastSettle || lastSettle.hand !== hand) {
    seats[0].isWinner = false
    seats[1].isWinner = false
  }

  return {
    events,
    hand,
    totalHands,
    leg,
    totalLegs: isDuplicate ? 2 : 1,
    isDuplicate,
    legStarted,
    handsStarted,
    completedHands,
    currentGameHandsStarted,
    currentGameCompletedHands,
    combinedNets,
    streetActions,
    sbSeat,
    street,
    board,
    pot,
    seats,
    toAct,
    lastSettle,
    matchOver,
    matchWinner,
    status,
  }
}

/** hook：传入事件数组，返回当前视图状态 */
export function useHoldemState(events: RawEvent[]) {
  return useMemo(() => reduceHoldemEvents(events), [events])
}
