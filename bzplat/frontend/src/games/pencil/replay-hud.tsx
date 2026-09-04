import { Clock } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import type { GameAuxiliaryProps } from '@/games/base'
import { useTickingRemaining } from '@/games/clock-tick'
import type { PencilViewModel } from '@/games/pencil/reducer'
import { eventSeatSubject, seatDisplay } from '@/games/seat-display'

function formatClock(seconds: number): string {
  const value = Math.max(0, Math.floor(seconds))
  const minutes = Math.floor(value / 60)
  return `${minutes}:${String(value % 60).padStart(2, '0')}`
}

function metric(label: string, value: number, total?: number) {
  return (
    <div className="min-w-0 rounded-md bg-muted/45 px-2 py-1.5 text-center">
      <div className="font-mono text-sm font-semibold text-foreground">
        {value}{total != null ? <span className="text-[11px] font-normal text-muted-foreground">/{total}</span> : null}
      </div>
      <div className="truncate text-[11px] leading-tight text-muted-foreground">{label}</div>
    </div>
  )
}

/**
 * 点格棋专属局面概览。
 *
 * 常规桌面作为棋盘上方的紧凑横向 HUD；超宽屏由 MatchViewer 放入左侧信息轨，
 * 本组件同步切为纵向。空白区域只承载可从当前回放帧直接推导的局面数据，避免
 * 重复通用对阵卡或展示并不存在的策略指标。
 */
