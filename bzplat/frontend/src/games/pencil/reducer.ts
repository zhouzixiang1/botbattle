import { useMemo } from 'react'

import type { RawEvent } from '@/games/base'
import {
  parseMatchTimeControl,
  type TimeControlAppliesTo,
  type TimeControlMode,
} from '../../lib/time-controls.ts'

export interface PencilViewModel {
  nDots: number
  size: number
  /** 交错网格：3点 4边 5已占边 2格 */
  grid: number[][]
  scores: [number, number]
  lastEdge: { x: number; y: number } | null
  toAct: number | null
  matchOver: boolean
  winner: number | null
  reason: string
  moveCount: number
  status: string
  /** 已占边归属："x,y" → player(0红/1蓝)。前端按玩家着色用。 */
  edgeOwner: Record<string, number>
  /** 格心归属网格：-1 未占 / 0 红 / 1 蓝 / -2 非格心。前端按归属着色用。 */
  boxOwner: number[][]
  /** 连走指示：true=当前手刚得分、本方将继续走 */
  extraTurn: boolean
  /** 当前 turn 是否要求对方强制让行；非 turn 帧为 false。 */
  mustPass: boolean
  /** 每方已用秒（象棋钟）；null=不限时 */
  timeUsed: [number | null, number | null] | null
  /** 每方剩余秒 */
  timeRemaining: [number | null, number | null] | null
  /** 超时方（null=未超时） */
  timeOut: number | null
}

const GRID_DOT = 3
const GRID_EDGE = 4
const GRID_EDGE_USED = 5
const GRID_BOX = 2

function emptyGrid(size: number): number[][] {
  const g: number[][] = []
  for (let x = 0; x < size; x++) {
    const col: number[] = []
    for (let y = 0; y < size; y++) {
      if (x % 2 === 0 && y % 2 === 0) col.push(GRID_DOT)
      else if ((x + y) % 2 === 1) col.push(GRID_EDGE)
      else col.push(GRID_BOX)
    }
    g.push(col)
  }
  return g
}

function emptyBoxOwner(size: number): number[][] {
  // -2=非格心，-1=未占
  const g: number[][] = []
  for (let x = 0; x < size; x++) {
    const col: number[] = []
    for (let y = 0; y < size; y++) {
      col.push(x % 2 === 1 && y % 2 === 1 ? -1 : -2)
    }
    g.push(col)
  }
  return g
}

