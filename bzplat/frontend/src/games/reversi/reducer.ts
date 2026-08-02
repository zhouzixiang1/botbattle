/**
 * 黑白棋事件归约（第 4 游戏前端，对标后端 games/reversi/engine.py）。
 *
 * 复用 gomoku/pencil 的 board 模式：match_start/turn/move/match_end/pass。
 * reversi 的 move 事件额外带 closed_boxes（被翻转的子坐标 + 新归属）。
 */
import { useMemo } from 'react'

import type { RawEvent } from '@/games/base'

export interface ReversiViewModel {
  size: number
  board: number[][] // -1 empty, 0 black, 1 white
  lastMove: { x: number; y: number } | null
  toAct: number | null
  matchOver: boolean
  winner: number | null
  reason: string
  moveCount: number
  status: string
  scores: [number, number] // [黑子数, 白子数]
}

const emptyBoard = (size: number) =>
  Array.from({ length: size }, () => Array(size).fill(-1) as number[])

/** 标准黑白棋开局棋盘（中心 4 子）。 */
function initialBoard(size: number): number[][] {
  const b = emptyBoard(size)
  const mid = Math.floor(size / 2)
  b[mid - 1][mid - 1] = 1 // 白
  b[mid][mid] = 1 // 白
  b[mid - 1][mid] = 0 // 黑
  b[mid][mid - 1] = 0 // 黑
  return b
}

function countDiscs(board: number[][]): [number, number] {
  let black = 0
  let white = 0
  for (const row of board) {
    for (const c of row) {
      if (c === 0) black++
      else if (c === 1) white++
    }
  }
  return [black, white]
}

export function reduceReversiEvents(events: RawEvent[]): ReversiViewModel {
  let size = 8
  let board = initialBoard(size)
  let lastMove: { x: number; y: number } | null = null
  let toAct: number | null = 0
  let matchOver = false
  let winner: number | null = null
  let reason = ''
  let moveCount = 0
  let status = 'live'
  let scores: [number, number] = [2, 2]

  for (const ev of events) {
    const t = String(ev.type || '')
    if (t === 'snapshot' && Array.isArray(ev.events)) {
      return reduceReversiEvents(ev.events as RawEvent[])
    }
    if (t === 'match_start') {
      size = Number(ev.size) || 8
      board = initialBoard(size)
      toAct = 0
      scores = countDiscs(board)
    } else if (t === 'turn') {
      toAct = typeof ev.player === 'number' ? ev.player : toAct
    } else if (t === 'move') {
      const x = Number(ev.x)
      const y = Number(ev.y)
      const p = typeof ev.player === 'number' ? ev.player : 0
      if (x >= 0 && y >= 0 && x < size && y < size) {
        board[x][y] = p
        lastMove = { x, y }
      }
      moveCount = Number(ev.move_index) || moveCount + 1
      // 翻转 closed_boxes（被夹的子归属更新）
      if (Array.isArray(ev.closed_boxes)) {
        for (const cb of ev.closed_boxes as { x: number; y: number; owner: number }[]) {
          const bx = Number(cb.x)
          const by = Number(cb.y)
          if (bx >= 0 && by >= 0 && bx < size && by < size) {
            board[bx][by] = Number(cb.owner)
          }
        }
      }
      scores = countDiscs(board)
    } else if (t === 'pass') {
      // 当前方无合法手 pass（不计 moveCount）
    } else if (t === 'match_end') {
      matchOver = true
      winner = ev.winner === null || ev.winner === undefined ? null : Number(ev.winner)
      reason = String(ev.reason || '')
      status = 'match_end'
      if (Array.isArray(ev.scores) && ev.scores.length >= 2) {
        scores = [Number(ev.scores[0]), Number(ev.scores[1])]
      } else if (Array.isArray(ev.board)) {
        board = ev.board as number[][]
        scores = countDiscs(board)
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
    scores,
  }
}

export function useReversiState(events: RawEvent[]): ReversiViewModel {
  return useMemo(() => reduceReversiEvents(events), [events])
}
