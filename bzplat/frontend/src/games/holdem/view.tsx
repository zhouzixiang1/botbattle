import type { GameAuxiliaryProps, RawEvent } from '@/games/base'
import type { HoldemViewModel } from './reducer'
import { resolveTerminalReason } from '@/games/reasons'

function displaySeat(value: unknown): string {
  const seat = Number(value)
  return Number.isFinite(seat) ? String(seat + 1) : '?'
}

function formatNet(value: number): string {
  return `${value >= 0 ? '+' : ''}${value.toLocaleString('en-US')}`
}

export function describeHoldemEvent(event: RawEvent): string {
  const actions: Record<string, string> = {
    fold: '弃牌',
    check: '过牌',
    call: '跟注',
    raise: '加注',
    allin: '全押',
  }
  if (event.type === 'action') {
    const action = actions[String(event.action)] ?? String(event.action ?? '?')
    return `座${displaySeat(event.player)} · ${action}${event.amount ? ` ${String(event.amount)}` : ''}`
  }
  if (event.type === 'settle') {
    const winners = (event.winners as unknown[] | undefined)?.map(displaySeat).join('/') || '?'
    return `赢家 座${winners} · 底池 ${String(event.pot ?? 0)}`
  }
  if (event.type === 'hand_start') return `第 ${(Number(event.hand) || 0) + 1} 手开始`
  if (event.type === 'deal_board') return `${String(event.street ?? '')}: ${(event.dealt as string[] | undefined)?.join(' ') ?? ''}`
  if (event.type === 'deal_hole') return '发底牌'
  if (event.type === 'match_start') return '对局开始'
  if (event.type === 'match_end') {
    const outcome = event.winner == null ? '平局' : `座${displaySeat(event.winner)}获胜`
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

export function HoldemReplaySummary({ vm }: GameAuxiliaryProps) {
  const state = vm as HoldemViewModel
  const netA = state.seats?.[0]?.net
  const netB = state.seats?.[1]?.net
  if (typeof netA !== 'number' || typeof netB !== 'number') return null
  return (
    <span className="font-mono text-xs text-muted-foreground">
      累计筹码{' '}
      <span className={netA >= 0 ? 'text-success' : 'text-destructive'}>座1 {formatNet(netA)}</span>
      {' · '}
      <span className={netB >= 0 ? 'text-success' : 'text-destructive'}>座2 {formatNet(netB)}</span>
    </span>
  )
}
