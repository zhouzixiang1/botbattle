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
import type { RawEvent } from '@/games/base'
import { fitText } from '@/games/base'
import type { SeatState } from './reducer'
import { reduceHoldemEvents } from './reducer'
import type { GameCanvasRenderer, Scene, SceneDelta, SeatInfo } from '@/games/canvas-types'
import { ensurePokerJS } from '@/lib/pokerjs'

// 牌桌布局比例（相对设计基线宽 W0=900）。桌面使用紧凑 16:9，
// 棋类仍保留通用 3:2；纵向半径同时受实际高度约束，避免裁切。
const W0 = 900
const L_RATIO = 0.22        // 座位/牌堆横向间距系数
const R_RATIO = 0.28        // 3:2 下的最大椭圆半轴系数
const CARD_RATIO = 100 / W0 // 牌尺寸系数
const POINT = ['2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K', 'A']
const SUIT_BY_CODE: Record<string, 'h' | 'd' | 's' | 'c'> = { h: 'h', d: 'd', s: 's', c: 'c' }

/** 按当前位图宽 W 计算布局尺寸（W 跟随父容器响应式变化）。 */
const layout = (W: number, H: number) => ({
  L: L_RATIO * W,
  R: Math.min(R_RATIO * W, H * 0.42),
  CARD_SIZE: Math.max(CARD_RATIO * W, W < 520 ? 42 : 0),
  /** 缩放因子：W/基线宽，用于把固定像素的 fitText maxWidth 等比放大。 */
  s: W / W0,
})

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
  /** 当前手结算赢家（lastSettle）；对局总胜者用 matchWinner */
  winners: number[] | null
  /** 当前手 settle.deltas（每手叠层用） */
  handDeltas: [number, number] | null
  handSettleReason: string | null
  matchOver: boolean
  /** 整场胜者座位 0/1；平局 null（match_end / final_chips 推导） */
  matchWinner: number | null
  /** 累计净筹码（各手 settle.deltas 累加，对应旧 PokerTable「累计」） */
  nets: [number, number]
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
    // reduceHoldemEvents 返回 { hand, sbSeat, street, board, pot, seats, toAct, lastSettle, matchOver, matchWinner, ... }
    // seats[i] = SeatState { hole:(string|null)[], chips, bet, folded, allin, isWinner, net, lastAction }
    const vm = reduceHoldemEvents(events)
    const seats = vm.seats ?? []
    // 历史公开回放读取边界若没有 winner，则用逐手累计 net 兜底；
    // 新写 replay/live 的 canonical deltas 已由 reducer 写入同一 net 字段。
    let matchWinner = vm.matchWinner
    if (vm.matchOver && matchWinner === null) {
      const n0 = seats[0]?.net ?? 0
      const n1 = seats[1]?.net ?? 0
      if (n0 > n1) matchWinner = 0
      else if (n1 > n0) matchWinner = 1
    }
    return {
      hand: vm.hand ?? 0,
      chips: [seats[0]?.chips ?? 20000, seats[1]?.chips ?? 20000],
      pot: vm.pot ?? 0,
      holes: seats.map((s: SeatState) => (s?.hole?.[0] ? [s.hole[0], s.hole[1]].filter(Boolean) as string[] : null)),
      board: vm.board ?? [],
      street: vm.street ?? 'preflop',
      toAct: vm.toAct ?? null,
      lastAction: (() => {
        // 注意：不能直接遍历 seats 取「第一个 lastAction 非空」的座 —— reducer 只在
        // acting 座上写 lastAction 但从不清除对手座（仅 hand_start/freshSeats 复位），
        // 故同街两人都行动后两座都会带 lastAction，正向遍历恒为座 0（陈旧）。
        // 改从事件流取最近一条 action 事件确定「最近行动者」，再用其 VM 座上权威的
        // lastAction（reducer 已按 call=投入增量 / raise=allin=本街累计做归一化）。
        let lastPlayer: number | null = null
        for (let i = events.length - 1; i >= 0; i--) {
          if (events[i]?.type === 'action') {
            lastPlayer = Number(events[i].player ?? 0)
            break
          }
        }
        if (lastPlayer === null) return null
        const la = seats[lastPlayer]?.lastAction
        if (!la) return null
        return { player: lastPlayer, action: la.action, amount: la.amount }
      })(),
      roundBets: seats.map((s: SeatState) => s?.bet ?? 0),
      winners: vm.lastSettle?.winners ?? null,
      handDeltas: vm.lastSettle
        ? [Number(vm.lastSettle.deltas[0] ?? 0), Number(vm.lastSettle.deltas[1] ?? 0)]
        : null,
      handSettleReason: vm.lastSettle?.reason ?? null,
      matchOver: !!vm.matchOver,
      matchWinner,
      nets: [seats[0]?.net ?? 0, seats[1]?.net ?? 0],
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
    const { L, R, CARD_SIZE, s } = layout(W, H)
    const X = (k: number) => W / 2 + L * k
    // 座位纵向位置：0.55R 偏移（原 0.67R）让座位块整体内移，
    // 配合 drawSeat 收紧的文字行间距，确保「筹码/累计/下注」落在椭圆底沿内。
    const Y0 = H / 2 - R * 0.55, Y1 = H / 2 + R * 0.55

    // 清屏 + 椭圆桌（照搬 drawBackground）
    ctx.clearRect(0, 0, W, H)
    ctx.beginPath()
    ctx.ellipse(W / 2 - L, H / 2, R, R, 0, Math.PI / 2, Math.PI * 3 / 2)
    ctx.ellipse(W / 2 + L, H / 2, R, R, 0, -Math.PI / 2, Math.PI / 2)
    ctx.closePath()
    // 深绿桌（比 botzone 'green' 略深，更耐看）
    ctx.fillStyle = '#0f5132'
    ctx.fill()

    // 牌桌顶栏：把手数/阶段/底池放到椭圆上方，不再挤在左侧狭窄区域。
    // 旧布局的 `轮: preflop` 只有约 58px 可用，会稳定被截成 `轮: pr…`。
    const pot = prev && prev.pot !== next.pot ? Math.round(prev.pot + (next.pot - prev.pot) * t) : next.pot
    ctx.font = `bold ${Math.max(11, Math.round(15 * s))}px "DM Sans", sans-serif`
    ctx.fillStyle = '#fff'
    ctx.textAlign = 'center'
    const streetLabels: Record<string, string> = {
      preflop: '翻牌前', flop: '翻牌', turn: '转牌', river: '河牌', showdown: '摊牌',
    }
    const tableStatus = `第 ${(next.hand || 0) + 1} 手 · ${streetLabels[next.street] ?? next.street} · 底池 ${pot.toLocaleString('en-US')}`
    if (!next.matchOver) {
      ctx.fillText(fitText(ctx, tableStatus, W * 0.72), W / 2, Math.max(14, H / 2 - R - 12 * s))
    }

    // 座位（上=座1, 下=座0）
    drawSeat(ctx, X(-0.75), Y0, 1, next, prev, t, opts.seats, s)
    drawSeat(ctx, X(-0.75), Y1, 0, next, prev, t, opts.seats, s)

    // 手牌：showdown 模式隐藏非人类/非摊牌对手牌
    const reveal = opts.revealMode ?? 'all'
    const showdownOpen =
      next.matchOver ||
      (next.handSettleReason === 'showdown' && !!next.winners)
    const holeFor = (idx: number): string[] | null => {
      const raw = next.holes[idx] ?? null
      if (!raw?.length) return null
      if (reveal === 'all' || showdownOpen) return raw
      // showdown：仅人类己方亮牌；其它画牌背
      if (opts.seats?.[idx]?.isHuman) return raw
      return ['back', 'back'] // 哨兵：drawCards 画牌背
    }
    const prevHoleFor = (idx: number): string[] | null => {
      const raw = prev?.holes[idx] ?? null
      if (!raw?.length) return null
      if (reveal === 'all' || showdownOpen) return raw
      if (opts.seats?.[idx]?.isHuman) return raw
      return ['back', 'back']
    }
    drawCards(ctx, X(0), Y0, holeFor(1), t, prevHoleFor(1), CARD_SIZE)
    drawCards(ctx, X(0), Y1, holeFor(0), t, prevHoleFor(0), CARD_SIZE)

    // 公共牌（5 槽，新发的翻面）
    const board = [...next.board]
    while (board.length < 5) board.push('')
    const prevBoard = prev ? [...(prev.board || [])] : []
    while (prevBoard.length < 5) prevBoard.push('')
    drawCommunity(ctx, X(0), H / 2, board, prevBoard, t, CARD_SIZE)

    // 动作浮字（黄字+阴影+上浮淡出）
    if (next.lastAction && (!prev || JSON.stringify(prev.lastAction) !== JSON.stringify(next.lastAction))) {
      drawActionFloat(ctx, X(0.75), next.lastAction.player === 0 ? Y1 : Y0, next.lastAction, t, s)
    }

    // 每手结算叠层（非整场结束时）
    if (!next.matchOver && next.handDeltas && next.winners && t > 0.15) {
      ctx.save()
      ctx.textAlign = 'center'
      ctx.font = `bold ${Math.round(18 * s)}px "DM Sans", sans-serif`
      ctx.shadowColor = 'black'
      ctx.shadowBlur = 8
      for (const idx of [0, 1] as const) {
        const d = next.handDeltas[idx]
        const yy = idx === 0 ? Y1 - 70 * s : Y0 - 70 * s
        const txt = d > 0 ? `赢得 ${d.toLocaleString('en-US')}` : d < 0 ? `输掉 ${(-d).toLocaleString('en-US')}` : '不赚不亏'
        ctx.fillStyle = d > 0 ? 'rgba(52,211,153,0.95)' : d < 0 ? 'rgba(248,113,113,0.95)' : 'rgba(255,255,255,0.85)'
        ctx.globalAlpha = Math.min(1, t * 1.2)
        ctx.fillText(fitText(ctx, txt, 180 * s), X(0.75), yy)
      }
      ctx.restore()
    }

    // 结算覆盖：对局结束 + 胜者（优先 BOT 名）
    if (next.matchOver) {
      ctx.save()
      ctx.textAlign = 'center'
      ctx.font = `bold ${Math.max(11, Math.round(17 * s))}px "DM Sans", sans-serif`
      ctx.fillStyle = 'rgba(255,238,88,0.95)'
      ctx.shadowColor = 'black'
      ctx.shadowBlur = 12
      let winnerTxt = '平局'
      if (next.matchWinner === 0 || next.matchWinner === 1) {
        winnerTxt = `胜者：${seatDisplayName(opts.seats?.[next.matchWinner], next.matchWinner)}`
      }
      ctx.fillText(
        fitText(ctx, `对局结束 · ${winnerTxt}`, W * 0.72),
        X(0),
        Math.max(14, H / 2 - R - 12 * s),
      )
      ctx.restore()
    }
  },
}