export function reducePencilEvents(events: RawEvent[]): PencilViewModel {
  let nDots = 6
  let size = 11
  let grid = emptyGrid(size)
  let scores: [number, number] = [0, 0]
  let lastEdge: { x: number; y: number } | null = null
  let toAct: number | null = null
  let matchOver = false
  let winner: number | null = null
  let reason = ''
  let moveCount = 0
  let status = 'live'
  let edgeOwner: Record<string, number> = {}
  let boxOwner: number[][] = emptyBoxOwner(size)
  let extraTurn = false
  let mustPass = false
  let timeUsed: [number | null, number | null] | null = null
  let timeRemaining: [number | null, number | null] | null = null
  let timeOut: number | null = null
  let timeMode: TimeControlMode | null = null
  let timeAppliesTo: TimeControlAppliesTo | null = null
  let botOnlySeat: number | null = null
  let timeSeconds: number | null = null
  let hasProjectedTimeControl = false

  for (const ev of events) {
    const t = String(ev.type || '')
    if (t === 'snapshot' && Array.isArray(ev.events)) {
      return reducePencilEvents(ev.events as RawEvent[])
    }
    if (t === 'match_start') {
      nDots = Number(ev.n_dots) || 6
      size = Number(ev.size) || 2 * nDots - 1
      grid = emptyGrid(size)
      boxOwner = emptyBoxOwner(size)
      scores = [0, 0]
      toAct = null
      edgeOwner = {}
      extraTurn = false
      mustPass = false
      hasProjectedTimeControl = ev.time_control !== undefined
      const timeControl = parseMatchTimeControl(ev.time_control, 'pencil')
      timeMode = timeControl?.mode ?? null
      timeAppliesTo = timeControl?.applies_to ?? null
      botOnlySeat = null
      timeSeconds = timeControl?.seconds ?? null
      timeUsed = timeSeconds === null ? null : timeAppliesTo === 'bot_only' ? [null, null] : [0, 0]
      timeRemaining = timeSeconds === null
        ? null
        : timeAppliesTo === 'bot_only'
          ? [null, null]
          : [timeSeconds, timeSeconds]
      timeOut = null
    } else if (t === 'turn') {
      toAct = typeof ev.player === 'number' ? ev.player : toAct
      mustPass = Number(ev.pass_) === 1
      if (
        timeMode === 'per_decision'
        && timeSeconds !== null
        && (toAct === 0 || toAct === 1)
        && (timeAppliesTo !== 'bot_only' || botOnlySeat === toAct)
      ) {
        if (!timeUsed) timeUsed = timeAppliesTo === 'bot_only' ? [null, null] : [0, 0]
        if (!timeRemaining) timeRemaining = timeAppliesTo === 'bot_only' ? [null, null] : [timeSeconds, timeSeconds]
        timeUsed[toAct] = 0
        timeRemaining[toAct] = timeSeconds
      }
      if (Array.isArray(ev.scores) && ev.scores.length >= 2) {
        scores = [Number(ev.scores[0]), Number(ev.scores[1])]
      }
    } else if (t === 'move') {
      const x = Number(ev.x)
      const y = Number(ev.y)
      const player = typeof ev.player === 'number' ? ev.player : 0
      if (x >= 0 && y >= 0 && x < size && y < size) {
        grid[x][y] = GRID_EDGE_USED
        edgeOwner[`${x},${y}`] = player
        lastEdge = { x, y }
      }
      if (Array.isArray(ev.scores) && ev.scores.length >= 2) {
        scores = [Number(ev.scores[0]), Number(ev.scores[1])]
      }
      moveCount = Number(ev.move_index) || moveCount + 1
      // move 与下一条权威 turn 之间不推测行动方；非得分手已换人，得分手
      // 还需等待对方 pass，均由随后的 turn 事件给出唯一真值。
      toAct = null
      mustPass = false
      // 消费 closed_boxes（本手新闭合格 + owner）——前端按归属着色
      const scored = !!ev.scored
      extraTurn = scored
      if (Array.isArray(ev.closed_boxes)) {
        for (const cb of ev.closed_boxes as { x: number; y: number; owner: number }[]) {
          const bx = Number(cb.x)
          const by = Number(cb.y)
          if (bx >= 0 && by >= 0 && bx < size && by < size) {
            boxOwner[bx][by] = Number(cb.owner)
          }
        }
      }
    } else if (t === 'pass') {
      // 对方正确 pass（连走方将再次行棋）——清 extraTurn
      extraTurn = false
      toAct = null
      mustPass = false
    } else if (t === 'illegal' || t === 'technical_incident') {
      // 决策已被裁判/平台接收，终局事件尚未到达；不得继续把该座位标成行动中。
      toAct = null
      mustPass = false
      extraTurn = false
    } else if (t === 'match_end') {
      matchOver = true
      winner = ev.winner === null || ev.winner === undefined ? null : Number(ev.winner)
      reason = String(ev.reason || '')
      status = 'match_end'
      extraTurn = false
      toAct = null
      mustPass = false
      if (Array.isArray(ev.scores) && ev.scores.length >= 2) {
        scores = [Number(ev.scores[0]), Number(ev.scores[1])]
      } else if (
        winner !== null
        && (reason === 'illegal' || reason === 'error' || reason === 'crash')
      ) {
        // The platform terminal is game-neutral and intentionally carries no
        // engine-only scores. Pencil's judge normalizes these adjudications to
        // 2:0, so reconstruct that visible result without inventing a score for
        // protocol/timeout platform faults.
        scores = winner === 0 ? [2, 0] : [0, 2]
      }
      // 消费 box_owners（最终归属网格，权威来源）
      if (Array.isArray(ev.box_owners)) {
        const bo = ev.box_owners as number[][]
        for (let x = 0; x < bo.length && x < size; x++) {
          for (let y = 0; y < (bo[x]?.length || 0) && y < size; y++) {
            boxOwner[x][y] = Number(bo[x][y])
          }
        }
      }
    } else if (t === 'error') {
      status = 'error'
      matchOver = true
      toAct = null
      mustPass = false
    } else if (t === 'time_used') {
      const seat = Number(ev.seat)
      if (seat !== 0 && seat !== 1) continue
      // A present-but-invalid frozen projection is not a legacy replay.  Do
      // not let clock-event budget fields silently reinterpret it.
      if (hasProjectedTimeControl && timeSeconds === null) continue
      const budget = Math.max(0, Number(ev.budget) || 0)
      // Historical replays predate match_start.time_control and carried the
      // fixed budget only on clock events.  Preserve that exact evidence, but
      // never let it rescue a present-yet-malformed new projection.
      if (timeSeconds === null && !hasProjectedTimeControl && budget > 0) {
        timeMode = 'per_side_total'
        timeAppliesTo = 'both_bots'
        timeSeconds = budget
      }
      if (timeAppliesTo === 'bot_only') botOnlySeat = seat
      if (!timeUsed) timeUsed = timeAppliesTo === 'bot_only' ? [null, null] : [0, 0]
      // The first clock event belongs to only one player. Initialise the untouched
      // player from the shared budget instead of showing a false 0:00 timeout.
      if (!timeRemaining) timeRemaining = timeAppliesTo === 'bot_only' ? [null, null] : [budget, budget]
      timeUsed[seat] = Number(ev.used) || 0
      timeRemaining[seat] = Number(ev.remaining) || 0
      toAct = null
      mustPass = false
    } else if (t === 'time_out') {
      const seat = Number(ev.seat)
      if (seat !== 0 && seat !== 1) continue
      if (hasProjectedTimeControl && timeSeconds === null) continue
      const budget = Math.max(0, Number(ev.budget) || 0)
      if (timeSeconds === null && !hasProjectedTimeControl && budget > 0) {
        timeMode = 'per_side_total'
        timeAppliesTo = 'both_bots'
        timeSeconds = budget
      }
      if (timeAppliesTo === 'bot_only') botOnlySeat = seat
      if (!timeUsed) timeUsed = timeAppliesTo === 'bot_only' ? [null, null] : [0, 0]
      if (!timeRemaining) timeRemaining = timeAppliesTo === 'bot_only' ? [null, null] : [budget, budget]
      timeUsed[seat] = Number(ev.used) || budget
      timeRemaining[seat] = 0
      timeOut = seat
      toAct = null
      mustPass = false
      extraTurn = false
    }
  }

  return {
    nDots,
    size,
    grid,
    scores,
    lastEdge,
    toAct,
    matchOver,
    winner,
    reason,
    moveCount,
    status,
    edgeOwner,
    boxOwner,
    extraTurn,
    mustPass,
    timeUsed,
    timeRemaining,
    timeOut,
  }
}

export function usePencilState(events: RawEvent[]): PencilViewModel {
  return useMemo(() => reducePencilEvents(events), [events])
}

export { GRID_DOT, GRID_EDGE, GRID_EDGE_USED, GRID_BOX }
