import type { RawEvent } from '@/games/base'
import type { SeatInfo } from '@/games/canvas-types'
import { resolveTerminalReason } from '@/games/reasons'
import { eventSeatSubject } from '@/games/seat-display'
import { holdemEventLeg, holdemPhysicalSeatForEvent } from './reducer'

function displaySeat(value: unknown): string {
  const seat = Number(value)
  return Number.isFinite(seat) ? String(seat + 1) : '?'
}

export function describeHoldemEvent(event: RawEvent, seats?: SeatInfo[]): string {
  const actions: Record<string, string> = {
    fold: '弃牌',
    check: '过牌',
    call: '跟注',
    raise: '加注至',
    allin: '全押至',
  }
  const streets: Record<string, string> = {
    flop: '翻牌',
    turn: '转牌',
    river: '河牌',
  }
  const eventLeg = holdemEventLeg(event)
  const legPrefix = eventLeg === null ? '' : `第 ${eventLeg + 1} 场 · `
  if (event.type === 'action') {
    const action = actions[String(event.action)] ?? String(event.action ?? '?')
    const physicalSeat = holdemPhysicalSeatForEvent(event.player, event)
    return `${legPrefix}${eventSeatSubject(seats, physicalSeat)} · ${action}${event.amount ? ` ${String(event.amount)}` : ''}（座位 ${displaySeat(physicalSeat)}）`
  }
  if (event.type === 'settle') {
    const winners = (event.winners as unknown[] | undefined)
      ?.map((winner) => {
        const physicalSeat = holdemPhysicalSeatForEvent(winner, event)
        return eventSeatSubject(seats, physicalSeat, `座位 ${displaySeat(physicalSeat)}`)
      }).join(' / ') || '赢家待定'
    return `${legPrefix}${winners} 赢得本手 · 底池 ${String(event.pot ?? 0)}`
  }
  if (event.type === 'hand_start') return `${legPrefix}第 ${(Number(event.hand) || 0) + 1} 手开始`
  if (event.type === 'deal_board') {
    const street = streets[String(event.street)] ?? '公共牌'
    return `${legPrefix}${street}发牌：${(event.dealt as string[] | undefined)?.join(' ') ?? ''}`
  }
  if (event.type === 'deal_hole') return `${legPrefix}发放底牌`
  if (event.type === 'match_start') return eventLeg === null ? '对局开始' : `第 ${eventLeg + 1} 场开始`
  if (event.type === 'match_end') {
    // winner=null 可能是真平，也可能是复式没有单一整场胜者；精确赛果
    // 只由 metadata.outcome 展示，时间线不在缺少上下文时猜测。
    const outcome = event.winner == null
      ? '赛果已结算'
      : `${eventSeatSubject(seats, event.winner, `座位 ${displaySeat(event.winner)}`)} 获胜`
    return `结束 · ${outcome} · ${resolveTerminalReason(event.reason, 'completed').label}`
  }
  if (event.type === 'turn') {
    const physicalSeat = holdemPhysicalSeatForEvent(event.player, event)
    return `${legPrefix}轮到 ${eventSeatSubject(seats, physicalSeat, `座位 ${displaySeat(physicalSeat)}`)}`
  }
  if (event.type === 'your_turn') return '轮到你'
  return event.type || '?'
}

export function holdemHandBoundaries(events: RawEvent[]): number[] {
  const boundaries: number[] = []
  events.forEach((event, index) => {
    if (event.type === 'hand_start') boundaries.push(index)
  })
  if (events.length) boundaries.push(events.length)
  return boundaries
}

/** 分段导航在 duplicate 中保留场内手号，不把第二场写成“第 71 手”。 */
export function holdemHandLabel(segment: number, events: RawEvent[]): string {
  const starts = events.filter((event) => event.type === 'hand_start')
  const event = starts[segment]
  if (!event) return `第 ${segment + 1} 手`
  const hand = Number(event.hand)
  const eventLeg = holdemEventLeg(event)
  const handNumber = Number.isInteger(hand) && hand >= 0 ? hand + 1 : segment + 1
  return eventLeg === null
    ? `第 ${handNumber} 手`
    : `第 ${eventLeg + 1} 场 · 第 ${handNumber} 手`
}
