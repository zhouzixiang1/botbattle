import { useMemo } from 'react'

import type { RawEvent } from '@/games/base'
import {
  parseMatchTimeControl,
  type TimeControlAppliesTo,
  type TimeControlMode,
} from '../../lib/time-controls.ts'

export const GOMOKU_COMPETITION_RULESET = 'gomoku_ccgc_2013_five_move_two_v2'
export const GOMOKU_PREVIOUS_COMPETITION_RULESET = 'gomoku_ccgc_2013_v1'
export const GOMOKU_LEGACY_RULESET = 'gomoku_freestyle_v1'

const GOMOKU_COMPETITION_RULESETS = new Set([
  GOMOKU_COMPETITION_RULESET,
  GOMOKU_PREVIOUS_COMPETITION_RULESET,
])

export const GOMOKU_PHASE_LABELS: Record<string, string> = {
  opening_proposal: '指定开局',
  swap_choice: '三手交换',
  white4: '白 4',
  black5_candidates: '五手候选',
  black5_select: '保留黑 5',
  normal_play: '正常行棋',
  terminal: '对局结束',
}

export type GomokuColor = 0 | 1

export interface GomokuPoint {
  x: number
  y: number
}

export interface GomokuInteractionDraft {
  phase: string
  points: GomokuPoint[]
  n: number | null
  fixedBlack1: GomokuPoint | null
}

export interface GomokuForbiddenState extends GomokuPoint {
  player: number | null
  kind: string
}

export interface GomokuViewModel {
  size: number
  board: number[][] // -1 empty, 0 black, 1 white
  lastMove: (GomokuPoint & { color: GomokuColor }) | null
  toAct: number | null // seat, never color
  toColor: GomokuColor | null
  seatColors: [GomokuColor, GomokuColor] // seat -> color
  matchOver: boolean
  winner: number | null // seat
  reason: string
  moveCount: number
  status: string
  ruleset: string
  protocolVersion: number | null
  phase: string
  openingCode: string
  n: number | null
  swapped: boolean | null
  candidates: GomokuPoint[]
  selectedIndex: number | null
  selectedPoint: GomokuPoint | null
  forbidden: GomokuForbiddenState | null
  consecutivePasses: number
  timeBudget: number | null
  timeRemaining: [number | null, number | null]
  timeOut: number | null
  interaction: GomokuInteractionDraft | null
}

const emptyBoard = (size: number) =>
  Array.from({ length: size }, () => Array(size).fill(-1) as number[])

function finiteInteger(value: unknown): number | null {
  const parsed = Number(value)
  return Number.isInteger(parsed) ? parsed : null
}

function color(value: unknown): GomokuColor | null {
  const parsed = finiteInteger(value)
  return parsed === 0 || parsed === 1 ? parsed : null
}

function point(value: unknown): GomokuPoint | null {
  if (!value || typeof value !== 'object') return null
  const row = value as Record<string, unknown>
  const x = finiteInteger(row.x)
  const y = finiteInteger(row.y)
  return x === null || y === null ? null : { x, y }
}

function points(value: unknown): GomokuPoint[] {
  if (!Array.isArray(value)) return []
  return value.map(point).filter((item): item is GomokuPoint => item !== null)
}

function readBoard(value: unknown, size: number): number[][] | null {
  if (!Array.isArray(value) || value.length !== size) return null
  const next: number[][] = []
  for (const column of value) {
    if (!Array.isArray(column) || column.length !== size) return null
    next.push(column.map((cell) => {
      const parsed = Number(cell)
      return parsed === 0 || parsed === 1 ? parsed : -1
    }))
  }
  return next
}

function readSeatColors(value: unknown): [GomokuColor, GomokuColor] | null {
  if (!Array.isArray(value) || value.length !== 2) return null
  const first = color(value[0])
  const second = color(value[1])
  if (first === null || second === null || first === second) return null
  return [first, second]
}

function place(board: number[][], value: unknown, stone: GomokuColor, size: number) {
  const target = point(value)
  if (!target || target.x < 0 || target.y < 0 || target.x >= size || target.y >= size) return
  board[target.x][target.y] = stone
}

