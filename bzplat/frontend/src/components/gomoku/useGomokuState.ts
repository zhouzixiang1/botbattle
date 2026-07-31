import { useMemo } from 'react'

export type RawEvent = Record<string, unknown> & { type?: string }

export interface GomokuViewModel {
  size: number
  board: number[][] // -1 empty, 0 black, 1 white
  lastMove: { x: number; y: number } | null
  toAct: number | null
  matchOver: boolean
  winner: number | null
  reason: string
  moveCount: number
  status: string
}

const emptyBoard = (size: number) =>
  Array.from({ length: size }, () => Array(size).fill(-1) as number[])

export function reduceGomokuEvents(events: RawEvent[]): GomokuViewModel {
  let size = 15
  let board = emptyBoard(size)
  let lastMove: { x: number; y: number } | null = null
  let toAct: number | null = 0
  let matchOver = false
  let winner: number | null = null
  let reason = ''
  let moveCount = 0
  let status = 'live'

  for (const ev of events) {
    const t = String(ev.type || '')
    if (t === 'snapshot' && Array.isArray(ev.events)) {
      return reduceGomokuEvents(ev.events as RawEvent[])
    }
    if (t === 'match_start') {
      size = Number(ev.size) || 15
      board = emptyBoard(size)
      toAct = 0
    } else if (t === 'turn') {
      toAct = typeof ev.player === 'number' ? ev.player : toAct
    } else if (t === 'move') {
      const x = Number(ev.x)
      const y = Number(ev.y)
      const p = Number(ev.player)
      if (x >= 0 && y >= 0 && x < size && y < size) {
        board[x][y] = p
        lastMove = { x, y }
        moveCount = Number(ev.move_index) || moveCount + 1
      }
    } else if (t === 'match_end') {
      matchOver = true
      winner = ev.winner === null || ev.winner === undefined ? null : Number(ev.winner)
      reason = String(ev.reason || '')
      status = 'match_end'
      if (Array.isArray(ev.board)) {
        board = ev.board as number[][]
      }
    } else if (t === 'error') {
      status = 'error'
      matchOver = true
    }
  }

  return {
    size,
    board,
    lastMove,
    toAct,
    matchOver,
    winner,
    reason,
    moveCount,
    status,
  }
}

export function useGomokuState(events: RawEvent[]): GomokuViewModel {
  return useMemo(() => reduceGomokuEvents(events), [events])
}