/** 座位显示名：BOT 名优先，否则 @用户名，再回退「座位 n」。 */
function seatDisplayName(info: SeatInfo | undefined, idx: number): string {
  const bot = (info?.botName || '').trim()
  if (bot) return bot
  const owner = (info?.ownerName || '').trim()
  if (owner) return info?.isHuman ? `${owner}（人类）` : owner
  // 显示从 1 起计（后端 0 起计，DB CHECK 约束未变）。
  return `座位 ${idx + 1}`
}

function drawSeat(
  ctx: CanvasRenderingContext2D,
  x: number, y: number, idx: number,
  next: HoldemScene, prev: HoldemScene | null, t: number,
  seats: SeatInfo[] | undefined,
  s: number, // 缩放因子 W/W0
) {
  const info = seats?.[idx]
  const chips = prev && prev.chips[idx] !== next.chips[idx]
    ? Math.round(prev.chips[idx] + (next.chips[idx] - prev.chips[idx]) * t)
    : next.chips[idx]
  const net = prev && prev.nets[idx] !== next.nets[idx]
    ? Math.round(prev.nets[idx] + (next.nets[idx] - prev.nets[idx]) * t)
    : next.nets[idx]
  const isToAct = next.toAct === idx && !next.matchOver
  const isMatchWinner = next.matchOver && next.matchWinner === idx
  const name = seatDisplayName(info, idx)
  const compact = s < 0.58
  // 座位块横向半宽（用于截断名字/数值，避免长文本越出椭圆桌或与公共牌重叠）
  const seatW = 130 * s
  ctx.textAlign = 'center'
  if (compact) {
    // 小屏不再把桌面版五行文字同比缩到 5px。身份/归属已由上方 DOM
    // 座位卡完整呈现，牌桌保留名字、筹码和累计三项关键状态。
    const compactW = 82
    ctx.fillStyle = isMatchWinner ? 'rgba(255,238,88,0.98)' : '#fff'
    ctx.font = 'bold 10px "DM Sans", sans-serif'
    ctx.fillText(fitText(ctx, name, compactW), x, y - 10)
    ctx.fillStyle = '#fff'
    ctx.font = 'bold 9px "DM Sans", sans-serif'
    ctx.fillText(fitText(ctx, `筹码 ${chips.toLocaleString('en-US')}`, compactW), x, y + 4)
    ctx.fillStyle = net > 0 ? '#34d399' : net < 0 ? '#f87171' : 'rgba(255,255,255,0.8)'
    ctx.fillText(fitText(ctx, `累计 ${net >= 0 ? '+' : ''}${net.toLocaleString('en-US')}`, compactW), x, y + 17)
    if (isToAct) {
      ctx.fillStyle = 'rgba(255,238,88,0.98)'
      ctx.fillText('行动中', x, y + 30)
    }
    return
  }
  // 头像圆（首字母）—— 圆心/半径随 s 缩放
  const ax = x - 25 * s, ay = y - 45 * s + 25 * s
  const initial = (name[0] || '?').toUpperCase()
  ctx.beginPath(); ctx.arc(ax, ay, 18 * s, 0, Math.PI * 2)
  ctx.fillStyle = idx === 0 ? '#3b82f6' : '#ef4444'; ctx.fill()
  if (isMatchWinner) {
    ctx.strokeStyle = 'rgba(255,238,88,0.95)'
    ctx.lineWidth = 3 * s
    ctx.stroke()
  }
  ctx.fillStyle = '#fff'; ctx.font = `bold ${Math.round(16 * s)}px "DM Sans"`
  ctx.fillText(initial, ax, ay + 5 * s)
  // 名字（两行：BOT名 + @用户名）—— 测量后按座位宽度截断，防止长名越出牌桌
  ctx.fillStyle = isMatchWinner ? 'rgba(255,238,88,0.98)' : '#fff'
  ctx.font = `bold ${Math.round(13 * s)}px "DM Sans"`
  if (isToAct) ctx.fillText('👉', x - 45 * s, y - 12 * s)
  ctx.textAlign = 'center'
  ctx.fillText(fitText(ctx, name, seatW), x, y + 16 * s)
  ctx.fillStyle = 'rgba(255,255,255,0.7)'; ctx.font = `${Math.round(11 * s)}px "DM Sans"`
  const ownerLine = info?.isHuman
    ? `@${info?.ownerName || '人类'}（你）`
    : info?.ownerName
      ? `@${info.ownerName}`
      // 显示从 1 起计（后端 0 起计，DB CHECK 约束未变）。
      : `座位 ${idx + 1}`
  ctx.fillText(fitText(ctx, ownerLine, seatW), x, y + 30 * s)
  // 本轮剩余筹码 + 累计净筹码（旧 PokerTable 底部「累计」搬到座位旁）。
  // fitText 保护：大数字（如 20000）按座位宽度截断，防溢出。
  // 行距收紧（16/30/44/58/72，原 18/34/50/66/82）：配合座位 Y=H/2±0.55R，确保末行（下注 72）落在椭圆内。
  ctx.fillStyle = '#fff'; ctx.font = `bold ${Math.round(13 * s)}px "DM Sans"`
  ctx.fillText(fitText(ctx, `筹码 ${chips.toLocaleString('en-US')}`, seatW), x, y + 44 * s)
  ctx.fillStyle = net > 0 ? '#34d399' : net < 0 ? '#f87171' : 'rgba(255,255,255,0.75)'
  ctx.font = `${Math.round(12 * s)}px "DM Sans"`
  ctx.fillText(fitText(ctx, `累计 ${net >= 0 ? '+' : ''}${net.toLocaleString('en-US')}`, seatW), x, y + 58 * s)
  // 本轮下注 / 弃牌 / 全押
  const bet = next.roundBets[idx] ?? 0
  if (bet > 0 && !next.folded[idx]) {
    ctx.fillStyle = 'rgba(253,224,71,0.95)'
    ctx.font = `${Math.round(11 * s)}px "DM Sans"`
    ctx.fillText(fitText(ctx, `下注 ${bet.toLocaleString('en-US')}`, seatW), x, y + 72 * s)
  }
  if (next.folded[idx]) {
    ctx.fillStyle = 'rgba(248,113,113,0.95)'
    ctx.font = `bold ${Math.round(12 * s)}px "DM Sans"`
    ctx.fillText('已弃牌', x, y + 82 * s)
  } else if (next.allin[idx]) {
    ctx.fillStyle = 'rgba(251,191,36,0.95)'
    ctx.font = `bold ${Math.round(12 * s)}px "DM Sans"`
    ctx.fillText('ALL-IN', x, y + 82 * s)
  }
}

