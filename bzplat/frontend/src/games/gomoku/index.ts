/** 全国机器博弈竞赛五子棋前端视图规格。 */
import { Grid3x3 } from 'lucide-react'

import type { GameViewSpec, RawEvent } from '@/games/base'
import type { SeatInfo } from '@/games/canvas-types'
import { GomokuCanvasRenderer } from '@/games/gomoku/canvas'
import { GomokuHumanTurnSurface } from '@/games/gomoku/human-actions'
import {
  GOMOKU_COMPETITION_RULESET,
  gomokuColorLabel,
  gomokuForbiddenLabel,
  gomokuPhaseLabel,
  gomokuSeatDetail,
  reduceGomokuEvents,
  type GomokuViewModel,
} from '@/games/gomoku/reducer'
import { GomokuReplaySummary } from '@/games/gomoku/replay-summary'
import { GomokuReplayHud } from '@/games/gomoku/replay-hud'
import { gomokuTerminalReason } from '@/games/gomoku/reasons'
import { eventSeatSubject } from '@/games/seat-display'

const GomokuBoardStub = () => null

function displaySeat(value: unknown): string {
  const seat = Number(value)
  return Number.isFinite(seat) ? String(seat + 1) : '?'
}

function actorColor(event: RawEvent): string {
  const explicit = Number(event.color)
  if (explicit === 0 || explicit === 1) return gomokuColorLabel(explicit)
  // 旧回放没有 color/seat_colors；仅在该旧事件形状下回退 seat0=黑、seat1=白。
  const legacySeat = Number(event.player)
  return legacySeat === 0 ? '黑' : legacySeat === 1 ? '白' : '棋色待定'
}

function phaseSuffix(event: RawEvent): string {
  return event.phase ? ` · ${gomokuPhaseLabel(event.phase)}` : ''
}

function pointText(value: unknown): string {
  if (!value || typeof value !== 'object') return '(?, ?)'
  const point = value as Record<string, unknown>
  return `(${String(point.x)}, ${String(point.y)})`
}

function displaySeconds(value: unknown): string {
  const seconds = Number(value)
  if (!Number.isFinite(seconds) || seconds < 0) return '未知'
  return `${Math.round(seconds * 10) / 10} 秒`
}

function describeGomokuEvent(event: RawEvent, seats?: SeatInfo[]): string {
  const actor = (value: unknown) => eventSeatSubject(seats, value, `座位 ${displaySeat(value)}`)
  if (event.type === 'match_start') {
    return event.ruleset === GOMOKU_COMPETITION_RULESET
      ? '对局开始 · 全国竞赛规则 · 15×15'
      : '对局开始 · 旧版自由五子棋'
  }
  if (event.type === 'turn') {
    return `轮到 ${actor(event.player)} · 当前执${actorColor(event)}${phaseSuffix(event)}`
  }
  if (event.type === 'your_turn') {
    const request = event.request && typeof event.request === 'object'
      ? event.request as Record<string, unknown>
      : null
    return `轮到你${request?.phase ? ` · ${gomokuPhaseLabel(request.phase)}` : ''}`
  }
  if (event.type === 'opening') {
    return `${actor(event.player)}提交指定开局 ${String(event.opening_code || '')} · N=${String(event.n)} · 黑1 ${pointText(event.black1)} · 白2 ${pointText(event.white2)} · 黑3 ${pointText(event.black3)}`
  }
  if (event.type === 'swap') {
    const mapping = Array.isArray(event.seat_colors) && event.seat_colors.length === 2
      ? ` · 座位 1 执${gomokuColorLabel(event.seat_colors[0])}，座位 2 执${gomokuColorLabel(event.seat_colors[1])}`
      : ''
    return `${actor(event.player)}${event.swapped ? '选择交换棋色' : '选择不交换棋色'}${mapping}`
  }
  if (event.type === 'move') {
    return `${actor(event.player)} · 执${actorColor(event)}落子 (${String(event.x)}, ${String(event.y)})${phaseSuffix(event)}`
  }
  if (event.type === 'black5_candidates') {
    const points = Array.isArray(event.points) ? event.points.map(pointText).join('、') : ''
    return `${actor(event.player)}提交 ${String(event.n)} 个黑 5 候选${points ? `：${points}` : ''}`
  }
  if (event.type === 'black5_selected') {
    return `${actor(event.player)}保留候选 #${Number(event.index) + 1} ${pointText(event.point)}，作为真实黑 5`
  }
  if (event.type === 'forbidden') {
    return `${actor(event.player)} · ${gomokuForbiddenLabel(event.forbidden_kind)} · (${String(event.x)}, ${String(event.y)})`
  }
  if (event.type === 'pass') {
    return `${actor(event.player)} · 当前执${actorColor(event)} · PASS 让行`
  }
  if (event.type === 'illegal') {
    return `${actor(event.player)} · 非法动作${phaseSuffix(event)}`
  }
  if (event.type === 'time_out') return `${actor(event.seat)} · 累计棋钟耗尽`
  if (event.type === 'time_used') {
    return `${actor(event.seat)} · 已用 ${displaySeconds(event.used)} · 剩余 ${displaySeconds(event.remaining)}`
  }
  if (event.type === 'match_end') {
    const outcome = event.winner == null ? '平局' : `${actor(event.winner)}获胜`
    return `结束 · ${outcome} · ${gomokuTerminalReason(event.reason, 'completed').label}`
  }
  return event.type || '?'
}

