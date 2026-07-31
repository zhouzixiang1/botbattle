import { CardRow } from './PlayingCard'
import { type MatchViewModel, type SeatState } from './useMatchState'

/* ── 扑克桌可视化（单挑，上下两座位） ───────────────────────── */

const STREET_LABEL: Record<string, string> = {
  preflop: '翻前',
  flop: '翻牌',
  turn: '转牌',
  river: '河牌',
  showdown: '摊牌',
}

const ACTION_LABEL: Record<string, string> = {
  fold: '弃牌',
  check: '过牌',
  call: '跟注',
  raise: '加注',
  allin: '全押',
}

function fmt(n: number): string {
  return n.toLocaleString('en-US')
}

function seatLabel(idx: number, sbSeat: number): string {
  return idx === sbSeat ? 'SB / 按钮' : 'BB'
}

function SeatBox({
  seat,
  idx,
  sbSeat,
  isTop,
  revealHole,
  matchOver,
}: {
  seat: SeatState
  idx: number
  sbSeat: number
  isTop: boolean
  revealHole: boolean
  matchOver: boolean
}) {
  const acting = !matchOver && !seat.folded && !seat.allin
  const dim = seat.folded ? 'opacity-40' : ''
  const winnerRing = seat.isWinner ? 'ring-2 ring-amber-300' : ''

  return (
    <div
      className={`flex items-center gap-3 rounded-xl border border-white/10 bg-slate-950/70 px-3 py-2 backdrop-blur-sm ${dim} ${winnerRing}`}
    >
      <div className="flex flex-col">
        <span className="text-xs text-slate-400">座位 {idx}</span>
        <span className="text-[10px] text-brand-300">{seatLabel(idx, sbSeat)}</span>
      </div>

      {/* 手牌 */}
      <CardRow
        cards={seat.hole}
        count={2}
        size="sm"
        hidden={!revealHole}
        highlight={seat.isWinner}
      />

      {/* 筹码 + 本街下注 */}
      <div className="ml-auto flex flex-col items-end">
        <span className="font-mono text-sm font-bold text-amber-300">{fmt(seat.chips)}</span>
        {seat.bet > 0 && (
          <span className="text-[11px] text-slate-300">下注 {fmt(seat.bet)}</span>
        )}
      </div>

      {/* 最近动作气泡 */}
      {seat.lastAction && (
        <span
          className={`rounded-full px-2 py-0.5 text-[10px] font-medium ${
            seat.lastAction.action === 'fold'
              ? 'bg-error-500/20 text-error-400'
              : seat.lastAction.action === 'raise' || seat.lastAction.action === 'allin'
                ? 'bg-brand-500/20 text-brand-300'
                : 'bg-slate-600/40 text-slate-300'
          }`}
        >
          {ACTION_LABEL[seat.lastAction.action] ?? seat.lastAction.action}
          {seat.lastAction.amount > 0 &&
          seat.lastAction.action !== 'fold' &&
          seat.lastAction.action !== 'check'
            ? ` ${fmt(seat.lastAction.amount)}`
            : ''}
        </span>
      )}

      {acting && isTop && <span className="ml-1 h-2 w-2 animate-pulse rounded-full bg-emerald-400" />}
      {seat.folded && <span className="text-[10px] text-error-400">已弃牌</span>}
      {seat.allin && !seat.folded && <span className="text-[10px] text-amber-400">ALL-IN</span>}
    </div>
  )
}

export default function PokerTable({
  vm,
  revealMode = 'all',
}: {
  vm: MatchViewModel
  /** 'all' 总是揭示双方底牌（实时观赛）；'showdown' 仅摊牌手揭示（回放） */
  revealMode?: 'all' | 'showdown'
}) {
  const [s0, s1] = vm.seats
  const boardCount = vm.street === 'preflop' ? 0 : vm.street === 'flop' ? 3 : vm.street === 'turn' ? 4 : 5
  const handLabel = vm.matchOver
    ? '对局结束'
    : `第 ${vm.hand + 1} / ${vm.totalHands} 手`

  // 是否揭示底牌：showdown 模式下，仅当前手以摊牌结算时才揭示
  const settledShowdown =
    vm.lastSettle !== null &&
    vm.lastSettle.hand === vm.hand &&
    vm.lastSettle.reason === 'showdown'
  const baseReveal = revealMode === 'all' || settledShowdown || vm.matchOver

  return (
    <div className="mx-auto w-full max-w-2xl">
      {/* 顶部状态条 */}
      <div className="mb-2 flex items-center justify-between text-xs text-slate-300">
        <span>{handLabel}</span>
        <span className="rounded-full bg-slate-800/80 px-2 py-0.5 text-brand-200">
          {STREET_LABEL[vm.street] ?? vm.street}
        </span>
        {vm.matchWinner !== null && (
          <span className="text-amber-300">
            胜者：座位 {vm.matchWinner}
          </span>
        )}
      </div>

      {/* 牌桌 */}
      <div className="felt-table relative rounded-[2rem] px-4 py-5">
        {/* 顶部座位（座位 1） */}
        <div className="relative z-10 mb-4">
          <SeatBox
            seat={s1}
            idx={1}
            sbSeat={vm.sbSeat}
            isTop
            revealHole={baseReveal || s1.isWinner}
            matchOver={vm.matchOver}
          />
        </div>

        {/* 中间：底池 + 公共牌 */}
        <div className="relative z-10 flex flex-col items-center gap-2 py-2">
          <div className="flex items-center gap-2">
            <span className="text-xs text-slate-400">底池</span>
            <span className="font-mono text-lg font-bold text-amber-300">{fmt(vm.pot)}</span>
          </div>
          <CardRow cards={vm.board} count={boardCount} size="md" />
          {vm.board.length === 0 && vm.street === 'preflop' && (
            <span className="text-[11px] text-slate-400">等待发牌…</span>
          )}
          {/* 当前行动者提示 */}
          {!vm.matchOver && vm.toAct !== null && (
            <span className="mt-1 text-[11px] text-emerald-300">
              轮到 座位 {vm.toAct} 决策
            </span>
          )}
        </div>

        {/* 底部座位（座位 0） */}
        <div className="relative z-10 mt-4">
          <SeatBox
            seat={s0}
            idx={0}
            sbSeat={vm.sbSeat}
            isTop={false}
            revealHole={baseReveal || s0.isWinner}
            matchOver={vm.matchOver}
          />
        </div>
      </div>

      {/* 底部信息：累计盈亏 */}
      <div className="mt-2 flex justify-between text-xs">
        <span className={s0.net >= 0 ? 'text-emerald-400' : 'text-error-400'}>
          座位 0 累计：{s0.net >= 0 ? '+' : ''}{fmt(s0.net)}
        </span>
        <span className={s1.net >= 0 ? 'text-emerald-400' : 'text-error-400'}>
          座位 1 累计：{s1.net >= 0 ? '+' : ''}{fmt(s1.net)}
        </span>
      </div>
    </div>
  )
}
