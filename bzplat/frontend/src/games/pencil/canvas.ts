/**
 * 点格棋 canvas 渲染器。
 *
 * 交错坐标的几何含义只有一套：偶偶是点、奇偶是水平边、偶奇是垂直边、
 * 奇奇是格心。draw 与 pick 共用下方导出的几何函数，避免“看见一条横边、
 * 点击却提交另一坐标”的双实现漂移。
 */
import type { RawEvent } from '@/games/base'
import type { GameCanvasRenderer, Scene, SceneDelta } from '@/games/canvas-types'
import {
  GRID_BOX,
  GRID_DOT,
  GRID_EDGE,
  GRID_EDGE_USED,
  reducePencilEvents,
  type PencilViewModel,
} from '@/games/pencil/reducer'

/** Canvas 内使用固定、跨主题都清晰的棋盘色板。 */
const COLORS = {
  background: '#f8fafc',
  board: '#ffffff',
  boardBorder: '#e2e8f0',
  dot: '#334155',
  emptyEdge: '#64748b',
  seat: ['#ef4444', '#2563eb'],
  box: ['rgba(239,68,68,0.22)', 'rgba(37,99,235,0.20)'],
  hover: '#047857',
  hoverHalo: 'rgba(16,185,129,0.22)',
  last: '#b45309',
} as const

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

export interface PencilCanvasLayout {
  cell: number
  boardPx: number
  ox: number
  oy: number
  cx: (x: number) => number
  cy: (y: number) => number
}

export interface PencilSegment {
  horizontal: boolean
  x1: number
  y1: number
  x2: number
  y2: number
}

export interface PencilBoxRect {
  left: number
  top: number
  width: number
  height: number
}

/** 棋盘布局：11×11 交错坐标完整铺入方形画布，外圈只保留约 5.5% 安全边距。 */
export function pencilCanvasLayout(W: number, H: number, size: number): PencilCanvasLayout {
  const minSide = Math.max(1, Math.min(W, H))
  const margin = Math.max(18, minSide * 0.055)
  const available = Math.max(1, minSide - margin * 2)
  const cell = available / Math.max(1, size - 1)
  const boardPx = cell * (size - 1)
  const ox = (W - boardPx) / 2
  const oy = (H - boardPx) / 2
  const cx = (x: number) => ox + x * cell
  const cy = (y: number) => oy + y * cell
  return { cell, boardPx, ox, oy, cx, cy }
}

export function isPencilEdgeCoordinate(x: number, y: number, size: number): boolean {
  return Number.isInteger(x)
    && Number.isInteger(y)
    && x >= 0
    && y >= 0
    && x < size
    && y < size
    && (x + y) % 2 === 1
}

/**
 * 边中心到相邻两点的真实线段。奇 x/偶 y 为水平边；偶 x/奇 y 为垂直边。
 * 两个点在交错坐标中相距 2，因此边的半长恰好是一个 cell。
 */
export function pencilEdgeSegment(
  x: number,
  y: number,
  size: number,
  layout: PencilCanvasLayout,
): PencilSegment | null {
  if (!isPencilEdgeCoordinate(x, y, size)) return null
  const horizontal = x % 2 === 1
  return horizontal
    ? {
        horizontal,
        x1: layout.cx(x) - layout.cell,
        y1: layout.cy(y),
        x2: layout.cx(x) + layout.cell,
        y2: layout.cy(y),
      }
    : {
        horizontal,
        x1: layout.cx(x),
        y1: layout.cy(y) - layout.cell,
        x2: layout.cx(x),
        y2: layout.cy(y) + layout.cell,
      }
}

/** 一个格心 (奇,奇) 的可视区域由四个相邻点围成，宽高均为 2×cell。 */
export function pencilBoxRect(
  x: number,
  y: number,
  layout: PencilCanvasLayout,
): PencilBoxRect | null {
  if (x % 2 !== 1 || y % 2 !== 1) return null
  return {
    left: layout.cx(x) - layout.cell,
    top: layout.cy(y) - layout.cell,
    width: layout.cell * 2,
    height: layout.cell * 2,
  }
}

function distanceToSegment(px: number, py: number, segment: PencilSegment): number {
  const dx = segment.x2 - segment.x1
  const dy = segment.y2 - segment.y1
  const lenSq = dx * dx + dy * dy
  const u = lenSq > 0
    ? Math.max(0, Math.min(1, ((px - segment.x1) * dx + (py - segment.y1) * dy) / lenSq))
    : 0
  return Math.hypot(px - (segment.x1 + u * dx), py - (segment.y1 + u * dy))
}

/**
 * 只吸附到“尚未占用”的合法边。格心、点、已占边和棋盘外全部返回 null；
 * 点附近同时邻接多条边，保留拒绝区以免一次含糊点击替用户选择方向。
 */