function turnLabelForRequest(request: Record<string, unknown> | null): string {
  const phase = String(request?.phase || '')
  if (phase === 'opening_proposal') return '轮到你提交指定开局'
  if (phase === 'swap_choice') return '轮到你决定是否交换棋色'
  if (phase === 'white4') return '轮到你落白 4'
  if (phase === 'black5_candidates') return '轮到你提交黑 5 候选'
  if (phase === 'black5_select') return '轮到你保留黑 5'
  if (phase === 'normal_play') return '轮到你落子或 PASS'
  return '轮到你操作'
}

export const gomokuSpec: GameViewSpec = {
  id: 'gomoku',
  label: '五子棋',
  icon: Grid3x3,
  kind: 'board',
  Board: GomokuBoardStub as unknown as GameViewSpec['Board'],
  reduce: reduceGomokuEvents as unknown as GameViewSpec['reduce'],
  CanvasRenderer: GomokuCanvasRenderer,
  canvasAspectRatio: 1,
  canvasFit: 'viewport',
  seatDetail: gomokuSeatDetail,
  progressUnit: 'move',
  matchFormatLabel: '指定开局单局',
  winner: (vm) => (vm as GomokuViewModel).winner,
  describeEvent: describeGomokuEvent,
  terminalReason: gomokuTerminalReason,
  humanPlay: {
    layout: 'canvas-with-log',
    turnLabel: '轮到你操作',
    turnLabelForRequest,
    invalidBoardPickMessage: '请选择当前阶段允许的空点；已占点、棋盘外和不符合指定开局范围的位置不会提交。',
    TurnSurface: GomokuHumanTurnSurface,
    endSummary: (vm) => {
      const state = vm as GomokuViewModel
      if (state.ruleset !== GOMOKU_COMPETITION_RULESET) return null
      const opening = state.openingCode ? `开局 ${state.openingCode}` : '指定开局'
      return `${opening}${state.n !== null ? ` · ${state.n} 打` : ''} · 座位 1 执${gomokuColorLabel(state.seatColors[0])} / 座位 2 执${gomokuColorLabel(state.seatColors[1])}`
    },
  },
  replay: {
    layout: 'with-timeline',
    progress: (vm) => (vm as GomokuViewModel).moveCount,
    Hud: GomokuReplayHud,
    Summary: GomokuReplaySummary,
    recordDownload: {
      label: '导出棋谱（JSON）',
    },
  },
}
