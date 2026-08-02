/**
 * Holdem Canvas 渲染器（canvas+GSAP 视觉重写 Task 4）。
 *
 * 复刻 botzone.org.cn `TexasHoldem2p.html` 的牌桌视觉：
 * - 发牌翻面动画（scale(|2t-1|,1) 模拟绕轴翻转）
 * - 动作浮字（黄字 + 阴影 + 上浮淡出）
 * - 筹码/底池随插值 t 平滑过渡
 * - 公共牌 5 槽增量发牌
 *
 * 色值照搬 Global Constraints（绿桌 #0f5132 / 红牌 #a22 / 黑牌 #000 / 动作字 rgba(255,238,88,α)），
 * 不走 semantic token（canvas 内部为复刻 botzone 固定色板）。
 *
 * 消费：useMatchState reducer（VM 字段名以该文件为准）、Poker.JS（Task 1 注入 ctx.drawPokerCard/drawPokerBack）、
 * GameCanvasRenderer 接口（Task 2）。
 */
import type { RawEvent, SeatState } from '@/components/poker/useMatchState'
import { reduceEvents } from '@/components/poker/useMatchState'
import type { GameCanvasRenderer, Scene, SceneDelta, SeatInfo } from '@/games/canvas-types'
import { ensurePokerJS } from '@/lib/pokerjs'

// botzone 布局常量（照搬）
const R = 190, L = 230, CARD_SIZE = 100
const POINT = ['2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K', 'A']
const SUIT_BY_CODE: Record<string, 'h' | 'd' | 's' | 'c'> = { h: 'h', d: 'd', s: 's', c: 'c' }

interface HoldemScene extends Scene {
  hand: number
  chips: [number, number]
  pot: number
  holes: (string[] | null)[]   // 每座手牌 [[card,card],...]
  board: string[]              // 公共牌
  street: string
  toAct: number | null
  lastAction: { player: number; action: string; amount?: number } | null
  roundBets: number[]          // 每座本轮已下注
  winners: number[] | null
  matchOver: boolean
  folded: boolean[]
  allin: boolean[]
}

/** 协议卡串（如 "Td"/"7s"）→ Poker.JS 可绘制的 {suit, point}。T→10。 */
function parseCardCode(card: string): { suit: 'h' | 'd' | 's' | 'c'; point: string } | null {
  if (!card || card.length < 2) return null
  // 协议 T → 显示 10
  const rankCh = card[0].toUpperCase().replace('T', '10')
  const suit = SUIT_BY_CODE[card[1].toLowerCase()]
  if (!suit) return null
  const point = POINT.includes(rankCh) ? rankCh : null
  if (!point) return null
  return { suit, point }
}

