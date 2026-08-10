import type { RawEvent } from '@/games/base'
import { resolveTerminalReason } from '@/games/reasons'
import { holdemEventLeg, holdemPhysicalSeatForEvent } from './reducer'

function displaySeat(value: unknown): string {
  const seat = Number(value)
  return Number.isFinite(seat) ? String(seat + 1) : '?'
}

export function describeHoldemEvent(event: RawEvent): string {
  const actions: Record<string, string> = {
    fold: '弃牌',
    check: '过牌',
    call: '跟注',
    raise: '加注至',
    allin: '全押至',
  }
  const eventLeg = holdemEventLeg(event)
  const legPrefix = eventLeg === null ? '' : `第 ${eventLeg + 1} 局 · `
  if (event.type === 'action') {
    const action = actions[String(event.action)] ?? String(event.action ?? '?')
    return `${legPrefix}座${displaySeat(holdemPhysicalSeatForEvent(event.player, event))} · ${action}${event.amount ? ` ${String(event.amount)}` : ''}`
  }
  if (event.type === 'settle') {
    const winners = (event.winners as unknown[] | undefined)
      ?.map((winner) => displaySeat(holdemPhysicalSeatForEvent(winner, event))).join('/') || '?'
    return `${legPrefix}赢家 座${winners} · 底池 ${String(event.pot ?? 0)}`
  }
  if (event.type === 'hand_start') return `${legPrefix}第 ${(Number(event.hand) || 0) + 1} 手开始`
  if (event.type === 'deal_board') return `${legPrefix}${String(event.street ?? '')}: ${(event.dealt as string[] | undefined)?.join(' ') ?? ''}`
  if (event.type === 'deal_hole') return `${legPrefix}发底牌`
  if (event.type === 'match_start') return `${legPrefix}对局开始`
  if (event.type === 'match_end') {
    // winner=null 也可能是 duplicate 每局独立计分，不能在无上下文的时序里猜成普通平局。
    const outcome = event.winner == null ? '无单一整场胜者' : `座${displaySeat(event.winner)}获胜`
    return `结束 · ${outcome} · ${resolveTerminalReason(event.reason, 'completed').label}`
  }
  if (event.type === 'turn') return `轮到座${displaySeat(event.player)}`
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

/** 分段导航在 duplicate 中保留局内手号，不把第二局写成“第 71 手”。 */
export function holdemHandLabel(segment: number, events: RawEvent[]): string {
  const starts = events.filter((event) => event.type === 'hand_start')
  const event = starts[segment]
  if (!event) return `第 ${segment + 1} 手`
  const hand = Number(event.hand)
  const eventLeg = holdemEventLeg(event)
  const handNumber = Number.isInteger(hand) && hand >= 0 ? hand + 1 : segment + 1
  return eventLeg === null
    ? `第 ${handNumber} 手`
    : `第 ${eventLeg + 1} 局 · 第 ${handNumber} 手`
}
