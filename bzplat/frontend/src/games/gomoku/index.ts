/** 五子棋前端视图规格（canvas 渲染；DOM GomokuBoard 已删）。 */
import { Grid3x3 } from 'lucide-react'
import type { GameViewSpec, RawEvent } from '../base'
import { reduceGomokuEvents, type GomokuViewModel } from './reducer'
import { GomokuCanvasRenderer } from './canvas'
import { gomokuTerminalReason } from './reasons'

const GomokuBoardStub = () => null  // canvas 接管，DOM Board 不再用

function displaySeat(value: unknown): string {
  const seat = Number(value)
  return Number.isFinite(seat) ? String(seat + 1) : '?'
}

function describeGomokuEvent(event: RawEvent): string {
  if (event.type === 'move') return `座${displaySeat(event.player)} · (${String(event.x)},${String(event.y)})`
  if (event.type === 'turn') return `轮到座${displaySeat(event.player)}`
  if (event.type === 'your_turn') return '轮到你'
  if (event.type === 'illegal') return `座${displaySeat(event.player)} · 非法落子`
  if (event.type === 'match_start') return '对局开始'
  if (event.type === 'match_end') {
    const outcome = event.winner == null ? '平局' : `座${displaySeat(event.winner)}获胜`
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
