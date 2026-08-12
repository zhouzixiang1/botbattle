import { Badge } from '@/components/ui/badge'
import type { GameAuxiliaryProps, RawEvent } from '@/games/base'
import type { SeatInfo } from '@/games/canvas-types'
import {
  holdemEventLeg,
  holdemPhysicalPairForEvent,
  holdemPhysicalSeatForEvent,
  latestHoldemHandAction,
  type HoldemViewModel,
  type Street,
} from '@/games/holdem/reducer'
import { eventSeatSubject, seatDisplay } from '@/games/seat-display'

const STREET_LABELS: Record<Street, string> = {
  preflop: '翻牌前',
  flop: '翻牌',
  turn: '转牌',
  river: '河牌',
  showdown: '摊牌',
}

const ACTION_LABELS: Record<string, string> = {
  fold: '弃牌',
  check: '过牌',
  call: '跟注',
  raise: '加注至',
  allin: '全押至',
}

function formatChips(value: number): string {
  return Math.round(value).toLocaleString('en-US')
}

function formatNet(value: number): string {
  return `${value >= 0 ? '+' : ''}${formatChips(value)}`
}

function actionText(event: RawEvent | undefined, seats?: SeatInfo[]): string {
  if (!event) return '等待首个动作'
  const seat = holdemPhysicalSeatForEvent(event.player, event)
  const action = String(event.action ?? '')
  const amount = Number(event.amount ?? 0)
  const amountText = amount > 0 ? ` ${formatChips(amount)}` : ''
  const subject = eventSeatSubject(seats, seat)
  const position = Number.isFinite(seat) ? `座位 ${seat + 1}` : '位置未知'
  return `${subject} · ${ACTION_LABELS[action] ?? action}${amountText} · ${position}`
}

/**
 * 德州当前帧信息面板。
 *
 * 这里只展示公开事件可推导的局面，不读取底牌，也不会重复顶部 Bot 身份卡。
 * 宽屏时由通用 MatchViewer 放在牌桌侧栏，中屏横排在牌桌上方，窄屏自然堆叠。
 */
