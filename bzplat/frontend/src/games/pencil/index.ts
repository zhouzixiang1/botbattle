/** 点格棋前端视图规格（canvas 渲染；DOM PencilBoard 已删）。 */
import { Circle } from 'lucide-react'
import type { GameViewSpec, RawEvent } from '../base'
import { reducePencilEvents, type PencilViewModel } from './reducer'
import { PencilCanvasRenderer } from './canvas'
import { PencilReplayHud } from './replay-hud'

const PencilBoardStub = () => null  // canvas 接管，DOM Board 不再用

function displaySeat(value: unknown): string {
  const seat = Number(value)
  return Number.isFinite(seat) ? String(seat + 1) : '?'
}

function describePencilEvent(event: RawEvent): string {
  if (event.type === 'move') {
    return `座${displaySeat(event.player)} · (${String(event.x)},${String(event.y)})${event.scored ? ' · 得分连走' : ''}`
  }
  if (event.type === 'pass') return `座${displaySeat(event.player)} · 让行`
  if (event.type === 'turn') return `轮到座${displaySeat(event.player)}`
  if (event.type === 'your_turn') return '轮到你'
  if (event.type === 'match_start') return '对局开始'
  if (event.type === 'match_end') return `结束 · 胜者 ${event.winner == null ? '平' : `座${displaySeat(event.winner)}`}`
  if (event.type === 'time_out') return `座${displaySeat(event.seat)} · 超时`
  if (event.type === 'error') return String(event.message || '对局异常')
  return event.type || '?'
}

export const pencilSpec: GameViewSpec = {
  id: 'pencil',
  label: '点格棋',
  icon: Circle,
  kind: 'board',
  Board: PencilBoardStub as unknown as GameViewSpec['Board'],
  reduce: reducePencilEvents as unknown as GameViewSpec['reduce'],
  CanvasRenderer: PencilCanvasRenderer,
  seatColors: ['红', '蓝'],
  progressUnit: 'move',
  matchFormatLabel: '单局',
  winner: (vm) => (vm as PencilViewModel).winner,
  describeEvent: describePencilEvent,
  humanPlay: {
    layout: 'canvas-with-log',
    turnLabel: '轮到你连边',
    serializeBoardPick: (x, y) => ({ response: { x, y } }),
  },
  replay: {
    layout: 'with-timeline',
    progress: (vm) => (vm as PencilViewModel).moveCount,
    Hud: PencilReplayHud,
  },
}