export function pickPencilEdge(
  canvasX: number,
  canvasY: number,
  grid: number[][],
  W: number,
  H: number,
): { x: number; y: number } | null {
  const size = grid.length
  if (size <= 0) return null
  const layout = pencilCanvasLayout(W, H, size)
  const right = layout.ox + layout.boardPx
  const bottom = layout.oy + layout.boardPx
  // CSS 像素 → canvas 设计坐标会经过 DPR 与布局缩放；最外圈边中心可能产生
  // 不足 1px 的浮点/取整误差。只容许 1px，真正位于棋盘外的点击仍被拒绝。
  const boundaryTolerance = 1
  if (
    canvasX < layout.ox - boundaryTolerance
    || canvasX > right + boundaryTolerance
    || canvasY < layout.oy - boundaryTolerance
    || canvasY > bottom + boundaryTolerance
  ) return null

  // 点本身不是边；明确留出拒绝半径，避免四向交点的随机吸附。
  const dotRejectRadius = Math.max(6, layout.cell * 0.2)
  for (let x = 0; x < size; x += 2) {
    for (let y = 0; y < size; y += 2) {
      if (Math.hypot(canvasX - layout.cx(x), canvasY - layout.cy(y)) <= dotRejectRadius) {
        return null
      }
    }
  }

  let best: { x: number; y: number; distance: number } | null = null
  let secondDistance = Number.POSITIVE_INFINITY
  for (let x = 0; x < size; x++) {
    for (let y = 0; y < size; y++) {
      // GRID_EDGE_USED 不参与候选，保证前端永远不会重发已占边。
      if (grid[x]?.[y] !== GRID_EDGE) continue
      const segment = pencilEdgeSegment(x, y, size, layout)
      if (!segment) continue
      const distance = distanceToSegment(canvasX, canvasY, segment)
      if (!best || distance < best.distance) {
        secondDistance = best?.distance ?? Number.POSITIVE_INFINITY
        best = { x, y, distance }
      } else if (distance < secondDistance) {
        secondDistance = distance
      }
    }
  }

  const hitRadius = Math.min(24, Math.max(8, layout.cell * 0.28))
  if (!best || best.distance > hitRadius) return null
  // 两条边几乎等距时不猜方向；移动端容许的差值按 cell 缩放。
  if (secondDistance - best.distance <= Math.max(2, layout.cell * 0.05)) return null
  return { x: best.x, y: best.y }
}

function strokeSegment(
  ctx: CanvasRenderingContext2D,
  segment: PencilSegment,
  fraction: number,
  color: string,
  width: number,
): void {
  const frac = Math.max(0, Math.min(1, fraction))
  ctx.save()
  ctx.strokeStyle = color
  ctx.lineWidth = width
  ctx.lineCap = 'round'
  ctx.beginPath()
  ctx.moveTo(segment.x1, segment.y1)
  ctx.lineTo(
    segment.x1 + (segment.x2 - segment.x1) * frac,
    segment.y1 + (segment.y2 - segment.y1) * frac,
  )
  ctx.stroke()
  ctx.restore()
}

/** Canvas roundRect 在旧 Safari 缺失时的等价路径。 */
function roundedRectPath(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  width: number,
  height: number,
  radius: number,
): void {
  const r = Math.max(0, Math.min(radius, width / 2, height / 2))
  if (typeof ctx.roundRect === 'function') {
    ctx.roundRect(x, y, width, height, r)
    return
  }
  ctx.moveTo(x + r, y)
  ctx.lineTo(x + width - r, y)
  ctx.quadraticCurveTo(x + width, y, x + width, y + r)
  ctx.lineTo(x + width, y + height - r)
  ctx.quadraticCurveTo(x + width, y + height, x + width - r, y + height)
  ctx.lineTo(x + r, y + height)
  ctx.quadraticCurveTo(x, y + height, x, y + height - r)
  ctx.lineTo(x, y + r)
  ctx.quadraticCurveTo(x, y, x + r, y)
  ctx.closePath()
}

