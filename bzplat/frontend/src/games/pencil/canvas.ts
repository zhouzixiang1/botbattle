/**
 * 点格棋 canvas 渲染器（PR-C：DOM PencilBoard → canvas + GSAP）。
 * 复用 reducePencilEvents（不重写归约）；新占边沿线绘制动画；闭合格归属淡入。
 */
import type { RawEvent } from '@/components/pencil/usePencilState'
import {
  reducePencilEvents, type PencilViewModel,
  GRID_DOT, GRID_EDGE_USED, GRID_BOX,
} from '@/components/pencil/usePencilState'
import type { GameCanvasRenderer, Scene, SceneDelta } from '@/games/canvas-types'

const EDGE_COLOR = ['#ef4444', '#3b82f6']
const BOX_FILL = ['rgba(239,68,68,0.25)', 'rgba(59,130,246,0.25)']

interface PencilScene extends Scene {
  nDots: number
  size: number
  grid: number[][]
  scores: [number, number]
  lastEdge: { x: number; y: number } | null
  toAct: number | null
  matchOver: boolean
  winner: number | null
  reason: string
  moveCount: number
  edgeOwner: Record<string, number>
  boxOwner: number[][]
  extraTurn: boolean
}

export const PencilCanvasRenderer: GameCanvasRenderer<PencilScene> = {
  toScene(events: RawEvent[]): PencilScene {
    const vm: PencilViewModel = reducePencilEvents(events)
    return {
      nDots: vm.nDots, size: vm.size, grid: vm.grid, scores: vm.scores,
      lastEdge: vm.lastEdge, toAct: vm.toAct, matchOver: vm.matchOver,
      winner: vm.winner, reason: vm.reason, moveCount: vm.moveCount,
      edgeOwner: vm.edgeOwner, boxOwner: vm.boxOwner, extraTurn: vm.extraTurn,
    }
  },
  diff(prev: PencilScene | null, next: PencilScene): SceneDelta {
    if (!prev) return { animation: 'none' }
    if (next.matchOver && !prev.matchOver) return { animation: 'settle' }
    if (next.moveCount !== prev.moveCount) return { animation: 'place' }
    return { animation: 'none' }
  },
  draw(ctx, prev, next, t, opts) {
    const { cell, ox, oy, cx, cy } = pencilLayout(opts.width, opts.height, next.size)
    const W = opts.width
    const H = opts.height
    const size = next.size

    ctx.clearRect(0, 0, W, H)
    ctx.fillStyle = 'rgba(255,255,255,0.9)'
    ctx.fillRect(ox - cell / 2, oy - cell / 2, cell * (size - 1) + cell, cell * (size - 1) + cell)

    // 闭合格归属填充（新闭合的格淡入：alpha 随 t）
    for (let x = 0; x < size; x++) {
      for (let y = 0; y < size; y++) {
        if (next.grid[x]?.[y] !== GRID_BOX) continue
        const owner = next.boxOwner[x]?.[y]
        if (owner !== 0 && owner !== 1) continue
        // 判断本格是否是"新闭合"（上一帧未占）
        const wasOwned = prev?.boxOwner[x]?.[y] === owner
        const alpha = wasOwned ? 1 : t
        ctx.save()
        ctx.globalAlpha = alpha
        ctx.fillStyle = BOX_FILL[owner]
        ctx.fillRect(cx(x) - cell / 2, cy(y) - cell / 2, cell, cell)
        ctx.restore()
      }
    }

    // 边（已占边按玩家着色；新占边沿线绘制：length 随 t 0→1）
    for (let x = 0; x < size; x++) {
      for (let y = 0; y < size; y++) {
        const v = next.grid[x]?.[y]
        if (v !== GRID_EDGE_USED) continue
        const horiz = y % 2 === 1 && x % 2 === 0
        const owner = next.edgeOwner[`${x},${y}`]
        const color = owner === 0 || owner === 1 ? EDGE_COLOR[owner] : '#0f172a'
        const isLast = next.lastEdge?.x === x && next.lastEdge?.y === y
        const wasUsed = prev?.grid[x]?.[y] === GRID_EDGE_USED
        // 新占边：从起点画到 t 比例处（沿线动画）
        const frac = (!wasUsed && t < 1) ? t : 1
        const sw = Math.max(3, cell * (isLast ? 0.22 : 0.16))
        ctx.save()
        ctx.strokeStyle = color
        ctx.lineWidth = sw
        ctx.lineCap = 'round'
        ctx.beginPath()
        if (horiz) {
          ctx.moveTo(cx(x) - cell / 2, cy(y))
          ctx.lineTo(cx(x) - cell / 2 + cell * frac, cy(y))
        } else {
          ctx.moveTo(cx(x), cy(y) - cell / 2)
          ctx.lineTo(cx(x), cy(y) - cell / 2 + cell * frac)
        }
        ctx.stroke()
        ctx.restore()
      }
    }

    // 圆点
    ctx.fillStyle = '#334155'
    for (let x = 0; x < size; x++) {
      for (let y = 0; y < size; y++) {
        if (next.grid[x]?.[y] === GRID_DOT) {
          ctx.beginPath()
          ctx.arc(cx(x), cy(y), Math.max(2, cell * 0.12), 0, Math.PI * 2)
          ctx.fill()
        }
      }
    }

    // 顶部信息
    ctx.fillStyle = '#334155'
    ctx.font = 'bold 15px "DM Sans", sans-serif'
    ctx.textAlign = 'left'
    const turnLabel = next.matchOver
      ? (next.winner === null ? '平局' : `${next.winner === 0 ? '红' : '蓝'}胜（${next.reason}）`)
      : `待行：${next.toAct === 0 ? '红' : next.toAct === 1 ? '蓝' : '—'}${next.extraTurn ? '（连走）' : ''}`
    ctx.fillText(
      `点格棋 · ${next.nDots}×${next.nDots} · 红 ${next.scores[0]} : ${next.scores[1]} 蓝 · ${turnLabel}`,
      12, 24,
    )
  },
  pick(canvasX, canvasY, scene, opts) {
    const s = scene as PencilScene
    const { cell, ox, oy } = pencilLayout(opts.width, opts.height, s.size)
    const gx = Math.round((canvasX - ox) / cell)
    const gy = Math.round((canvasY - oy) / cell)
    if (gx < 0 || gy < 0 || gx >= s.size || gy >= s.size) return null
    return { x: gx, y: gy }
  },
}

/** pencil 棋盘布局（draw 与 pick 共用）。 */
function pencilLayout(W: number, H: number, size: number) {
  const margin = 40
  const cell = Math.max(10, Math.floor(Math.min(W - margin * 2, H - margin * 2) / (size + 1)))
  const boardPx = cell * (size - 1)
  const ox = (W - boardPx) / 2
  const oy = (H - boardPx) / 2
  const cx = (x: number) => ox + x * cell
  const cy = (y: number) => oy + y * cell
  return { cell, ox, oy, cx, cy }
}
