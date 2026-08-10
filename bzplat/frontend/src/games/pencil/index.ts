/** 点格棋前端视图规格（canvas 渲染；DOM PencilBoard 已删）。 */
import { Circle } from 'lucide-react'
import type { GameViewSpec, RawEvent } from '@/games/base'
import { PencilCanvasRenderer } from '@/games/pencil/canvas'
import { isPencilPassRequest, PencilHumanActions } from '@/games/pencil/human-actions'
import { pencilTerminalReason } from '@/games/pencil/reasons'
import { reducePencilEvents, type PencilViewModel } from '@/games/pencil/reducer'
import { PencilReplayHud } from '@/games/pencil/replay-hud'

const PencilBoardStub = () => null  // canvas 接管，DOM Board 不再用

function displaySeat(value: unknown): string {
  const seat = Number(value)
  return Number.isFinite(seat) ? String(seat + 1) : '?'
}

function displaySeconds(value: unknown): string {
  const seconds = Number(value)
  if (!Number.isFinite(seconds) || seconds < 0) return '未知'
  return `${Math.round(seconds * 10) / 10} 秒`
}

function describePencilEvent(event: RawEvent): string {
  if (event.type === 'move') {
    return `座${displaySeat(event.player)} · (${String(event.x)},${String(event.y)})${event.scored ? ' · 得分连走' : ''}`
  }
  if (event.type === 'pass') return `座${displaySeat(event.player)} · 让行`
  if (event.type === 'turn') return `轮到座${displaySeat(event.player)}`
  if (event.type === 'your_turn') return '轮到你'
  if (event.type === 'illegal') {
    const detail: Record<string, string> = {
      pass: '错误让行',
      illegal_move: '非法连边',
      crash: 'Bot 运行异常',
      error: 'Bot 响应异常',
    }
    return `座${displaySeat(event.player ?? event.seat)} · ${detail[String(event.why)] || '非法连边'}`
  }
  if (event.type === 'match_start') return '对局开始'
  if (event.type === 'match_end') {
    const outcome = event.winner == null ? '平局' : `座${displaySeat(event.winner)}获胜`
    return `结束 · ${outcome} · ${pencilTerminalReason(event.reason, 'completed').label}`
  }
  if (event.type === 'time_out') return `座${displaySeat(event.seat)} · 超时`
  if (event.type === 'time_used') {
    return `座${displaySeat(event.seat)} · 已用 ${displaySeconds(event.used)} · 剩余 ${displaySeconds(event.remaining)}`
  }
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
  canvasAspectRatio: 1,
  seatColors: ['红', '蓝'],
  progressUnit: 'move',
  matchFormatLabel: '单局',
  winner: (vm) => (vm as PencilViewModel).winner,
  describeEvent: describePencilEvent,
  terminalReason: pencilTerminalReason,
  humanPlay: {
    layout: 'canvas-with-log',
    turnLabel: '轮到你连边',
    turnLabelForRequest: (request) => isPencilPassRequest(request) ? '轮到你确认让行' : '轮到你连边',
    serializeBoardPick: (x, y) => ({ response: { x, y } }),
    canPickBoard: (request) => !isPencilPassRequest(request),
    invalidBoardPickMessage: '请选择一条尚未占用的边；点、格心、已占边和棋盘外区域不会提交。',
    ActionPanel: PencilHumanActions,
  },
  replay: {
    layout: 'with-timeline',
    progress: (vm) => (vm as PencilViewModel).moveCount,
    Hud: PencilReplayHud,
  },
}
