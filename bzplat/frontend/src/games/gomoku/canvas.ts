/** 全国机器博弈竞赛五子棋 canvas：规则阶段、交换棋色、五手候选与禁手均可回放。 */
import type { RawEvent } from '@/games/base'
import { fitText, scaleFactor } from '@/games/base'
import {
  gomokuColorLabel,
  gomokuPhaseLabel,
  isCurrentGomokuCompetitionRuleset,
  isGomokuCompetitionRuleset,
  reduceGomokuEvents,
  type GomokuColor,
  type GomokuPoint,
  type GomokuViewModel,
} from './reducer'
import { gomokuTerminalReason } from './reasons'
import type { GameCanvasRenderer, Scene, SceneDelta } from '@/games/canvas-types'
import { seatDisplay } from '@/games/seat-display'

interface GomokuScene extends Scene, GomokuViewModel {}

const COLORS = {
  board: '#e8c98a',
  boardEdge: '#c99f55',
  grid: '#806116',
  ink: '#4d390f',
  black: '#171717',
  white: '#f7f7f5',
  whiteEdge: '#7a7a73',
  last: '#d97706',
  candidate: '#2563eb',
  selected: '#047857',
  forbidden: '#b91c1c',
  hover: '#0f766e',
}

function samePoint(left: GomokuPoint | null | undefined, right: GomokuPoint | null | undefined) {
  return Boolean(left && right && left.x === right.x && left.y === right.y)
}

function pointKey(point: GomokuPoint) {
  return `${point.x},${point.y}`
}

function isEmpty(scene: GomokuScene, point: GomokuPoint) {
  return scene.board[point.x]?.[point.y] === -1
}

function transformPoint(point: GomokuPoint, index: number, center: number): GomokuPoint {
  const dx = point.x - center
  const dy = point.y - center
  const transformed: Array<[number, number]> = [
    [dx, dy], [-dy, dx], [-dx, -dy], [dy, -dx],
    [-dx, dy], [dx, -dy], [dy, dx], [-dy, -dx],
  ]
  const [x, y] = transformed[index]
  return { x: x + center, y: y + center }
}

function pointSetKey(points: GomokuPoint[]) {
  return points.map(pointKey).sort().join('|')
}

/** 与后端纯裁判相同：只使用保持当前彩色四子盘面不变的 D4 变换。 */
function candidateShapeKey(scene: GomokuScene, target: GomokuPoint): string | null {
  const black: GomokuPoint[] = []
  const white: GomokuPoint[] = []
  for (let x = 0; x < scene.size; x++) {
    for (let y = 0; y < scene.size; y++) {
      if (scene.board[x]?.[y] === 0) black.push({ x, y })
      else if (scene.board[x]?.[y] === 1) white.push({ x, y })
    }
  }
  if (black.length !== 2 || white.length !== 2) return null
  const center = Math.floor(scene.size / 2)
  const blackKey = pointSetKey(black)
  const whiteKey = pointSetKey(white)
  const stable = Array.from({ length: 8 }, (_, index) => index).filter((index) => (
    pointSetKey(black.map((point) => transformPoint(point, index, center))) === blackKey
    && pointSetKey(white.map((point) => transformPoint(point, index, center))) === whiteKey
  ))
  if (stable.length === 0) return null
  return stable
    .map((index) => transformPoint(target, index, center))
    .map((point) => point.x * scene.size + point.y)
    .sort((left, right) => left - right)[0]
    .toString()
}

function interactionPointIsLegal(scene: GomokuScene, target: GomokuPoint): boolean {
  if (!isEmpty(scene, target)) return false
  const interaction = scene.interaction
  if (!interaction) return true
  const picked = interaction.points
  const center = Math.floor(scene.size / 2)
  if (interaction.phase === 'opening_proposal') {
    const fixed = interaction.fixedBlack1 ?? { x: center, y: center }
    if (samePoint(target, fixed)) return false
    if (!picked[0]) {
      return Math.max(Math.abs(target.x - center), Math.abs(target.y - center)) === 1
    }
    if (samePoint(target, picked[0])) return false
    return Math.abs(target.x - center) <= 2 && Math.abs(target.y - center) <= 2
  }
  if (interaction.phase === 'black5_candidates') {
    const alreadyPicked = picked.some((item) => samePoint(item, target))
    if (alreadyPicked) return true
    if (picked.length >= (interaction.n ?? scene.n ?? 2)) return false
    const targetShape = candidateShapeKey(scene, target)
    if (targetShape === null) return false
    return picked.every((point) => candidateShapeKey(scene, point) !== targetShape)
  }
  if (interaction.phase === 'black5_select') {
    return scene.candidates.some((item) => samePoint(item, target))
  }
  if (interaction.phase === 'swap_choice') return false
  return true
}