export function HoldemReplayHud({ vm, seats }: GameAuxiliaryProps) {
  const state = vm as HoldemViewModel
  if (!state?.seats || !Array.isArray(state.events)) return null

  const settles = state.events.filter((event) => event.type === 'settle')
  const hasStarted = state.legStarted
  const completedHands = state.completedHands
  const totalMatchHands = state.totalHands * state.totalLegs
  const noCompletedTerminal = state.matchOver && completedHands === 0
  const currentHand = hasStarted && !noCompletedTerminal
    ? Math.min(state.totalHands, state.hand + 1)
    : 0
  const currentSettled = state.lastSettle?.hand === state.hand
  const phase = state.status === 'error'
    ? '对局中止'
    : noCompletedTerminal
      ? '未完成手牌'
      : state.matchOver
        ? state.isDuplicate ? '复式赛完成' : '整场完赛'
        : !hasStarted
          ? '等待发牌'
        : currentSettled
          ? '本手结算'
          : STREET_LABELS[state.street] ?? state.street
  const potLabel = state.pot > 0 ? '当前底池' : state.lastSettle ? '本手底池' : '当前底池'
  const potValue = state.pot > 0 ? state.pot : state.lastSettle?.pot ?? 0
  const lastAction = latestHoldemHandAction(state.events)
  const identities = ([0, 1] as const).map((seat) => seatDisplay(seats?.[seat], seat))
  const subjects = ([0, 1] as const).map((seat) => eventSeatSubject(seats, seat))
  const actingText = state.status === 'error'
    ? '对局已中止'
    : state.matchOver
      ? '最终局面'
      : !hasStarted
        ? '等待发牌'
    : currentSettled
      ? '等待下一手'
      : lastAction?.action === 'fold'
        ? '等待本手结算'
        : state.seats.some((seat) => seat.allin) && state.toAct === null
          ? '自动发牌 · 等待结算'
          : lastAction && state.toAct === null
            ? '等待下一街或结算'
          : state.toAct === 0 || state.toAct === 1
            ? `${subjects[state.toAct]} 行动`
            : '等待裁判'
  const recentSettles = settles.slice(-6).reverse()
  const wins = ([0, 1] as const).map((seat) => settles.filter((event) => {
    const winners = (event.winners as unknown[] | undefined)
      ?.map((winner) => holdemPhysicalSeatForEvent(winner, event)) ?? []
    return winners.length === 1 && winners[0] === seat
  }).length)
  const draws = settles.filter((event) => ((event.winners as unknown[] | undefined)?.length ?? 0) > 1).length
  const progress = totalMatchHands > 0
    ? Math.min(100, Math.max(0, (completedHands / totalMatchHands) * 100))
    : 0
  const legPrefix = state.isDuplicate ? `第 ${state.leg + 1}/${state.totalLegs} 局 · ` : ''

  return (
    <section
      data-testid="holdem-position-overview"
      aria-label="德州扑克局面概览"
      className="@container/holdem min-w-0 rounded-xl border border-border bg-card p-3 shadow-sm 3xl:sticky 3xl:top-6"
    >
      <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1.5">
        <div className="min-w-0">
          <div className="text-[11px] font-medium uppercase tracking-[0.14em] text-muted-foreground">局面概览</div>
          <div className="mt-0.5 text-sm font-semibold text-foreground">
            {currentHand > 0
              ? `${legPrefix}当前手 ${currentHand} / ${state.totalHands}`
              : noCompletedTerminal
                ? `未完成任何一手 · 共 ${totalMatchHands} 手`
                : `${legPrefix}等待发牌 · 共 ${totalMatchHands} 手`}
          </div>
        </div>
        <Badge variant={state.matchOver ? 'secondary' : 'outline'} className="ml-auto shrink-0">
          {phase}
        </Badge>
        <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted" role="progressbar" aria-label="已完成手数" aria-valuemin={0} aria-valuemax={totalMatchHands} aria-valuenow={completedHands}>
          <div className="h-full rounded-full bg-primary transition-[width]" style={{ width: `${progress}%` }} />
        </div>
        <div className="flex w-full items-center justify-between text-[11px] text-muted-foreground">
          <span>{state.isDuplicate ? '总计已结算' : '已结算'} {completedHands} 手</span>
          <span>剩余 {Math.max(0, totalMatchHands - completedHands)} 手</span>
        </div>
      </div>

      <div className="mt-2.5 grid grid-cols-2 gap-2 @max-3xs/holdem:grid-cols-1 @xl/holdem:grid-cols-[1.05fr_1fr_1fr]">
        <div className="col-span-2 grid grid-cols-2 gap-x-3 gap-y-1 rounded-lg border border-border/70 bg-muted/25 px-3 py-2 text-xs @max-3xs/holdem:col-span-1 @xl/holdem:col-span-1">
          <div>
            <div className="text-muted-foreground">阶段</div>
            <div className="mt-0.5 font-medium text-foreground">{phase}</div>
          </div>
          <div>
            <div className="text-muted-foreground">{potLabel}</div>
            <div className="mt-0.5 font-mono font-semibold text-foreground">{formatChips(potValue)}</div>
          </div>
          <div className="col-span-2 flex items-center justify-between gap-2 border-t border-border/60 pt-1.5 text-muted-foreground">
            <span>{actingText}</span>
            {hasStarted && <span className="shrink-0">按钮 · 座位 {state.sbSeat + 1}</span>}
          </div>
        </div>

        {([0, 1] as const).map((seat) => {
          const player = state.seats[seat]
          const identity = identities[seat]
          const handWinners = state.lastSettle?.winners ?? []
          const status = state.status === 'error'
            ? '已中止'
            : state.matchOver
              ? state.matchWinner === seat ? '整场胜者' : '已完赛'
            : !hasStarted
              ? '等待发牌'
            : currentSettled
              ? handWinners.length > 1
                ? '本手平分'
                : handWinners.includes(seat) ? '本手获胜' : '本手结束'
            : player.folded ? '已弃牌'
              : player.allin ? 'ALL-IN'
                : state.toAct === seat ? '当前行动'
                  : '等待行动'
          return (
            <div
              key={seat}
              data-testid={`holdem-seat-state-${seat + 1}`}
              className={`min-w-0 rounded-lg border px-3 py-2 ${hasStarted && state.toAct === seat && !state.matchOver ? 'border-primary/50 bg-primary/5 ring-1 ring-primary/20' : 'border-border/70 bg-muted/20'}`}
            >
              <div className="flex min-w-0 items-center justify-between gap-2">
                <span className="min-w-0 truncate text-xs font-semibold text-foreground">{identity.subject}</span>
                <span className="shrink-0 text-[10px] text-muted-foreground sm:text-[11px]">{identity.kind}</span>
              </div>
              <div className="mt-0.5 truncate text-[10px] text-muted-foreground">
                {identity.owner ? `${identity.owner} · ` : ''}{identity.seat} · {hasStarted ? (seat === state.sbSeat ? '小盲 / 按钮' : '大盲') : '尚未发牌'}
              </div>
              <div className="mt-1.5 grid grid-cols-3 gap-1.5 text-[11px]">
                <div><span className="block text-muted-foreground">剩余</span><span className="font-mono font-medium text-foreground">{formatChips(player.chips)}</span></div>
                <div><span className="block text-muted-foreground">本街</span><span className="font-mono font-medium text-foreground">{formatChips(player.bet)}</span></div>
                <div><span className="block text-muted-foreground">累计</span><span className={`font-mono font-semibold ${player.net > 0 ? 'text-success' : player.net < 0 ? 'text-destructive' : 'text-foreground'}`}>{formatNet(player.net)}</span></div>
              </div>
              <div className={`mt-1.5 border-t border-border/60 pt-1 text-[11px] ${hasStarted && state.toAct === seat && !state.matchOver ? 'font-medium text-primary' : 'text-muted-foreground'}`}>
                {status}
              </div>
            </div>
          )
        })}
      </div>

      <div className="mt-2 grid grid-cols-[0.9fr_1.1fr] gap-2 @max-3xs/holdem:grid-cols-1">
        <div className="min-w-0 rounded-lg border border-border/70 px-3 py-2">
          <div className="text-[11px] text-muted-foreground">最近动作</div>
          <div className="mt-0.5 truncate text-xs font-medium text-foreground">{actionText(lastAction, seats)}</div>
          <div className="mt-1 text-[11px] text-muted-foreground">
            胜手 {subjects[0]} {wins[0]} · {subjects[1]} {wins[1]}{draws > 0 ? ` · 平分 ${draws}` : ''}
          </div>
        </div>
        <div className="min-w-0 rounded-lg border border-border/70 px-3 py-2">
          <div className="flex items-center justify-between gap-2 text-[11px] text-muted-foreground">
            <span>最近 {recentSettles.length || 0} 手 · {subjects[0]} 净变化</span>
            <span className="shrink-0">单位：筹码</span>
          </div>
          {recentSettles.length ? (
            <div className="mt-1.5 grid grid-cols-3 gap-1 sm:grid-cols-6" aria-label="最近手牌结果">
              {recentSettles.map((event) => {
                const hand = Number(event.hand ?? 0)
                const eventLeg = holdemEventLeg(event)
                const deltas = holdemPhysicalPairForEvent(
                  (event.deltas as number[] | undefined) ?? [0, 0],
                  event,
                )
                const delta = Number(deltas[0] ?? 0)
                const handLabel = eventLeg === null
                  ? `第 ${hand + 1} 手`
                  : `第 ${eventLeg + 1} 局第 ${hand + 1} 手`
                return (
                  <div
                    key={`${eventLeg ?? 0}-${hand}-${delta}`}
                    aria-label={`${handLabel}，${subjects[0]} ${formatNet(delta)}`}
                    className={`min-w-0 rounded px-1 py-1 text-center font-mono text-[10px] font-medium ${delta > 0 ? 'bg-success/10 text-success' : delta < 0 ? 'bg-destructive/10 text-destructive' : 'bg-muted text-muted-foreground'}`}
                  >
                    {formatNet(delta)}
                  </div>
                )
              })}
            </div>
          ) : (
            <div className="mt-1.5 text-xs text-muted-foreground">首手尚未结算</div>
          )}
        </div>
      </div>
    </section>
  )
}