export function PencilReplayHud({ vm, seats, liveEdge }: GameAuxiliaryProps) {
  const state = vm as PencilViewModel
  // 直播边缘对行动方本地走秒；新 time_used 事件到达即重新校准。
  // hook 必须先于早退 return 调用，state 为空时以全空数组退化为不显示。
  const timeRemaining = useTickingRemaining(
    Boolean(liveEdge) && !state?.matchOver,
    state?.timeRemaining ?? [null, null],
    state?.toAct === 0 || state?.toAct === 1 ? state.toAct : null,
  )
  if (!state?.scores) return null

  const totalBoxes = Math.max(0, (state.nDots - 1) ** 2)
  const totalEdges = Math.max(0, state.nDots * (state.nDots - 1) * 2)
  const connectedEdges = Math.min(totalEdges, Object.keys(state.edgeOwner ?? {}).length)
  const edgeProgress = totalEdges > 0 ? Math.round((connectedEdges / totalEdges) * 100) : 0
  const majorityTarget = Math.floor(totalBoxes / 2) + 1
  const scoreGap = Math.abs(state.scores[0] - state.scores[1])
  const leader = state.scores[0] === state.scores[1] ? null : state.scores[0] > state.scores[1] ? 0 : 1
  const redEdges = Object.values(state.edgeOwner ?? {}).filter((owner) => owner === 0).length
  const blueEdges = Object.values(state.edgeOwner ?? {}).filter((owner) => owner === 1).length
  const boxMap = Array.from({ length: Math.max(0, state.nDots - 1) }, (_, row) =>
    Array.from({ length: Math.max(0, state.nDots - 1) }, (_, column) =>
      state.boxOwner?.[column * 2 + 1]?.[row * 2 + 1] ?? -1,
    ),
  )
  const mappedBoxes = boxMap.flat()
  const mappedRedBoxes = mappedBoxes.filter((owner) => owner === 0).length
  const mappedBlueBoxes = mappedBoxes.filter((owner) => owner === 1).length
  const claimedBoxes = Math.min(totalBoxes, mappedRedBoxes + mappedBlueBoxes)
  const adjudicatedWinner = state.matchOver
    && (state.winner === 0 || state.winner === 1)
    && state.scores[state.winner] < majorityTarget
    ? state.winner
    : null
  const actingSeat = state.toAct === 0 || state.toAct === 1 ? state.toAct : null
  const identities = ([0, 1] as const).map((seat) => seatDisplay(seats?.[seat], seat))
  const subjects = ([0, 1] as const).map((seat) => eventSeatSubject(seats, seat))
  const stateLabel = state.matchOver
    ? '对局已结束'
    : state.mustPass && actingSeat != null
      ? `${subjects[actingSeat]} 需要让行`
    : actingSeat != null
      ? `${subjects[actingSeat]} 正在行动`
      : '等待裁判'

  return (
    <section
      data-testid="pencil-position-overview"
      aria-label="点格棋局面概览"
      className="min-w-0 overflow-hidden rounded-xl border border-border bg-card p-2 shadow-sm 2xl:flex 2xl:flex-col 2xl:py-1.5"
    >
      <div className="flex min-w-0 items-center gap-2">
        <div className="min-w-0">
          <div className="text-xs font-semibold text-foreground">局面概览</div>
          <div data-testid="pencil-turn-status" className="truncate text-[11px] leading-tight text-muted-foreground">
            {stateLabel}{state.extraTurn && !state.matchOver && !state.mustPass ? ' · 得分连走' : ''}
          </div>
        </div>
        <span className="ml-auto shrink-0 font-mono text-xs text-muted-foreground">{edgeProgress}%</span>
      </div>
      <div
        className="mt-2 h-1.5 overflow-hidden rounded-full bg-muted"
        role="progressbar"
        aria-label="已连边进度"
        aria-valuemin={0}
        aria-valuemax={totalEdges}
        aria-valuenow={connectedEdges}
      >
        <div className="h-full rounded-full bg-primary transition-[width]" style={{ width: `${edgeProgress}%` }} />
      </div>

      <div className="mt-2 grid grid-cols-2 gap-2 2xl:grid-cols-1 2xl:gap-1.5">
        {([0, 1] as const).map((seat) => {
          const identity = identities[seat]
          const isActing = !state.matchOver && state.toAct === seat
          const remaining = timeRemaining[seat]
          const timedOut = state.timeOut === seat
          const color = seat === 0 ? 'text-seat-1' : 'text-seat-2'
          const tint = seat === 0 ? 'bg-seat-1/5' : 'bg-seat-2/5'
          return (
            <div
              key={seat}
              data-testid={`pencil-seat-score-${seat + 1}`}
              className={`min-w-0 rounded-lg border px-2.5 py-2 2xl:py-1.5 ${tint} ${isActing ? 'border-primary/50 ring-2 ring-primary/25' : 'border-border'}`}
            >
              <div className="flex min-w-0 items-center gap-2">
                <span className="min-w-0 flex-1 truncate text-xs font-semibold text-foreground">{identity.subject}</span>
                <span className={`ml-auto shrink-0 font-mono text-xl font-bold leading-none ${color}`}>{state.scores[seat]}</span>
              </div>
              <div className="mt-0.5 flex min-w-0 items-center gap-1.5 text-[10px] text-muted-foreground">
                {identity.owner && <span className="min-w-0 truncate">{identity.owner}</span>}
                {identity.owner && <span aria-hidden="true">·</span>}
                <Badge variant="outline" className={`shrink-0 px-1.5 text-[10px] leading-tight ${color}`}>
                  {seat === 0 ? '先手 · 红' : '后手 · 蓝'} · {identity.seat}
                </Badge>
              </div>
              <div className="mt-1.5 flex min-w-0 items-center gap-1.5 text-[11px] text-muted-foreground">
                {isActing && !timedOut && (
                  <span className="min-w-0 flex-1 truncate text-primary">
                    {state.mustPass ? '强制让行' : '当前行动'}
                  </span>
                )}
                {(remaining != null || timedOut) && (
                  <span className={`ml-auto inline-flex shrink-0 items-center gap-1 font-mono tabular-nums ${isActing ? 'text-foreground' : ''}`}>
                    <Clock className="size-3" />{formatClock(remaining ?? 0)}
                  </span>
                )}
                {timedOut && <Badge variant="destructive" className="text-[11px] leading-tight">超时</Badge>}
              </div>
            </div>
          )
        })}
      </div>

      <div className="mt-2 grid grid-cols-4 gap-1.5 2xl:grid-cols-2">
        {metric('已连边', connectedEdges, totalEdges)}
        {metric('剩余边', Math.max(0, totalEdges - connectedEdges))}
        {metric('已占格', claimedBoxes, totalBoxes)}
        {metric('未决格', Math.max(0, totalBoxes - claimedBoxes))}
      </div>

      <div className="mt-2 flex min-w-0 items-center gap-2 border-t border-border pt-2 text-[11px] leading-tight text-muted-foreground">
        <span className="shrink-0">最近连边</span>
        <span data-testid="pencil-last-edge" className="min-w-0 flex-1 truncate text-right font-mono text-foreground">
          {state.lastEdge ? `(${state.lastEdge.x}, ${state.lastEdge.y})` : '尚无'}
        </span>
      </div>

      <div className="hidden 2xl:mt-2 2xl:flex 2xl:min-h-0 2xl:flex-1 2xl:flex-col 3xl:mt-3">
        <div>
          <div className="flex items-center gap-2 text-[11px] leading-tight text-muted-foreground">
            <span className="font-medium">格子归属</span>
            <span className="ml-auto font-mono">
              红 {mappedRedBoxes} · 蓝 {mappedBlueBoxes} · 未决 {Math.max(0, totalBoxes - claimedBoxes)}
            </span>
          </div>
          <div
            data-testid="pencil-box-map"
            role="img"
            aria-label={`格子归属缩略图：红方 ${mappedRedBoxes} 格，蓝方 ${mappedBlueBoxes} 格，未决 ${Math.max(0, totalBoxes - claimedBoxes)} 格`}
            className="mx-auto mt-2 grid aspect-square w-full max-w-32 gap-1 2xl:max-w-24 3xl:max-w-32"
            style={{ gridTemplateColumns: `repeat(${Math.max(1, state.nDots - 1)}, minmax(0, 1fr))` }}
          >
            {boxMap.flatMap((row, rowIndex) => row.map((owner, columnIndex) => (
              <span
                key={`${rowIndex}-${columnIndex}`}
                aria-hidden
                data-owner={owner}
                className={`grid min-w-0 place-items-center rounded-sm border text-[10px] font-semibold leading-none ${owner === 0
                  ? 'border-seat-1/30 bg-seat-1/15 text-seat-1'
                  : owner === 1
                    ? 'border-seat-2/35 bg-seat-2/15 text-seat-2'
                    : 'border-border bg-muted/35 text-muted-foreground'
                }`}
              >
                {owner === 0 ? '1' : owner === 1 ? '2' : ''}
              </span>
            )))}
          </div>
        </div>

        <div className="mt-2 3xl:mt-3">
          <div className="flex items-center gap-2 text-[11px] leading-tight text-muted-foreground">
            <span className="font-medium">连边构成</span>
            <span className="ml-auto font-mono">红 {redEdges} · 蓝 {blueEdges} · 空 {Math.max(0, totalEdges - connectedEdges)}</span>
          </div>
          <div
            data-testid="pencil-edge-composition"
            role="img"
            aria-label={`连边构成：红方 ${redEdges} 条，蓝方 ${blueEdges} 条，未连 ${Math.max(0, totalEdges - connectedEdges)} 条`}
            className="mt-1.5 flex h-2 overflow-hidden rounded-full bg-muted"
          >
            <span aria-hidden className="h-full bg-seat-1" style={{ width: `${totalEdges ? (redEdges / totalEdges) * 100 : 0}%` }} />
            <span aria-hidden className="h-full bg-seat-2" style={{ width: `${totalEdges ? (blueEdges / totalEdges) * 100 : 0}%` }} />
          </div>
        </div>

        <div className="mt-2 border-t border-border pt-2 3xl:mt-3 3xl:pt-3">
          <div className="text-[11px] font-medium leading-tight text-muted-foreground">胜负态势</div>
          <div className="mt-1.5 rounded-lg bg-muted/45 px-2.5 py-2">
            <div className="text-xs font-semibold text-foreground">
              {adjudicatedWinner != null
                ? `${subjects[adjudicatedWinner]} 经裁判判定获胜`
                : leader == null
                  ? '双方暂时持平'
                  : `${subjects[leader]} 领先 ${scoreGap} 格`}
            </div>
            <div className="mt-1 text-[11px] leading-tight text-muted-foreground">
              {adjudicatedWinner != null
                ? `终止前红 ${mappedRedBoxes} 格 · 蓝 ${mappedBlueBoxes} 格 · ${Math.max(0, totalBoxes - claimedBoxes)} 格未决`
                : `过半门槛 ${majorityTarget} 格 · 尚有 ${Math.max(0, totalBoxes - claimedBoxes)} 格未决`}
            </div>
          </div>
        </div>
      </div>
    </section>
  )
}