function interactionColor(scene: GomokuScene): GomokuColor {
  const phase = scene.interaction?.phase
  if (phase === 'opening_proposal') return scene.interaction?.points[0] ? 0 : 1
  if (phase === 'black5_candidates' || phase === 'black5_select') return 0
  if (phase === 'white4') return 1
  return scene.toColor ?? 0
}

/** 2013 竞赛规则的 15×15 棋盘定位点：H8 天元与四个星位。 */
export function gomokuStarPoints(size: number): GomokuPoint[] {
  if (size !== 15) return []
  return [
    { x: 7, y: 7 },
    { x: 3, y: 3 },
    { x: 3, y: 11 },
    { x: 11, y: 11 },
    { x: 11, y: 3 },
  ]
}

function drawStone(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  radius: number,
  stone: GomokuColor,
  alpha = 1,
) {
  ctx.save()
  ctx.globalAlpha = alpha
  ctx.beginPath()
  ctx.arc(x, y, radius, 0, Math.PI * 2)
  ctx.fillStyle = stone === 0 ? COLORS.black : COLORS.white
  ctx.fill()
  ctx.lineWidth = Math.max(1, radius * 0.08)
  ctx.strokeStyle = stone === 0 ? '#000000' : COLORS.whiteEdge
  ctx.stroke()
  ctx.restore()
}

function drawCandidate(
  ctx: CanvasRenderingContext2D,
  centerX: number,
  centerY: number,
  radius: number,
  index: number,
  selected: boolean,
  draft = false,
) {
  ctx.save()
  ctx.beginPath()
  ctx.arc(centerX, centerY, radius, 0, Math.PI * 2)
  ctx.fillStyle = draft ? 'rgba(23,23,23,0.22)' : 'rgba(37,99,235,0.12)'
  ctx.fill()
  ctx.lineWidth = selected ? Math.max(3, radius * 0.18) : Math.max(2, radius * 0.12)
  ctx.strokeStyle = selected ? COLORS.selected : COLORS.candidate
  ctx.setLineDash(selected ? [] : [Math.max(2, radius * 0.28), Math.max(2, radius * 0.2)])
  ctx.stroke()
  ctx.setLineDash([])
  ctx.fillStyle = selected ? COLORS.selected : COLORS.candidate
  ctx.font = `700 ${Math.max(10, Math.floor(radius * 0.9))}px "DM Sans", sans-serif`
  ctx.textAlign = 'center'
  ctx.textBaseline = 'middle'
  ctx.fillText(String(index + 1), centerX, centerY)
  ctx.restore()
}