export const PokerCanvasRenderer: GameCanvasRenderer<HoldemScene> = {
  toScene(events: RawEvent[]): HoldemScene {
    // reduceEvents 返回 { hand, sbSeat, street, board, pot, seats, toAct, lastSettle, matchOver, ... }
    // seats[i] = SeatState { hole:(string|null)[], chips, bet, folded, allin, isWinner, net, lastAction }
    const vm = reduceEvents(events)
    const seats = vm.seats ?? []
    return {
      hand: vm.hand ?? 0,
      chips: [seats[0]?.chips ?? 20000, seats[1]?.chips ?? 20000],
      pot: vm.pot ?? 0,
      holes: seats.map((s: SeatState) => (s?.hole?.[0] ? [s.hole[0], s.hole[1]].filter(Boolean) as string[] : null)),
      board: vm.board ?? [],
      street: vm.street ?? 'preflop',
      toAct: vm.toAct ?? null,
      lastAction: (() => {
        // 取最后一个有 lastAction 的座位（当前行动方）
        for (let i = 0; i < seats.length; i++) {
          const la = seats[i]?.lastAction
          if (la) return { player: i, action: la.action, amount: la.amount }
        }
        return null
      })(),
      roundBets: seats.map((s: SeatState) => s?.bet ?? 0),
      winners: vm.lastSettle?.winners ?? null,
      matchOver: !!vm.matchOver,
      folded: seats.map((s: SeatState) => !!s?.folded),
      allin: seats.map((s: SeatState) => !!s?.allin),
    }
  },
  diff(prev: HoldemScene | null, next: HoldemScene): SceneDelta {
    if (!prev) return { animation: 'deal' }
    if (next.matchOver && !prev.matchOver) return { animation: 'settle' }
    if (next.hand !== prev.hand) return { animation: 'settle' }
    const newCards = (next.board.length > (prev.board?.length ?? 0)) ||
      next.holes.some((h, i) => (h?.length ?? 0) > ((prev.holes[i]?.length) ?? 0))
    if (newCards) return { animation: 'deal' }
    if (JSON.stringify(next.lastAction) !== JSON.stringify(prev.lastAction)) return { animation: 'place' }
    return { animation: 'none' }
  },
  draw(ctx, prev, next, t, opts) {
    ensurePokerJS(ctx)
    const W = opts.width, H = opts.height
    const X = (s: number) => W / 2 + L * s
    const Y0 = H / 2 - R * 0.67, Y1 = H / 2 + R * 0.67

    // 清屏 + 椭圆桌（照搬 drawBackground）
    ctx.clearRect(0, 0, W, H)
    ctx.beginPath()
    ctx.ellipse(W / 2 - L, H / 2, R, R, 0, Math.PI / 2, Math.PI * 3 / 2)
    ctx.ellipse(W / 2 + L, H / 2, R, R, 0, -Math.PI / 2, Math.PI / 2)
    ctx.closePath()
    // 深绿桌（比 botzone 'green' 略深，更耐看）
    ctx.fillStyle = '#0f5132'
    ctx.fill()

    // 左侧信息：手数/轮/底池（随 t 插值底池）
    const pot = prev && prev.pot !== next.pot ? Math.round(prev.pot + (next.pot - prev.pot) * t) : next.pot
    ctx.font = 'bold 16px "DM Sans", sans-serif'
    ctx.fillStyle = '#fff'
    ctx.textAlign = 'left'
    ctx.fillText(`第 ${(next.hand || 0) + 1} 手`, X(-1.4), H / 2 - 35)
    ctx.fillText(`轮: ${next.street}`, X(-1.4), H / 2)
    ctx.fillText(`底池: ${pot}`, X(-1.4), H / 2 + 35)

    // 座位（上=座1, 下=座0）
    drawSeat(ctx, X(-0.75), Y0, 1, next, prev, t, opts.seats)
    drawSeat(ctx, X(-0.75), Y1, 0, next, prev, t, opts.seats)

    // 手牌（翻面动画：新牌 scale(2|t-0.5|,1)）
    drawCards(ctx, X(0), Y0, next.holes[1] ?? null, t, prev?.holes[1] ?? null)
    drawCards(ctx, X(0), Y1, next.holes[0] ?? null, t, prev?.holes[0] ?? null)

    // 公共牌（5 槽，新发的翻面）
    const board = [...next.board]
    while (board.length < 5) board.push('')
    const prevBoard = prev ? [...(prev.board || [])] : []
    while (prevBoard.length < 5) prevBoard.push('')
    drawCommunity(ctx, X(0), H / 2, board, prevBoard, t)

    // 动作浮字（黄字+阴影+上浮淡出）
    if (next.lastAction && (!prev || JSON.stringify(prev.lastAction) !== JSON.stringify(next.lastAction))) {
      drawActionFloat(ctx, X(0.75), next.lastAction.player === 0 ? Y1 : Y0, next.lastAction, t)
    }

    // 结算覆盖（简要高亮赢家座）
    if (next.matchOver && next.winners && next.winners.length) {
      ctx.save()
      ctx.textAlign = 'center'
      ctx.font = 'bold 28px "DM Sans", sans-serif'
      ctx.fillStyle = 'rgba(255,238,88,0.95)'
      ctx.shadowColor = 'black'
      ctx.shadowBlur = 12
      ctx.fillText('对局结束', X(0), H / 2 - R - 20)
      ctx.restore()
    }
  },
}

