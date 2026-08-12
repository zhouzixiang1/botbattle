/** 五子棋前端视图规格（canvas 渲染；DOM GomokuBoard 已删）。 */
import { Grid3x3 } from 'lucide-react'
import type { GameViewSpec, RawEvent } from '../base'
import type { SeatInfo } from '@/games/canvas-types'
import { eventSeatSubject } from '@/games/seat-display'
import { reduceGomokuEvents, type GomokuViewModel } from './reducer'
import { GomokuCanvasRenderer } from './canvas'
import { gomokuTerminalReason } from './reasons'

const GomokuBoardStub = () => null  // canvas 接管，DOM Board 不再用

function displaySeat(value: unknown): string {
  const seat = Number(value)
  return Number.isFinite(seat) ? String(seat + 1) : '?'
}

function describeGomokuEvent(event: RawEvent, seats?: SeatInfo[]): string {
  const actor = (value: unknown) => eventSeatSubject(seats, value, `座位 ${displaySeat(value)}`)
  const position = (value: unknown) => Number(value) === 0
    ? '先手 / 黑'
    : Number(value) === 1 ? '后手 / 白' : '位置未知'
  if (event.type === 'move') return `${actor(event.player)} · 落子 (${String(event.x)},${String(event.y)}) · ${position(event.player)}`
  if (event.type === 'turn') return `轮到 ${actor(event.player)} · ${position(event.player)}`
  if (event.type === 'your_turn') return '轮到你'
  if (event.type === 'illegal') return `${actor(event.player)} · 非法落子 · ${position(event.player)}`
  if (event.type === 'match_start') return '对局开始'
  if (event.type === 'match_end') {
    const outcome = event.winner == null ? '平局' : `${actor(event.winner)}获胜`
    const reason = gomokuTerminalReason(event.reason, 'completed').label
    return `结束 · ${outcome}${reason ? ` · ${reason}` : ''}`
  }
  return event.type || '?'
}

export const gomokuSpec: GameViewSpec = {
  id: 'gomoku',
  label: '五子棋',
  icon: Grid3x3,
  kind: 'board',
  Board: GomokuBoardStub as unknown as GameViewSpec['Board'],
  reduce: reduceGomokuEvents as unknown as GameViewSpec['reduce'],
  CanvasRenderer: GomokuCanvasRenderer,
  seatColors: ['黑', '白'],
  progressUnit: 'move',
  matchFormatLabel: '单局',
  winner: (vm) => (vm as GomokuViewModel).winner,
  describeEvent: describeGomokuEvent,
  terminalReason: gomokuTerminalReason,
  humanPlay: {
    layout: 'canvas-with-log',
    turnLabel: '轮到你落子',
    serializeBoardPick: (x, y) => ({ response: { x, y } }),
  },
  replay: {
    layout: 'with-timeline',
    progress: (vm) => (vm as GomokuViewModel).moveCount,
  },
}