function drawCards(
  ctx: CanvasRenderingContext2D,
  x: number, y: number,
  cards: string[] | null, t: number, prevCards: string[] | null,
  CARD_SIZE: number,
) {
  if (!cards || !cards.length) return
  const d = (CARD_SIZE * 3.3) / 4
  const x0 = x - (d / 2) * (cards.length - 1) - (CARD_SIZE * 3) / 8
  for (let i = 0; i < cards.length; i++) {
    const code = cards[i]
    const forceBack = !code || code === 'back'
    const isNew = !prevCards || !prevCards[i] || prevCards[i] === 'back'
    const ct = isNew && !forceBack ? t : 1
    ctx.save()
    ctx.translate(x0 + i * d, y - CARD_SIZE / 2)
    if (isNew && ct < 1 && !forceBack) {
      ctx.translate(d * (0.5 - Math.abs(ct - 0.5)), 0)
      ctx.scale(2 * Math.abs(ct - 0.5), 1)
    }
    const parsed = forceBack ? null : parseCardCode(code)
    if (!parsed || (isNew && ct < 0.5) || forceBack) {
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
  CARD_SIZE: number,
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
  s: number, // 缩放因子 W/W0
) {
  const ty = Math.pow(t, 4)
  const txt = ACTION_TEXT[action.action] ?? action.action
  ctx.save()
  ctx.textAlign = 'center'
  ctx.font = `bold ${Math.round(22 * s)}px "DM Sans"`
  ctx.fillStyle = `rgba(255,238,88,${1 - ty})`
  ctx.shadowColor = 'black'; ctx.shadowBlur = 10 * s
  const actionTxt = action.amount ? `${txt} ${action.amount.toLocaleString('en-US')}` : txt
  ctx.fillText(fitText(ctx, actionTxt, 180 * s), x, y - 20 * s - 10 * s * ty)
  ctx.restore()
}
