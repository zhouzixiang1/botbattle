import { Badge } from '@/components/ui/badge'
import type { GameAuxiliaryProps } from '@/games/base'
import {
  GOMOKU_COMPETITION_RULESET,
  gomokuColorLabel,
  gomokuForbiddenLabel,
  gomokuPhaseLabel,
  type GomokuViewModel,
} from '@/games/gomoku/reducer'

function selectedLabel(vm: GomokuViewModel): string | null {
  if (!vm.selectedPoint) return null
  const ordinal = vm.selectedIndex === null ? '' : `#${vm.selectedIndex + 1} `
  return `保留 ${ordinal}(${vm.selectedPoint.x}, ${vm.selectedPoint.y})`
}

export function GomokuReplaySummary({ vm }: GameAuxiliaryProps) {
  const state = vm as GomokuViewModel
  const competition = state.ruleset === GOMOKU_COMPETITION_RULESET
  const selected = selectedLabel(state)

  return (
    <div
      data-testid="gomoku-replay-summary"
      className="flex min-w-0 flex-1 flex-wrap items-center gap-x-2 gap-y-1 text-xs"
    >
      <Badge variant={competition ? 'default' : 'secondary'}>
        {competition ? '全国竞赛规则' : '旧版自由五子棋'}
      </Badge>
      <span className="font-medium text-foreground">{gomokuPhaseLabel(state.phase)}</span>
      {state.openingCode && (
        <span data-testid="gomoku-opening-summary" className="text-muted-foreground">
          开局 {state.openingCode}{state.n !== null ? ` · ${state.n} 打` : ''}
        </span>
      )}
      {state.swapped !== null && (
        <span data-testid="gomoku-swap-summary" className="text-muted-foreground">
          {state.swapped ? '已交换棋色' : '未交换棋色'}
        </span>
      )}
      <span
        data-testid="gomoku-seat-colors"
        className="font-medium text-foreground"
      >
        座位 1 执{gomokuColorLabel(state.seatColors[0])} · 座位 2 执{gomokuColorLabel(state.seatColors[1])}
      </span>
      {state.candidates.length > 0 && (
        <span data-testid="gomoku-candidate-summary" className="text-muted-foreground">
          候选 {state.candidates.length} 点{selected ? ` · ${selected}` : ' · 待保留'}
        </span>
      )}
      {state.forbidden && (
        <Badge data-testid="gomoku-forbidden-summary" variant="destructive">
          {gomokuForbiddenLabel(state.forbidden.kind)} · ({state.forbidden.x}, {state.forbidden.y})
        </Badge>
      )}
    </div>
  )
}
