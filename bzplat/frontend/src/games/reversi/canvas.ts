/**
 * 黑白棋 canvas 渲染器（第 4 游戏前端，克隆 gomoku canvas 适配 8×8 翻转棋）。
 * 复用 reduceReversiEvents；棋子落子缩放+淡入；最后一手标记；顶栏显示双方子数。
 */
import type { RawEvent } from '@/games/base'
import { reduceReversiEvents, type ReversiViewModel } from './reducer'
import type { GameCanvasRenderer, Scene, SceneDelta } from '@/games/canvas-types'

interface ReversiScene extends Scene {
  size: number
  board: number[][]            // -1 空 / 0 黑 / 1 白
  lastMove: { x: number; y: number } | null
  toAct: number | null
  matchOver: boolean
  winner: number | null
  reason: string
  moveCount: number
  scores: [number, number]     // [黑子数, 白子数]
}

export const ReversiCanvasRenderer: GameCanvasRenderer<ReversiScene> = {
  toScene(events: RawEvent[]): ReversiScene {
    const vm: ReversiViewModel = reduceReversiEvents(events)
    return {
      size: vm.size,
      board: vm.board,
      lastMove: vm.lastMove,
      toAct: vm.toAct,
      matchOver: vm.matchOver,
      winner: vm.winner,
      reason: vm.reason,
      moveCount: vm.moveCount,
      scores: vm.scores,
    }
  },
  diff(prev: ReversiScene | null, next: ReversiScene): SceneDelta {
    if (!prev) return { animation: 'none' }
    if (next.matchOver && !prev.matchOver) return { animation: 'settle' }
    if (next.moveCount !== prev.moveCount) return { animation: 'place' }
    return { animation: 'none' }
  },
  draw(ctx, prev, next, t, opts) {
    const { cell, ox, oy, cx, cy } = reversiLayout(opts.width, opts.height, next.size)
    const W = opts.width
    const H = opts.height
    const size = next.size

    // 清屏 + 绿色棋盘背景（黑白棋传统色）
    ctx.clearRect(0, 0, W, H)
    ctx.fillStyle = '#2e7d32'
    ctx.fillRect(ox - cell / 2, oy - cell / 2, cell * size, cell * size)

    // 格线
    ctx.strokeStyle = 'rgba(0,0,0,0.3)'
    ctx.lineWidth = 1
    for (let i = 0; i <= size; i++) {
      ctx.beginPath(); ctx.moveTo(ox - cell / 2 + i * cell, oy - cell / 2); ctx.lineTo(ox - cell / 2 + i * cell, oy - cell / 2 + size * cell); ctx.stroke()
      ctx.beginPath(); ctx.moveTo(ox - cell / 2, oy - cell / 2 + i * cell); ctx.lineTo(ox - cell / 2 + size * cell, oy - cell / 2 + i * cell); ctx.stroke()
    }

    // 棋子（圆盘，居中于格而非交叉点——黑白棋在格内）
    for (let x = 0; x < size; x++) {
      for (let y = 0; y < size; y++) {
        const v = next.board[x]?.[y]
        if (v == null || v < 0) continue
        const isLast = next.lastMove?.x === x && next.lastMove?.y === y
        const wasPresent = prev?.board[x]?.[y] === v
        const scale = (isLast && !wasPresent && t < 1) ? Math.max(0.001, t) : 1
        const alpha = (isLast && !wasPresent && t < 1) ? t : 1
        const r = cell * 0.4 * scale
        ctx.save()
        ctx.globalAlpha = alpha
        ctx.beginPath()
        ctx.arc(cx(x), cy(y), r, 0, Math.PI * 2)
        ctx.fillStyle = v === 0 ? '#111' : '#f5f5f5'
        ctx.fill()
        ctx.lineWidth = 1.5
        ctx.strokeStyle = v === 0 ? '#000' : '#999'
        ctx.stroke()
        ctx.restore()
        // 最后一手标记
        if (isLast && t >= 1) {
          ctx.save()
          ctx.beginPath()
          ctx.arc(cx(x), cy(y), Math.max(2, cell * 0.1), 0, Math.PI * 2)
          ctx.fillStyle = '#f59e0b'
          ctx.fill()
          ctx.restore()
        }
      }
    }

    // 顶部信息：子数 / 待行 / 胜负 + 双方 BOT 名
    ctx.fillStyle = '#fff'
    ctx.font = 'bold 15px "DM Sans", sans-serif'
    ctx.textAlign = 'left'
    const name0 = seatShort(opts.seats?.[0], '黑')
    const name1 = seatShort(opts.seats?.[1], '白')
    const turnLabel = next.matchOver
      ? (next.winner === null
        ? `平局（${next.scores[0]}:${next.scores[1]}）`
        : `${next.winner === 0 ? name0 : name1}胜 ${next.scores[0]}:${next.scores[1]}${next.reason ? `（${next.reason}）` : ''}`)
      : `待行：${next.toAct === 0 ? name0 : next.toAct === 1 ? name1 : '—'}`
    ctx.fillText(`黑白棋 · ${size}×${size} · 第 ${next.moveCount} 手 · ${turnLabel}`, 12, 24)
    ctx.font = '12px "DM Sans", sans-serif'
    ctx.fillText(`● ${name0}（黑 ${next.scores[0]}）  ○ ${name1}（白 ${next.scores[1]}）`, 12, 42)
  },
  pick(canvasX, canvasY, scene, opts) {
    const s = scene as ReversiScene
    const { cell, ox, oy } = reversiLayout(opts.width, opts.height, s.size)
    // 黑白棋棋子在格内居中，pick 用格中心
    const gx = Math.floor((canvasX - (ox - cell / 2)) / cell)
    const gy = Math.floor((canvasY - (oy - cell / 2)) / cell)
    if (gx < 0 || gy < 0 || gx >= s.size || gy >= s.size) return null
    return { x: gx, y: gy }
  },
}

function seatShort(
  info: { botName?: string; ownerName?: string; isHuman?: boolean } | undefined,
  fallback: string,
): string {
  const bot = (info?.botName || '').trim()
  if (bot) return bot
  const owner = (info?.ownerName || '').trim()
  if (owner) return owner
  return fallback
}

/** reversi 棋盘布局：棋子居中于格（非交叉点），draw 与 pick 共用。 */
function reversiLayout(W: number, H: number, size: number) {
  const margin = 50
  const cell = Math.max(8, Math.floor(Math.min(W - margin * 2, H - margin * 2) / size))
  const boardPx = cell * size
  const ox = (W - boardPx) / 2 + cell / 2  // 第一个格中心
  const oy = (H - boardPx) / 2 + cell / 2
  const cx = (x: number) => ox + x * cell
  const cy = (y: number) => oy + y * cell
  return { cell, ox, oy, cx, cy }
}
