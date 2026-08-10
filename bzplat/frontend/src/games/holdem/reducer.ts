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
  // 本手累计净盈亏（来自 settle.deltas 累加）
  net: number
}

export type Street = 'preflop' | 'flop' | 'turn' | 'river' | 'showdown'

export interface HoldemViewModel {
  events: RawEvent[]
  hand: number // 当前手号（0-based）
  totalHands: number
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
  status: string // 'idle'|'connecting'|'live'|'match_end'|'error'
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
  let sbSeat = 0
  let street: Street = 'preflop'
  let board: string[] = []
  let pot = 0
  let seats = freshSeats()
  let toAct: number | null = null
  let lastSettle: HoldemViewModel['lastSettle'] = null
  let matchOver = false
  let matchWinner: number | null = null

  for (const ev of events) {
    switch (ev.type) {
      case 'snapshot': {
        // 快照：重置后重放其携带的历史事件
        const hist = (ev.events as RawEvent[] | undefined) ?? []
        if (hist.length) {
          const sub = reduceHoldemEvents(hist)
          hand = sub.hand
          totalHands = sub.totalHands
          sbSeat = sub.sbSeat
          street = sub.street
          board = sub.board
          pot = sub.pot
          seats = sub.seats
          toAct = sub.toAct
          lastSettle = sub.lastSettle
          matchOver = sub.matchOver
          matchWinner = sub.matchWinner
        }
        break
      }
      case 'hand_start': {
        hand = Number(ev.hand ?? hand)
        sbSeat = Number(ev.sb ?? 0)
        const chips = ev.chips as [number, number] | undefined
        // 每手复位：筹码、bet、folded、allin；net 保持累计
        const prevNet: [number, number] = [seats[0].net, seats[1].net]
        seats = freshSeats(
          chips ? [Number(chips[0]), Number(chips[1])] : [20000, 20000],
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
          seats[0].hole = [holes[0]?.[0] ?? null, holes[0]?.[1] ?? null]
          seats[1].hole = [holes[1]?.[0] ?? null, holes[1]?.[1] ?? null]
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
        // 翻后 BB（非 SB）先行动
        toAct = 1 - sbSeat
        break
      }
      case 'action': {
        const player = Number(ev.player ?? 0)
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
        toAct = 1 - player
        break
      }
      case 'settle': {
        const winners = (ev.winners as number[] | undefined) ?? []
        const deltas = (ev.deltas as number[] | undefined) ?? [0, 0]
        const chips = ev.chips as [number, number] | undefined
        const reason = String(ev.reason ?? '')
        const b = ev.board as string[] | undefined
        if (b) board = [...b]
        // 同步筹码到权威值，清零本街下注
        if (chips) {
          seats[0].chips = Number(chips[0])
          seats[1].chips = Number(chips[1])
        }
        seats[0].bet = 0
        seats[1].bet = 0
        seats[0].isWinner = winners.includes(0)
        seats[1].isWinner = winners.includes(1)
        seats[0].net += Number(deltas[0] ?? 0)
        seats[1].net += Number(deltas[1] ?? 0)
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
        // 公开 replay/live 只有平台权威 winner/reason/deltas 一套终局。
        if (Array.isArray(ev.deltas) && ev.deltas.length >= 2) {
          const da = Number(ev.deltas[0])
          const db = Number(ev.deltas[1])
          seats[0].net = da
          seats[1].net = db
        }
        matchWinner = ev.winner === null || ev.winner === undefined
          ? null
          : Number(ev.winner)
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
    sbSeat,
    street,
    board,
    pot,
    seats,
    toAct,
    lastSettle,
    matchOver,
    matchWinner,
    status: 'live',
  }
}

/** hook：传入事件数组，返回当前视图状态 */
export function useHoldemState(events: RawEvent[]) {
  return useMemo(() => reduceHoldemEvents(events), [events])
}
