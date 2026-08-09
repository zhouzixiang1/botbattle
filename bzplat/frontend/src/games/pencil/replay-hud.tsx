import { Clock } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import type { GameAuxiliaryProps } from '@/games/base'
import type { PencilViewModel } from './reducer'

function formatClock(seconds: number): string {
  const value = Math.max(0, Math.floor(seconds))
  const minutes = Math.floor(value / 60)
  return `${minutes}:${String(value % 60).padStart(2, '0')}`
}

/** 点格棋专属比分/棋钟 HUD；通用 MatchViewer 只按契约挂载。 */
export function PencilReplayHud({ vm, seats }: GameAuxiliaryProps) {
  const state = vm as PencilViewModel
  if (!state?.scores) return null
  return (
    <div className="grid grid-cols-2 gap-2">
      {([0, 1] as const).map((seat) => {
        const isActing = !state.matchOver && state.toAct === seat
        const remaining = state.timeRemaining?.[seat]
        const timedOut = state.timeOut === seat
        const name = seats?.[seat]?.botName || seats?.[seat]?.ownerName || (seat === 0 ? '红方' : '蓝方')
        const color = seat === 0 ? 'text-chart-3' : 'text-chart-2'
        return (
          <div key={seat} className={`rounded-lg border p-3 ${isActing ? 'ring-2 ring-primary' : 'border-border'}`}>
            <div className="flex items-center justify-between">
              <span className={`font-medium ${color}`}>{name}</span>
              <span className="font-mono text-lg font-bold">{state.scores[seat]}</span>
            </div>
            {(remaining != null || timedOut) && (
              <div className={`mt-1 text-sm ${isActing ? 'text-foreground' : 'text-muted-foreground'}`}>
                <Clock className="inline size-3.5" /> {formatClock(remaining ?? 0)}
                {timedOut && <Badge variant="destructive" className="ml-1 text-xs">超时</Badge>}
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}