export function gomokuColorLabel(value: unknown): string {
  return color(value) === 0 ? '黑' : color(value) === 1 ? '白' : '待定'
}

export function isGomokuCompetitionRuleset(value: unknown): boolean {
  return GOMOKU_COMPETITION_RULESETS.has(String(value || ''))
}

export function isCurrentGomokuCompetitionRuleset(value: unknown): boolean {
  return String(value || '') === GOMOKU_COMPETITION_RULESET
}

export function gomokuPhaseLabel(value: unknown, candidateCount?: unknown): string {
  const phase = String(value || '')
  if (phase === 'black5_candidates') {
    const count = finiteInteger(candidateCount)
    const numeral = count === 2 ? '二' : count === 3 ? '三' : count === 4 ? '四' : count === 5 ? '五' : null
    return numeral ? `五手${numeral}打` : GOMOKU_PHASE_LABELS[phase]
  }
  return GOMOKU_PHASE_LABELS[phase] || phase || '准备中'
}

export function gomokuForbiddenLabel(value: unknown): string {
  const labels: Record<string, string> = {
    overline: '长连禁手',
    double_four: '四四禁手',
    double_three: '三三禁手',
    forbidden_overline: '长连禁手',
    forbidden_double_four: '四四禁手',
    forbidden_double_three: '三三禁手',
  }
  return labels[String(value || '')] || '黑方禁手'
}

export function gomokuSeatDetail(vm: unknown | null, seat: number): string | undefined {
  if (seat !== 0 && seat !== 1) return undefined
  const state = vm as GomokuViewModel | null
  if (!state) return undefined
  const mapped = state?.seatColors?.[seat] ?? (seat as GomokuColor)
  if (!isGomokuCompetitionRuleset(state.ruleset)) {
    return `${seat === 0 ? '先手' : '后手'} · ${gomokuColorLabel(mapped)}`
  }
  return `${seat === 0 ? '开局提案方' : '交换决策方'} · 当前执${gomokuColorLabel(mapped)}`
}

