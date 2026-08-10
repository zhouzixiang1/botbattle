import { Clock } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import type { GameAuxiliaryProps } from '@/games/base'
import type { PencilViewModel } from '@/games/pencil/reducer'

function formatClock(seconds: number): string {
  const value = Math.max(0, Math.floor(seconds))
  const minutes = Math.floor(value / 60)
  return `${minutes}:${String(value % 60).padStart(2, '0')}`
}

/** 点格棋专属比分/棋钟 HUD；通用 MatchViewer 只按契约挂载。 */
export function PencilReplayHud({ vm }: GameAuxiliaryProps) {
  const state = vm as PencilViewModel
  if (!state?.scores) return null
  return (
    <div className="grid grid-cols-2 gap-2">
      {([0, 1] as const).map((seat) => {
        const isActing = !state.matchOver && state.toAct === seat
        const remaining = state.timeRemaining?.[seat]
        const timedOut = state.timeOut === seat
        const color = seat === 0 ? 'text-destructive' : 'text-chart-2'
        return (
          <div
            key={seat}
            data-testid={`pencil-seat-score-${seat + 1}`}
            className={`min-w-0 rounded-lg border px-3 py-2 ${isActing ? 'border-primary/50 ring-2 ring-primary/30' : 'border-border'}`}
          >
            <div className="flex min-w-0 items-center gap-2">
              <Badge variant="outline" className={`shrink-0 ${color}`}>
                座位 {seat + 1} · {seat === 0 ? '红' : '蓝'}
              </Badge>
              <span className={`ml-auto shrink-0 font-mono text-xl font-bold ${color}`}>{state.scores[seat]}</span>
            </div>
            {(remaining != null || timedOut) && (
              <div className={`mt-1.5 flex items-center gap-1 text-sm ${isActing ? 'text-foreground' : 'text-muted-foreground'}`}>
                <Clock className="inline size-3.5" /> {formatClock(remaining ?? 0)}
                {timedOut && <Badge variant="destructive" className="ml-1 text-xs">超时</Badge>}
                {isActing && !timedOut && <span className="ml-auto text-xs text-primary">当前行动</span>}
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}
