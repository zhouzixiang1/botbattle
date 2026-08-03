/**
 * 点格棋 canvas 渲染器（PR-C：DOM PencilBoard → canvas + GSAP）。
 * 复用 reducePencilEvents（不重写归约）；新占边沿线绘制动画；闭合格归属淡入。
 */
import type { RawEvent } from '@/games/base'
import { getGame } from '@/games'
import {
  reducePencilEvents, type PencilViewModel,
  GRID_DOT, GRID_EDGE, GRID_EDGE_USED, GRID_BOX,
} from './reducer'
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

    // 边：未占灰色细线 + 已占按玩家着色（新占边沿线动画）
    for (let x = 0; x < size; x++) {
      for (let y = 0; y < size; y++) {
        const v = next.grid[x]?.[y]
        if (v !== GRID_EDGE && v !== GRID_EDGE_USED) continue
        const horiz = y % 2 === 1 && x % 2 === 0
        const used = v === GRID_EDGE_USED
        const owner = next.edgeOwner[`${x},${y}`]
        const color = used
          ? (owner === 0 || owner === 1 ? EDGE_COLOR[owner] : '#0f172a')
          : 'rgba(148,163,184,0.55)'
        const isLast = used && next.lastEdge?.x === x && next.lastEdge?.y === y
        const wasUsed = prev?.grid[x]?.[y] === GRID_EDGE_USED
        const frac = used && !wasUsed && t < 1 ? t : 1
        const sw = Math.max(used ? 3 : 1.5, cell * (isLast ? 0.22 : used ? 0.16 : 0.06))
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

    // 闭合格内归属标记（首字母/分数）
    ctx.font = `bold ${Math.max(10, Math.floor(cell * 0.35))}px "DM Sans", sans-serif`
    ctx.textAlign = 'center'
    ctx.textBaseline = 'middle'
    for (let x = 0; x < size; x++) {
      for (let y = 0; y < size; y++) {
        if (next.grid[x]?.[y] !== GRID_BOX) continue
        const owner = next.boxOwner[x]?.[y]
        if (owner !== 0 && owner !== 1) continue
        const wasOwned = prev?.boxOwner[x]?.[y] === owner
        const alpha = wasOwned ? 1 : t
        ctx.save()
        ctx.globalAlpha = alpha
        ctx.fillStyle = EDGE_COLOR[owner]
        const label = opts.seats?.[owner]?.botName?.[0]
          || opts.seats?.[owner]?.ownerName?.[0]
          || (owner === 0 ? 'R' : 'B')
        ctx.fillText(label.toUpperCase(), cx(x), cy(y))
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

    // 顶部信息 + 双方名
    ctx.fillStyle = '#334155'
    ctx.font = 'bold 15px "DM Sans", sans-serif'
    ctx.textAlign = 'left'
    ctx.textBaseline = 'alphabetic'
    const sc = getGame('pencil').seatColors ?? ['红', '蓝']
    const name0 = seatShort(opts.seats?.[0], sc[0] ?? '红')
    const name1 = seatShort(opts.seats?.[1], sc[1] ?? '蓝')
    const turnLabel = next.matchOver
      ? (next.winner === null
        ? '平局'
        : `${next.winner === 0 ? name0 : name1}胜（${next.reason}）`)
      : `待行：${next.toAct === 0 ? name0 : next.toAct === 1 ? name1 : '—'}${next.extraTurn ? '（连走）' : ''}`
    ctx.fillText(
      `点格棋 · ${next.nDots}×${next.nDots} · ${name0} ${next.scores[0]} : ${next.scores[1]} ${name1} · ${turnLabel}`,
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
