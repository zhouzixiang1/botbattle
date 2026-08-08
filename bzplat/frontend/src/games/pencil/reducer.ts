import { useMemo } from 'react'

import type { RawEvent } from '@/games/base'

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
  /** 每方已用秒（象棋钟）；null=不限时 */
  timeUsed: [number, number] | null
  /** 每方剩余秒 */
  timeRemaining: [number, number] | null
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
  let toAct: number | null = 0
  let matchOver = false
  let winner: number | null = null
  let reason = ''
  let moveCount = 0
  let status = 'live'
  let edgeOwner: Record<string, number> = {}
  let boxOwner: number[][] = emptyBoxOwner(size)
  let extraTurn = false
  let timeUsed: [number, number] | null = null
  let timeRemaining: [number, number] | null = null
  let timeOut: number | null = null

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
      toAct = 0
      edgeOwner = {}
      extraTurn = false
    } else if (t === 'turn') {
      toAct = typeof ev.player === 'number' ? ev.player : toAct
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
    } else if (t === 'match_end') {
      matchOver = true
      winner = ev.winner === null || ev.winner === undefined ? null : Number(ev.winner)
      reason = String(ev.reason || '')
      status = 'match_end'
      extraTurn = false
      if (Array.isArray(ev.scores) && ev.scores.length >= 2) {
        scores = [Number(ev.scores[0]), Number(ev.scores[1])]
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
    } else if (t === 'time_used') {
      const seat = Number(ev.seat)
      if (!timeUsed) timeUsed = [0, 0]
      if (!timeRemaining) timeRemaining = [0, 0]
      timeUsed[seat] = Number(ev.used) || 0
      timeRemaining[seat] = Number(ev.remaining) || 0
    } else if (t === 'time_out') {
      timeOut = Number(ev.seat)
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
    timeUsed,
    timeRemaining,
    timeOut,
  }
}

export function usePencilState(events: RawEvent[]): PencilViewModel {
  return useMemo(() => reducePencilEvents(events), [events])
}

export { GRID_DOT, GRID_EDGE, GRID_EDGE_USED, GRID_BOX }