export const GomokuCanvasRenderer: GameCanvasRenderer<GomokuScene> = {
  toScene(events: RawEvent[]): GomokuScene {
    return reduceGomokuEvents(events) as GomokuScene
  },
  diff(prev: GomokuScene | null, next: GomokuScene): SceneDelta {
    if (!prev) return { animation: 'none' }
    if (next.matchOver && !prev.matchOver) return { animation: 'settle' }
    if (next.forbidden && !prev.forbidden) return { animation: 'settle' }
    if (next.moveCount !== prev.moveCount) return { animation: 'place' }
    if (next.candidates.length !== prev.candidates.length || next.selectedIndex !== prev.selectedIndex) {
      return { animation: 'place' }
    }
    return { animation: 'none' }
  },
  draw(ctx, prev, next, t, opts) {
    const layout = gomokuLayout(opts.width, opts.height, next.size)
    const { cell, cx, cy } = layout
    const W = opts.width
    const H = opts.height
    const size = next.size

    ctx.clearRect(0, 0, W, H)
    ctx.fillStyle = COLORS.board
    ctx.fillRect(0, 0, W, H)
    ctx.fillStyle = COLORS.boardEdge
    ctx.fillRect(0, 0, W, Math.max(3, W * 0.008))

    ctx.strokeStyle = COLORS.grid
    ctx.lineWidth = Math.max(1, W / 900)
    for (let i = 0; i < size; i++) {
      ctx.beginPath(); ctx.moveTo(cx(0), cy(i)); ctx.lineTo(cx(size - 1), cy(i)); ctx.stroke()
      ctx.beginPath(); ctx.moveTo(cx(i), cy(0)); ctx.lineTo(cx(i), cy(size - 1)); ctx.stroke()
    }

    ctx.fillStyle = COLORS.grid
    for (const point of gomokuStarPoints(size)) {
      ctx.beginPath()
      ctx.arc(cx(point.x), cy(point.y), Math.max(2, cell * 0.1), 0, Math.PI * 2)
      ctx.fill()
    }

    for (let x = 0; x < size; x++) {
      for (let y = 0; y < size; y++) {
        const stone = next.board[x]?.[y]
        if (stone !== 0 && stone !== 1) continue
        const isLast = next.lastMove?.x === x && next.lastMove?.y === y
        const wasPresent = prev?.board[x]?.[y] === stone
        const enter = isLast && !wasPresent && t < 1 ? Math.max(0.001, t) : 1
        drawStone(ctx, cx(x), cy(y), cell * 0.43 * enter, stone, isLast && !wasPresent ? t : 1)
        if (isLast && t >= 1) {
          ctx.beginPath()
          ctx.arc(cx(x), cy(y), Math.max(2, cell * 0.11), 0, Math.PI * 2)
          ctx.fillStyle = stone === 0 ? '#fbbf24' : '#dc2626'
          ctx.fill()
        }
      }
    }

    // 权威五手候选始终按事件原值保留为回放标记；被保留点使用绿色实线，其余为蓝色虚线。
    next.candidates.forEach((candidate, index) => {
      drawCandidate(
        ctx,
        cx(candidate.x),
        cy(candidate.y),
        cell * 0.5,
        index,
        index === next.selectedIndex || samePoint(candidate, next.selectedPoint),
      )
    })

    // 人类输入草稿不进入权威棋盘：开局三子用半透明棋子，五手二打用编号环。
    const interaction = next.interaction
    if (interaction?.phase === 'opening_proposal') {
      const fixed = interaction.fixedBlack1 ?? { x: 7, y: 7 }
      drawStone(ctx, cx(fixed.x), cy(fixed.y), cell * 0.43, 0, 0.62)
      interaction.points.forEach((draft, index) => {
        drawStone(ctx, cx(draft.x), cy(draft.y), cell * 0.43, index === 0 ? 1 : 0, 0.62)
        ctx.save()
        ctx.fillStyle = index === 0 ? COLORS.ink : COLORS.white
        ctx.font = `700 ${Math.max(10, Math.floor(cell * 0.46))}px "DM Sans", sans-serif`
        ctx.textAlign = 'center'
        ctx.textBaseline = 'middle'
        ctx.fillText(index === 0 ? '2' : '3', cx(draft.x), cy(draft.y))
        ctx.restore()
      })
    } else if (interaction?.phase === 'black5_candidates') {
      interaction.points.forEach((draft, index) => {
        drawCandidate(ctx, cx(draft.x), cy(draft.y), cell * 0.5, index, false, true)
      })
    }

    const hover = opts.hoverPick
    if (hover && interactionPointIsLegal(next, hover)) {
      ctx.save()
      ctx.beginPath()
      ctx.arc(cx(hover.x), cy(hover.y), cell * 0.51, 0, Math.PI * 2)
      ctx.fillStyle = 'rgba(15,118,110,0.12)'
      ctx.fill()
      ctx.lineWidth = Math.max(2, cell * 0.1)
      ctx.strokeStyle = COLORS.hover
      ctx.stroke()
      ctx.restore()
      if (!interaction?.points.some((item) => samePoint(item, hover))) {
        drawStone(ctx, cx(hover.x), cy(hover.y), cell * 0.39, interactionColor(next), 0.35)
      }
    }

    if (next.forbidden) {
      const fx = cx(next.forbidden.x)
      const fy = cy(next.forbidden.y)
      const radius = cell * 0.58
      ctx.save()
      ctx.strokeStyle = COLORS.forbidden
      ctx.lineWidth = Math.max(3, cell * 0.12)
      ctx.beginPath(); ctx.arc(fx, fy, radius, 0, Math.PI * 2); ctx.stroke()
      ctx.beginPath(); ctx.moveTo(fx - radius * 0.55, fy - radius * 0.55); ctx.lineTo(fx + radius * 0.55, fy + radius * 0.55); ctx.stroke()
      ctx.beginPath(); ctx.moveTo(fx + radius * 0.55, fy - radius * 0.55); ctx.lineTo(fx - radius * 0.55, fy + radius * 0.55); ctx.stroke()
      ctx.restore()
    }

    const s = scaleFactor(W)
    const name0 = seatDisplay(opts.seats?.[0], 0).subject
    const name1 = seatDisplay(opts.seats?.[1], 1).subject
    const ruleLabel = isCurrentGomokuCompetitionRuleset(next.ruleset)
      ? '现行五手二打规则'
      : isGomokuCompetitionRuleset(next.ruleset)
        ? '历史竞赛规则'
        : '旧版自由棋'
    const phaseLabel = gomokuPhaseLabel(next.phase, next.n)
    const turnLabel = next.matchOver
      ? next.winner === null
        ? '平局'
        : `${next.winner === 0 ? name0 : name1}胜${next.reason ? ` · ${gomokuTerminalReason(next.reason, 'completed').label}` : ''}`
      : next.toAct === 0 || next.toAct === 1
        ? `${next.toAct === 0 ? name0 : name1} · ${gomokuColorLabel(next.toColor)}`
        : '等待裁判'
    ctx.fillStyle = COLORS.ink
    ctx.textAlign = 'left'
    ctx.textBaseline = 'alphabetic'
    ctx.font = `700 ${Math.max(11, Math.round(15 * s))}px "DM Sans", sans-serif`
    ctx.fillText(
      fitText(ctx, `${ruleLabel} · ${phaseLabel} · 第 ${next.moveCount} 手 · ${turnLabel}`, W - Math.max(20, 24 * s)),
      Math.max(10, 12 * s),
      Math.max(18, 24 * s),
    )
    ctx.font = `${Math.max(10, Math.round(12 * s))}px "DM Sans", sans-serif`
    const seat0 = `${name0}（${gomokuColorLabel(next.seatColors[0])}）`
    const seat1 = `${name1}（${gomokuColorLabel(next.seatColors[1])}）`
    ctx.fillText(
      fitText(ctx, `座位 1 ${seat0}  ·  座位 2 ${seat1}`, W - Math.max(20, 24 * s)),
      Math.max(10, 12 * s),
      Math.max(34, 42 * s),
    )
  },
  pick(canvasX, canvasY, scene, opts) {
    const gomoku = scene as GomokuScene
    const { cell, ox, oy } = gomokuLayout(opts.width, opts.height, gomoku.size)
    const target = {
      x: Math.round((canvasX - ox) / cell),
      y: Math.round((canvasY - oy) / cell),
    }
    if (target.x < 0 || target.y < 0 || target.x >= gomoku.size || target.y >= gomoku.size) return null
    return interactionPointIsLegal(gomoku, target) ? target : null
  },
  keyboardPicks(scene) {
    const gomoku = scene as GomokuScene
    const legal: GomokuPoint[] = []
    for (let y = 0; y < gomoku.size; y++) {
      for (let x = 0; x < gomoku.size; x++) {
        const target = { x, y }
        if (interactionPointIsLegal(gomoku, target)) legal.push(target)
      }
    }
    return legal
  },
}

/** draw/pick 共用布局；顶部保留两行密集状态，方形棋盘在 390px 下仍尽量放大。 */
function gomokuLayout(W: number, H: number, size: number) {
  const header = Math.max(48, W * 0.065)
  const gutter = Math.max(18, W * 0.035)
  const cell = Math.max(
    8,
    Math.min((W - gutter * 2) / (size - 1), (H - header - gutter) / (size - 1)),
  )
  const boardPx = cell * (size - 1)
  const ox = (W - boardPx) / 2
  const oy = header + Math.max(0, (H - header - gutter - boardPx) / 2)
  const cx = (x: number) => ox + x * cell
  const cy = (y: number) => oy + y * cell
  return { cell, ox, oy, cx, cy }
}

export function gomokuCandidateKey(point: GomokuPoint) {
  return pointKey(point)
}
