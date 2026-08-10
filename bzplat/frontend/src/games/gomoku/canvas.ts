/**
 * 五子棋 canvas 渲染器（PR-B：DOM GomokuBoard → canvas + GSAP）。
 * 复用 reduceGomokuEvents（不重写归约）；棋子落子缩放+淡入动画；最后一手标记脉冲。
 */
import type { RawEvent } from '@/games/base'
import { fitText, scaleFactor } from '@/games/base'
import { reduceGomokuEvents, type GomokuViewModel } from './reducer'
import { gomokuTerminalReason } from './reasons'
import type { GameCanvasRenderer, Scene, SceneDelta } from '@/games/canvas-types'

interface GomokuScene extends Scene {
  size: number
  board: number[][]            // -1 空 / 0 黑 / 1 白
  lastMove: { x: number; y: number } | null
  toAct: number | null
  matchOver: boolean
  winner: number | null
  reason: string
  moveCount: number
}

export const GomokuCanvasRenderer: GameCanvasRenderer<GomokuScene> = {
  toScene(events: RawEvent[]): GomokuScene {
    const vm: GomokuViewModel = reduceGomokuEvents(events)
    return {
      size: vm.size,
      board: vm.board,
      lastMove: vm.lastMove,
      toAct: vm.toAct,
      matchOver: vm.matchOver,
      winner: vm.winner,
      reason: vm.reason,
      moveCount: vm.moveCount,
    }
  },
  diff(prev: GomokuScene | null, next: GomokuScene): SceneDelta {
    if (!prev) return { animation: 'none' }
    if (next.matchOver && !prev.matchOver) return { animation: 'settle' }
    if (next.moveCount !== prev.moveCount) return { animation: 'place' }
    return { animation: 'none' }
  },
  draw(ctx, prev, next, t, opts) {
    const { cell, cx, cy } = gomokuLayout(opts.width, opts.height, next.size)
    const W = opts.width
    const H = opts.height
    const size = next.size

    // 清屏 + 木色背景（填满整个 canvas，避免方形棋盘两侧透明留白）
    ctx.clearRect(0, 0, W, H)
    ctx.fillStyle = '#e8c98a'
    ctx.fillRect(0, 0, W, H)

    // 网格线
    ctx.strokeStyle = '#8b6914'
    ctx.lineWidth = 1
    for (let i = 0; i < size; i++) {
      ctx.beginPath(); ctx.moveTo(cx(0), cy(i)); ctx.lineTo(cx(size - 1), cy(i)); ctx.stroke()
      ctx.beginPath(); ctx.moveTo(cx(i), cy(0)); ctx.lineTo(cx(i), cy(size - 1)); ctx.stroke()
    }

    // 星位（天元+星）：size≥9 时在 (size-1)/4 的倍数处画实心点。
    // 真实棋盘视觉必备——无星位的棋盘「看起来没做完」。
    if (size >= 9) {
      const step = (size - 1) / 4
      const starPos = [step, step * 2, step * 3]
      ctx.fillStyle = '#8b6914'
      for (const sx of starPos) {
        for (const sy of starPos) {
          const ix = Math.round(sx), iy = Math.round(sy)
          ctx.beginPath()
          ctx.arc(cx(ix), cy(iy), Math.max(2, cell * 0.1), 0, Math.PI * 2)
          ctx.fill()
        }
      }
    }

    // 棋子（新棋子缩放+淡入：r 从 0→cell*0.42，alpha 0→1）
    for (let x = 0; x < size; x++) {
      for (let y = 0; y < size; y++) {
        const v = next.board[x]?.[y]
        if (v == null || v < 0) continue
        const isLast = next.lastMove?.x === x && next.lastMove?.y === y
        // 新落的子用 t 缩放进入；其余子直接画
        const wasPresent = prev?.board[x]?.[y] === v
        const scale = (isLast && !wasPresent && t < 1) ? Math.max(0.001, t) : 1
        const alpha = (isLast && !wasPresent && t < 1) ? t : 1
        const r = cell * 0.42 * scale
        ctx.save()
        ctx.globalAlpha = alpha
        ctx.beginPath()
        ctx.arc(cx(x), cy(y), r, 0, Math.PI * 2)
        ctx.fillStyle = v === 0 ? '#1a1a1a' : '#f5f5f5'
        ctx.fill()
        ctx.lineWidth = 1
        ctx.strokeStyle = v === 0 ? '#000' : '#888'
        ctx.stroke()
        ctx.restore()
        // 最后一手标记（脉冲：r 随 t 缩放）
        if (isLast && t >= 1) {
          ctx.save()
          ctx.beginPath()
          ctx.arc(cx(x), cy(y), Math.max(2, cell * 0.12), 0, Math.PI * 2)
          ctx.fillStyle = v === 0 ? '#f59e0b' : '#dc2626'
          ctx.fill()
          ctx.restore()
        }
      }
    }

    // 顶部信息：步数 / 待行 / 胜负 + 双方 BOT 名（字体/偏移按 s=W/W0 缩放，fitText 防溢出）
    const s = scaleFactor(W)
    ctx.fillStyle = '#5b4413'
    ctx.font = `bold ${Math.round(15 * s)}px "DM Sans", sans-serif`
    ctx.textAlign = 'left'
    const sc = ['黑', '白']
    const name0 = seatShort(opts.seats?.[0], sc[0] ?? '黑')
    const name1 = seatShort(opts.seats?.[1], sc[1] ?? '白')
    const turnLabel = next.matchOver
      ? (next.winner === null
        ? '平局'
        : `${next.winner === 0 ? name0 : name1}胜${next.reason ? `（${gomokuTerminalReason(next.reason, 'completed').label}）` : ''}`)
      : `待行：${next.toAct === 0 ? name0 : next.toAct === 1 ? name1 : '—'}`
    ctx.fillText(fitText(ctx, `五子棋 · ${size}×${size} · 第 ${next.moveCount} 手 · ${turnLabel}`, W - 24 * s), 12 * s, 24 * s)
    ctx.font = `${Math.round(12 * s)}px "DM Sans", sans-serif`
    ctx.fillText(fitText(ctx, `● ${name0}（黑）  ○ ${name1}（白）`, W - 24 * s), 12 * s, 42 * s)
  },
  pick(canvasX, canvasY, scene, opts) {
    const s = scene as GomokuScene
    const { cell, ox, oy } = gomokuLayout(opts.width, opts.height, s.size)
    const gx = Math.round((canvasX - ox) / cell)
    const gy = Math.round((canvasY - oy) / cell)
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
  if (owner) return info?.isHuman ? `${owner}` : owner
  return fallback
}

/** gomoku 棋盘布局（draw 与 pick 共用，保证坐标一致）。margin 按宽比例缩放（对齐 holdem）。 */
function gomokuLayout(W: number, H: number, size: number) {
  const margin = Math.max(24, W * 0.05)
  const cell = Math.max(8, Math.floor(Math.min(W - margin * 2, H - margin * 2) / (size + 1)))
  const boardPx = cell * (size - 1)
  const ox = (W - boardPx) / 2
  const oy = (H - boardPx) / 2
  const cx = (x: number) => ox + x * cell
  const cy = (y: number) => oy + y * cell
  return { cell, ox, oy, cx, cy }
}