export function reduceGomokuEvents(events: RawEvent[]): GomokuViewModel {
  let size = 15
  let board = emptyBoard(size)
  let lastMove: GomokuViewModel['lastMove'] = null
  let toAct: number | null = 0
  let toColor: GomokuColor | null = 0
  let seatColors: [GomokuColor, GomokuColor] = [0, 1]
  let matchOver = false
  let winner: number | null = null
  let reason = ''
  let moveCount = 0
  let status = 'live'
  let ruleset = GOMOKU_LEGACY_RULESET
  let protocolVersion: number | null = null
  let phase = 'normal_play'
  let openingCode = ''
  let n: number | null = null
  let swapped: boolean | null = null
  let candidates: GomokuPoint[] = []
  let selectedIndex: number | null = null
  let selectedPoint: GomokuPoint | null = null
  let forbidden: GomokuForbiddenState | null = null
  let consecutivePasses = 0
  let timeBudget: number | null = null
  let timeRemaining: [number | null, number | null] = [null, null]
  let timeOut: number | null = null
  let timeMode: TimeControlMode | null = null
  let timeAppliesTo: TimeControlAppliesTo | null = null
  let botOnlySeat: number | null = null
  let hasProjectedTimeControl = false
  let interaction: GomokuInteractionDraft | null = null

  for (const ev of events) {
    const t = String(ev.type || '')
    if (t === 'snapshot' && Array.isArray(ev.events)) {
      return reduceGomokuEvents(ev.events as RawEvent[])
    }
    if (t === 'match_start') {
      size = Number(ev.size) || 15
      board = emptyBoard(size)
      seatColors = [0, 1]
      toAct = 0
      toColor = 0
      matchOver = false
      winner = null
      reason = ''
      moveCount = 0
      status = 'live'
      ruleset = typeof ev.ruleset === 'string' && ev.ruleset ? ev.ruleset : GOMOKU_LEGACY_RULESET
      protocolVersion = finiteInteger(ev.protocol_version)
      phase = isGomokuCompetitionRuleset(ruleset) ? 'opening_proposal' : 'normal_play'
      openingCode = ''
      n = null
      swapped = null
      candidates = []
      selectedIndex = null
      selectedPoint = null
      forbidden = null
      consecutivePasses = 0
      hasProjectedTimeControl = ev.time_control !== undefined
      const timeControl = parseMatchTimeControl(ev.time_control, 'gomoku')
      const legacyBudget = Number(ev.time_budget_per_side)
      timeBudget = timeControl?.seconds ?? (
        !hasProjectedTimeControl && Number.isFinite(legacyBudget) && legacyBudget > 0
          ? legacyBudget
          : null
      )
      timeMode = timeControl?.mode ?? (timeBudget === null ? null : 'per_side_total')
      timeAppliesTo = timeControl?.applies_to ?? (timeBudget === null ? null : 'both_bots')
      botOnlySeat = null
      timeRemaining = timeAppliesTo === 'bot_only' ? [null, null] : [timeBudget, timeBudget]
      timeOut = null
      interaction = null
    } else if (t === 'turn') {
      const nextSeat = finiteInteger(ev.player)
      if (nextSeat === 0 || nextSeat === 1) toAct = nextSeat
      if (
        timeMode === 'per_decision'
        && timeBudget !== null
        && (nextSeat === 0 || nextSeat === 1)
        && (timeAppliesTo !== 'bot_only' || botOnlySeat === nextSeat)
      ) {
        timeRemaining[nextSeat] = timeBudget
      }
      toColor = color(ev.color) ?? (toAct === 0 || toAct === 1 ? seatColors[toAct] : null)
      if (typeof ev.phase === 'string' && ev.phase) phase = ev.phase
      interaction = null
    } else if (t === 'opening') {
      place(board, ev.black1, 0, size)
      place(board, ev.white2, 1, size)
      place(board, ev.black3, 0, size)
      const black3 = point(ev.black3)
      if (black3) lastMove = { ...black3, color: 0 }
      moveCount = Math.max(moveCount, 3)
      openingCode = String(ev.opening_code || '')
      n = finiteInteger(ev.n)
      phase = 'swap_choice'
      interaction = null
    } else if (t === 'swap') {
      seatColors = readSeatColors(ev.seat_colors) ?? seatColors
      swapped = Boolean(ev.swapped)
      phase = 'white4'
      interaction = null
    } else if (t === 'black5_candidates') {
      candidates = points(ev.points)
      n = finiteInteger(ev.n) ?? n
      selectedIndex = null
      selectedPoint = null
      phase = 'black5_select'
      interaction = null
    } else if (t === 'black5_selected') {
      selectedIndex = finiteInteger(ev.index)
      selectedPoint = point(ev.point)
      phase = 'black5_select'
      interaction = null
    } else if (t === 'move') {
      const x = finiteInteger(ev.x)
      const y = finiteInteger(ev.y)
      const seat = finiteInteger(ev.player)
      const stone = color(ev.color) ?? color(seat)
      if (x !== null && y !== null && stone !== null && x >= 0 && y >= 0 && x < size && y < size) {
        board[x][y] = stone
        lastMove = { x, y, color: stone }
        const authoritativeMove = finiteInteger(ev.move_index)
        moveCount = authoritativeMove ?? moveCount + 1
      }
      consecutivePasses = 0
      const movePhase = String(ev.phase || '')
      if (movePhase === 'white4') phase = 'black5_candidates'
      else if (movePhase === 'black5_select') phase = 'normal_play'
      interaction = null
    } else if (t === 'pass') {
      consecutivePasses += 1
      const seat = finiteInteger(ev.player)
      if (seat === 0 || seat === 1) {
        toAct = 1 - seat
        toColor = seatColors[toAct]
      }
      phase = 'normal_play'
      interaction = null
    } else if (t === 'forbidden') {
      const target = point(ev)
      forbidden = target ? {
        ...target,
        player: finiteInteger(ev.player),
        kind: String(ev.forbidden_kind || ''),
      } : null
    } else if (t === 'time_used') {
      const seat = finiteInteger(ev.seat)
      const remaining = Number(ev.remaining)
      if (hasProjectedTimeControl && timeBudget === null) continue
      if (timeAppliesTo === 'bot_only' && (seat === 0 || seat === 1)) botOnlySeat = seat
      if ((seat === 0 || seat === 1) && Number.isFinite(remaining)) timeRemaining[seat] = remaining
      const budget = Number(ev.budget)
      if (!hasProjectedTimeControl && Number.isFinite(budget) && budget > 0) {
        timeBudget = budget
        timeMode = 'per_side_total'
        timeAppliesTo = 'both_bots'
        if (timeRemaining[0] === null && timeRemaining[1] === null) timeRemaining = [budget, budget]
      }
    } else if (t === 'time_out') {
      const seat = finiteInteger(ev.seat)
      const budget = Number(ev.budget)
      if (hasProjectedTimeControl && timeBudget === null) continue
      if (timeAppliesTo === 'bot_only' && (seat === 0 || seat === 1)) botOnlySeat = seat
      if (!hasProjectedTimeControl && Number.isFinite(budget) && budget > 0) {
        timeBudget = budget
        timeMode = 'per_side_total'
        timeAppliesTo = 'both_bots'
        if (timeRemaining[0] === null && timeRemaining[1] === null) timeRemaining = [budget, budget]
      }
      if (seat === 0 || seat === 1) {
        timeRemaining[seat] = 0
        timeOut = seat
      }
      toAct = null
      toColor = null
    } else if (t === 'match_end') {
      matchOver = true
      winner = ev.winner === null || ev.winner === undefined ? null : Number(ev.winner)
      reason = String(ev.reason || '')
      status = 'match_end'
      phase = 'terminal'
      openingCode = String(ev.opening_code || openingCode)
      n = finiteInteger(ev.n) ?? n
      seatColors = readSeatColors(ev.seat_colors) ?? seatColors
      if (typeof ev.ruleset === 'string' && ev.ruleset) ruleset = ev.ruleset
      protocolVersion = finiteInteger(ev.protocol_version) ?? protocolVersion
      const finalBoard = readBoard(ev.board, size)
      if (finalBoard) board = finalBoard
      interaction = null
      toAct = null
      toColor = null
    } else if (t === 'error') {
      status = 'error'
      matchOver = true
      phase = 'terminal'
      interaction = null
      toAct = null
      toColor = null
    } else if (t === 'human_draft') {
      const request = ev.request && typeof ev.request === 'object'
        ? ev.request as Record<string, unknown>
        : {}
      const requestBoard = readBoard(request.board, size)
      if (requestBoard) board = requestBoard
      seatColors = readSeatColors(request.seat_colors) ?? seatColors
      const requestSeat = finiteInteger(request.me)
      if (requestSeat === 0 || requestSeat === 1) toAct = requestSeat
      toColor = color(request.color) ?? (toAct === 0 || toAct === 1 ? seatColors[toAct] : null)
      phase = typeof request.phase === 'string' && request.phase ? request.phase : String(ev.phase || phase)
      n = finiteInteger(request.n) ?? finiteInteger(ev.n) ?? n
      const requestCandidates = points(request.candidates)
      if (requestCandidates.length) candidates = requestCandidates
      interaction = {
        phase,
        points: points(ev.points),
        n: finiteInteger(ev.n) ?? n,
        fixedBlack1: point(request.fixed_black1),
      }
    }
  }

  return {
    size,
    board,
    lastMove,
    toAct,
    toColor,
    seatColors,
    matchOver,
    winner,
    reason,
    moveCount,
    status,
    ruleset,
    protocolVersion,
    phase,
    openingCode,
    n,
    swapped,
    candidates,
    selectedIndex,
    selectedPoint,
    forbidden,
    consecutivePasses,
    timeBudget,
    timeRemaining,
    timeOut,
    interaction,
  }
}

export function useGomokuState(events: RawEvent[]): GomokuViewModel {
  return useMemo(() => reduceGomokuEvents(events), [events])
}
