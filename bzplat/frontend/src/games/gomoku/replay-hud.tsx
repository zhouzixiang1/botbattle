import { Clock } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import type { GameAuxiliaryProps } from '@/games/base'
import { gomokuSeatDetail, type GomokuViewModel } from '@/games/gomoku/reducer'
import { eventSeatSubject } from '@/games/seat-display'

function formatClock(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds)) return '—'
  const value = Math.max(0, Math.floor(seconds))
  const minutes = Math.floor(value / 60)
  return `${minutes}:${String(value % 60).padStart(2, '0')}`
}

/** 全场棋钟是五子棋规则状态，观战、回放与人机页共用同一视图。 */
export function GomokuReplayHud({ vm, seats }: GameAuxiliaryProps) {
  const state = vm as GomokuViewModel
  return (
    <section
      data-testid="gomoku-clock-hud"
      aria-label="五子棋全场棋钟"
      className="grid min-w-0 grid-cols-2 gap-2 rounded-xl border border-border bg-card p-2 shadow-sm 2xl:grid-cols-1"
    >
      {([0, 1] as const).map((seat) => {
        const acting = !state.matchOver && state.toAct === seat
        const timedOut = state.timeOut === seat
        return (
          <div
            key={seat}
            data-testid={`gomoku-clock-seat-${seat + 1}`}
            className={`min-w-0 rounded-lg border px-2.5 py-2 ${acting ? 'border-primary/50 bg-primary/5 ring-2 ring-primary/20' : 'border-border bg-muted/25'}`}
          >
            <div className="flex min-w-0 items-center gap-2">
              <span className="min-w-0 flex-1 truncate text-xs font-semibold text-foreground">
                {eventSeatSubject(seats, seat)}
              </span>
              <span className="inline-flex shrink-0 items-center gap-1 font-mono text-sm font-semibold tabular-nums text-foreground">
                <Clock aria-hidden="true" className="size-3.5 text-muted-foreground" />
                {formatClock(state.timeRemaining[seat])}
              </span>
            </div>
            <div className="mt-1 flex min-w-0 items-center gap-1.5 text-[11px] text-muted-foreground">
              <span className="min-w-0 flex-1 truncate">{gomokuSeatDetail(state, seat)}</span>
              {acting && !timedOut && <Badge variant="outline">当前行动</Badge>}
              {timedOut && <Badge variant="destructive">棋钟耗尽</Badge>}
            </div>
          </div>
        )
      })}
    </section>
  )
}