export const PencilCanvasRenderer: GameCanvasRenderer<PencilScene> = {
  toScene(events: RawEvent[]): PencilScene {
    const vm: PencilViewModel = reducePencilEvents(events)
    return {
      nDots: vm.nDots,
      size: vm.size,
      grid: vm.grid,
      scores: vm.scores,
      lastEdge: vm.lastEdge,
      toAct: vm.toAct,
      matchOver: vm.matchOver,
      winner: vm.winner,
      reason: vm.reason,
      moveCount: vm.moveCount,
      edgeOwner: vm.edgeOwner,
      boxOwner: vm.boxOwner,
      extraTurn: vm.extraTurn,
    }
  },
  diff(prev: PencilScene | null, next: PencilScene): SceneDelta {
    if (!prev) return { animation: 'none' }
    if (next.matchOver && !prev.matchOver) return { animation: 'settle' }
    if (next.moveCount !== prev.moveCount) return { animation: 'place' }
    return { animation: 'none' }
  },
  draw(ctx, prev, next, t, opts) {
    const layout = pencilCanvasLayout(opts.width, opts.height, next.size)
    const { cell, cx, cy } = layout
    const size = next.size

    ctx.clearRect(0, 0, opts.width, opts.height)
    ctx.fillStyle = COLORS.background
    ctx.fillRect(0, 0, opts.width, opts.height)

    // 棋盘区域与页面卡片区分开，但不引入大面积装饰或额外文字。
    const boardPad = Math.max(5, cell * 0.13)
    ctx.fillStyle = COLORS.board
    ctx.strokeStyle = COLORS.boardBorder
    ctx.lineWidth = Math.max(1, cell * 0.025)
    ctx.beginPath()
    roundedRectPath(
      ctx,
      layout.ox - boardPad,
      layout.oy - boardPad,
      layout.boardPx + boardPad * 2,
      layout.boardPx + boardPad * 2,
      Math.max(8, cell * 0.18),
    )
    ctx.fill()
    ctx.stroke()

    // 格归属填满四条真实边围出的 2×cell 区域；边与点稍后覆盖其上。
    for (let x = 1; x < size; x += 2) {
      for (let y = 1; y < size; y += 2) {
        if (next.grid[x]?.[y] !== GRID_BOX) continue
        const owner = next.boxOwner[x]?.[y]
        if (owner !== 0 && owner !== 1) continue
        const rect = pencilBoxRect(x, y, layout)
        if (!rect) continue
        const wasOwned = prev?.boxOwner[x]?.[y] === owner
        ctx.save()
        ctx.globalAlpha = wasOwned ? 1 : t
        ctx.fillStyle = COLORS.box[owner]
        ctx.fillRect(rect.left, rect.top, rect.width, rect.height)
        ctx.restore()
      }
    }

    // 未占边是清晰可辨的灰色目标；已占边以红/蓝高对比覆盖。
    for (let x = 0; x < size; x++) {
      for (let y = 0; y < size; y++) {
        const value = next.grid[x]?.[y]
        if (value !== GRID_EDGE && value !== GRID_EDGE_USED) continue
        const segment = pencilEdgeSegment(x, y, size, layout)
        if (!segment) continue
        if (value === GRID_EDGE) {
          strokeSegment(ctx, segment, 1, COLORS.emptyEdge, Math.max(2, cell * 0.065))
          continue
        }

        const owner = next.edgeOwner[`${x},${y}`]
        const ownerColor = owner === 0 || owner === 1 ? COLORS.seat[owner] : COLORS.dot
        const isLast = next.lastEdge?.x === x && next.lastEdge?.y === y
        const wasUsed = prev?.grid[x]?.[y] === GRID_EDGE_USED
        const fraction = !wasUsed && t < 1 ? t : 1
        if (isLast) {
          strokeSegment(ctx, segment, fraction, COLORS.last, Math.max(8, cell * 0.25))
        }
        strokeSegment(ctx, segment, fraction, ownerColor, Math.max(5, cell * 0.15))
      }
    }

    // 合法 hover 预览只覆盖未占边，视觉与 pick 返回值完全一致。
    const hover = opts.hoverPick
    if (hover && next.grid[hover.x]?.[hover.y] === GRID_EDGE) {
      const segment = pencilEdgeSegment(hover.x, hover.y, size, layout)
      if (segment) {
        strokeSegment(ctx, segment, 1, COLORS.hoverHalo, Math.max(10, cell * 0.3))
        strokeSegment(ctx, segment, 1, COLORS.hover, Math.max(4, cell * 0.11))
      }
    }

    // 格内只标座位 1/2，不使用 Bot 首字母；同名 Bot 与同首字母用户也不会歧义。
    ctx.font = `700 ${Math.max(13, Math.floor(cell * 0.46))}px "DM Sans", sans-serif`
    ctx.textAlign = 'center'
    ctx.textBaseline = 'middle'
    for (let x = 1; x < size; x += 2) {
      for (let y = 1; y < size; y += 2) {
        const owner = next.boxOwner[x]?.[y]
        if (owner !== 0 && owner !== 1) continue
        const wasOwned = prev?.boxOwner[x]?.[y] === owner
        ctx.save()
        ctx.globalAlpha = wasOwned ? 1 : t
        ctx.fillStyle = COLORS.seat[owner]
        ctx.fillText(String(owner + 1), cx(x), cy(y))
        ctx.restore()
      }
    }

    // 点最后绘制，保持所有边端点清楚且遮住线帽叠色。
    ctx.fillStyle = COLORS.dot
    for (let x = 0; x < size; x += 2) {
      for (let y = 0; y < size; y += 2) {
        if (next.grid[x]?.[y] !== GRID_DOT) continue
        ctx.beginPath()
        ctx.arc(cx(x), cy(y), Math.max(3, cell * 0.105), 0, Math.PI * 2)
        ctx.fill()
      }
    }
  },
  pick(canvasX, canvasY, scene, opts) {
    const pencil = scene as PencilScene
    return pickPencilEdge(canvasX, canvasY, pencil.grid, opts.width, opts.height)
  },
  keyboardPicks(scene) {
    const pencil = scene as PencilScene
    const legal: Array<{ x: number; y: number }> = []
    for (let x = 0; x < pencil.size; x++) {
      for (let y = 0; y < pencil.size; y++) {
        if (pencil.grid[x]?.[y] === GRID_EDGE) legal.push({ x, y })
      }
    }
    return legal
  },
}