function drawSeat(
  ctx: CanvasRenderingContext2D,
  x: number, y: number, idx: number,
  next: HoldemScene, prev: HoldemScene | null, t: number,
  seats?: SeatInfo[],
) {
  const info = seats?.[idx]
  const chips = prev && prev.chips[idx] !== next.chips[idx]
    ? Math.round(prev.chips[idx] + (next.chips[idx] - prev.chips[idx]) * t)
    : next.chips[idx]
  const isToAct = next.toAct === idx && !next.matchOver
  ctx.textAlign = 'center'
  // 头像圆（首字母）
  const initial = info?.botName?.[0] ?? info?.ownerName?.[0] ?? '?'
  ctx.beginPath(); ctx.arc(x - 25, y - 45 + 25, 18, 0, Math.PI * 2)
  ctx.fillStyle = idx === 0 ? '#3b82f6' : '#ef4444'; ctx.fill()
  ctx.fillStyle = '#fff'; ctx.font = 'bold 16px "DM Sans"'
  ctx.fillText(initial, x - 25, y - 45 + 25 + 5)
  // 名字（两行：BOT名 + @用户名）
  ctx.fillStyle = '#fff'; ctx.font = '13px "DM Sans"'
  if (isToAct) ctx.fillText('👉', x - 45, y - 12)
  ctx.fillText(info?.botName ?? `座位 ${idx}`, x, y + 20)
  ctx.fillStyle = 'rgba(255,255,255,0.6)'; ctx.font = '11px "DM Sans"'
  ctx.fillText(info?.isHuman ? `@${info?.ownerName} (你)` : `@${info?.ownerName ?? ''}`, x, y + 36)
  // 筹码
  ctx.fillStyle = '#fff'; ctx.font = 'bold 13px "DM Sans"'
  ctx.fillText(`筹码 ${chips}`, x, y + 52)
}

function drawCards(
  ctx: CanvasRenderingContext2D,
  x: number, y: number,
  cards: string[] | null, t: number, prevCards: string[] | null,
) {
  if (!cards || !cards.length) return
  const d = (CARD_SIZE * 3.3) / 4
  const x0 = x - (d / 2) * (cards.length - 1) - (CARD_SIZE * 3) / 8
  for (let i = 0; i < cards.length; i++) {
    const isNew = !prevCards || !prevCards[i]
    const ct = isNew ? t : 1
    ctx.save()
    ctx.translate(x0 + i * d, y - CARD_SIZE / 2)
    if (isNew && ct < 1) {
      ctx.translate(d * (0.5 - Math.abs(ct - 0.5)), 0)
      ctx.scale(2 * Math.abs(ct - 0.5), 1)
    }
    const parsed = parseCardCode(cards[i])
    if (!parsed || (isNew && ct < 0.5)) {
      ctx.drawPokerBack(0, 0, CARD_SIZE)
    } else {
      ctx.drawPokerCard(0, 0, CARD_SIZE, parsed.suit, parsed.point)
    }
    ctx.restore()
  }
}

function drawCommunity(
  ctx: CanvasRenderingContext2D,
  x: number, y: number,
  board: string[], prevBoard: string[], t: number,
) {
  const d = (CARD_SIZE * 3.3) / 4
  const x0 = x - (d / 2) * (board.length - 1) - (CARD_SIZE * 3) / 8
  for (let i = 0; i < board.length; i++) {
    const isNew = !!board[i] && !prevBoard[i]
    const ct = isNew ? Math.min((t * 1.5) / 0.85, 1) : 1
    if (!board[i]) continue
    ctx.save()
    ctx.translate(x0 + i * d, y - CARD_SIZE / 2)
    if (isNew && ct < 1) {
      ctx.translate(d * (0.5 - Math.abs(ct - 0.5)), 0)
      ctx.scale(2 * Math.abs(ct - 0.5), 1)
    }
    const parsed = parseCardCode(board[i])
    if (!parsed || (isNew && ct < 0.5)) {
      ctx.drawPokerBack(0, 0, CARD_SIZE)
    } else {
      ctx.drawPokerCard(0, 0, CARD_SIZE, parsed.suit, parsed.point)
    }
    ctx.restore()
  }
}

const ACTION_TEXT: Record<string, string> = {
  fold: '弃牌 Fold', check: '过牌 Check', call: '跟注 Call', raise: '加注 Raise', allin: '全押 AllIn',
}
function drawActionFloat(
  ctx: CanvasRenderingContext2D,
  x: number, y: number,
  action: { action: string; amount?: number }, t: number,
) {
  const ty = Math.pow(t, 4)
  const txt = ACTION_TEXT[action.action] ?? action.action
  ctx.save()
  ctx.textAlign = 'center'
  ctx.font = 'bold 22px "DM Sans"'
  ctx.fillStyle = `rgba(255,238,88,${1 - ty})`
  ctx.shadowColor = 'black'; ctx.shadowBlur = 10
  ctx.fillText(action.amount ? `${txt} ${action.amount}` : txt, x, y - 20 - 10 * ty)
  ctx.restore()
}
