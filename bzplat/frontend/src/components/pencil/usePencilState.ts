import { useMemo } from 'react'

export type RawEvent = Record<string, unknown> & { type?: string }

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

export function reducePencilEvents(events: RawEvent[]): PencilViewModel {
  let nDots = 11
  let size = 21
  let grid = emptyGrid(size)
  let scores: [number, number] = [0, 0]
  let lastEdge: { x: number; y: number } | null = null
  let toAct: number | null = 0
  let matchOver = false
  let winner: number | null = null
  let reason = ''
  let moveCount = 0
  let status = 'live'

  for (const ev of events) {
    const t = String(ev.type || '')
    if (t === 'snapshot' && Array.isArray(ev.events)) {
      return reducePencilEvents(ev.events as RawEvent[])
    }
    if (t === 'match_start') {
      nDots = Number(ev.n_dots) || 11
      size = Number(ev.size) || 2 * nDots - 1
      grid = emptyGrid(size)
      scores = [0, 0]
      toAct = 0
    } else if (t === 'turn') {
      toAct = typeof ev.player === 'number' ? ev.player : toAct
      if (Array.isArray(ev.scores) && ev.scores.length >= 2) {
        scores = [Number(ev.scores[0]), Number(ev.scores[1])]
      }
    } else if (t === 'move') {
      const x = Number(ev.x)
      const y = Number(ev.y)
      if (x >= 0 && y >= 0 && x < size && y < size) {
        grid[x][y] = GRID_EDGE_USED
        lastEdge = { x, y }
      }
      if (Array.isArray(ev.scores) && ev.scores.length >= 2) {
        scores = [Number(ev.scores[0]), Number(ev.scores[1])]
      }
      moveCount = Number(ev.move_index) || moveCount + 1
    } else if (t === 'match_end') {
      matchOver = true
      winner = ev.winner === null || ev.winner === undefined ? null : Number(ev.winner)
      reason = String(ev.reason || '')
      status = 'match_end'
      if (Array.isArray(ev.scores) && ev.scores.length >= 2) {
        scores = [Number(ev.scores[0]), Number(ev.scores[1])]
      }
    } else if (t === 'error') {
      status = 'error'
      matchOver = true
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
  }
}

export function usePencilState(events: RawEvent[]): PencilViewModel {
  return useMemo(() => reducePencilEvents(events), [events])
}

export { GRID_DOT, GRID_EDGE, GRID_EDGE_USED, GRID_BOX }
