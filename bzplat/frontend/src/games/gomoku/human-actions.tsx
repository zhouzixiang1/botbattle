import { useEffect, useMemo, useState } from 'react'
import { Check, Eraser, FastForward, RotateCcw, Shuffle } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import type { HumanTurnSurfaceProps, RawEvent } from '@/games/base'
import {
  gomokuColorLabel,
  gomokuPhaseLabel,
  type GomokuPoint,
} from '@/games/gomoku/reducer'

const BOARD_PHASES = new Set([
  'opening_proposal',
  'white4',
  'black5_candidates',
  'black5_select',
  'normal_play',
])

const BLACK5_CANDIDATE_COUNT = 2

function integer(value: unknown): number | null {
  const parsed = Number(value)
  return Number.isInteger(parsed) ? parsed : null
}

function readPoints(value: unknown): GomokuPoint[] {
  if (!Array.isArray(value)) return []
  return value.flatMap((item) => {
    if (!item || typeof item !== 'object') return []
    const row = item as Record<string, unknown>
    const x = integer(row.x)
    const y = integer(row.y)
    return x === null || y === null ? [] : [{ x, y }]
  })
}

function samePoint(left: GomokuPoint, right: GomokuPoint) {
  return left.x === right.x && left.y === right.y
}

function pointLabel(point: GomokuPoint) {
  return `(${point.x}, ${point.y})`
}

function phaseInstruction(phase: string): string {
  if (phase === 'opening_proposal') return '黑 1 固定在天元。依次点白 2、黑 3；五手二打固定提交 2 个候选。'
  if (phase === 'swap_choice') return '查看前三手后决定是否交换棋色；座位不变，只交换黑白身份。'
  if (phase === 'white4') return '你当前执白。点一个空点落下白 4。'
  if (phase === 'black5_candidates') return '你当前执黑。为五手二打依次选择 2 个不同形的空点；同一剩余对称轨道只可选一个，候选点尚不落入正式棋盘。'
  if (phase === 'black5_select') return '你当前执白。从候选中保留一个点；该点将成为唯一真实的黑 5。'
  if (phase === 'normal_play') return '点一个空点落子；裁判按当前棋色判定五连与黑方禁手。'
  return '等待裁判给出可执行动作。'
}

export function GomokuHumanTurnSurface({
  disabled,
  legal,
  request,
  events,
  renderBoard,
  onSubmit,
}: HumanTurnSurfaceProps) {
  const phase = typeof request?.phase === 'string' ? request.phase : ''
  const [draftPoints, setDraftPoints] = useState<GomokuPoint[]>([])

  useEffect(() => {
    setDraftPoints([])
  }, [request])

  const candidates = readPoints(request?.candidates)
  const actionEnabled = legal && !disabled && Boolean(request)
  const boardEnabled = actionEnabled && BOARD_PHASES.has(phase)
  // HumanPlay's clock updates the parent every 500 ms. Keep the synthetic
  // board event stable across those renders so GameCanvas doesn't mistake a
  // timer tick for a new position and clear the keyboard-selected point.
  const draftEvent = useMemo<RawEvent>(() => ({
    type: 'human_draft',
    request: request ?? {},
    phase,
    points: draftPoints,
    n: BLACK5_CANDIDATE_COUNT,
  }), [draftPoints, phase, request])
  const boardEvents = useMemo(
    () => request ? [...events, draftEvent] : events,
    [draftEvent, events, request],
  )

  const submitMove = (point: GomokuPoint) => {
    onSubmit({ response: { action: 'move', x: point.x, y: point.y } })
  }

  const submitSelection = (point: GomokuPoint) => {
    const index = candidates.findIndex((candidate) => samePoint(candidate, point))
    if (index >= 0) onSubmit({ response: { action: 'black5_select', index } })
  }

  const handleBoardPoint = (x: number, y: number) => {
    if (!actionEnabled) return
    const target = { x, y }
    if (phase === 'opening_proposal') {
      setDraftPoints((current) => current.length === 0
        ? [target]
        : current.length === 1
          ? [current[0], target]
          : [current[0], target])
      return
    }
    if (phase === 'black5_candidates') {
      setDraftPoints((current) => {
        const exists = current.some((point) => samePoint(point, target))
        if (exists) return current.filter((point) => !samePoint(point, target))
        return current.length < BLACK5_CANDIDATE_COUNT ? [...current, target] : current
      })
      return
    }
    if (phase === 'black5_select') {
      submitSelection(target)
      return
    }
    if (phase === 'white4' || phase === 'normal_play') submitMove(target)
  }

  const confirmOpening = () => {
    if (draftPoints.length !== 2) return
    onSubmit({
      response: {
        action: 'opening',
        white2: draftPoints[0],
        black3: draftPoints[1],
        n: BLACK5_CANDIDATE_COUNT,
      },
    })
  }

  const confirmCandidates = () => {
    if (draftPoints.length !== BLACK5_CANDIDATE_COUNT) return
    onSubmit({ response: { action: 'black5_candidates', points: draftPoints } })
  }

  return (
    <section data-testid="gomoku-human-surface" className="min-w-0 space-y-2.5">
      {renderBoard({
        events: boardEvents,
        onMove: handleBoardPoint,
        interactive: boardEnabled,
      })}

      <div className="min-w-0 rounded-lg border border-border bg-card px-3 py-2.5 shadow-xs">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <Badge variant="outline" data-testid="gomoku-human-phase">
            {gomokuPhaseLabel(phase, BLACK5_CANDIDATE_COUNT)}
          </Badge>
          {(integer(request?.me) === 0 || integer(request?.me) === 1) && (
            <span className="text-xs text-muted-foreground">
              座位 {Number(request?.me) + 1} · 当前执{gomokuColorLabel(request?.color)}
            </span>
          )}
          {phase === 'black5_candidates' && (
            <span className="ml-auto text-xs font-medium tabular-nums text-foreground" aria-live="polite">
              已选 {draftPoints.length}/{BLACK5_CANDIDATE_COUNT}
            </span>
          )}
        </div>
        <p className="mt-1.5 text-sm leading-5 text-muted-foreground">
          {phaseInstruction(phase)}
        </p>

        {phase === 'opening_proposal' && (
          <div className="mt-2.5 space-y-2.5" data-testid="gomoku-opening-controls">
            <div className="grid min-w-0 grid-cols-3 gap-2 text-xs">
              <div className="rounded-md border border-border bg-muted/30 px-2 py-1.5">
                <span className="block text-muted-foreground">黑 1</span>
                <span className="font-mono font-medium text-foreground">(7, 7)</span>
              </div>
              <div className="rounded-md border border-border bg-muted/30 px-2 py-1.5">
                <span className="block text-muted-foreground">白 2</span>
                <span className="font-mono font-medium text-foreground">
                  {draftPoints[0] ? pointLabel(draftPoints[0]) : '待选择'}
                </span>
              </div>
              <div className="rounded-md border border-border bg-muted/30 px-2 py-1.5">
                <span className="block text-muted-foreground">黑 3</span>
                <span className="font-mono font-medium text-foreground">
                  {draftPoints[1] ? pointLabel(draftPoints[1]) : '待选择'}
                </span>
              </div>
            </div>
            <div className="flex min-w-0 flex-wrap gap-2">
              <Button
                type="button"
                variant="outline"
                className="min-h-11"
                disabled={!actionEnabled || draftPoints.length === 0}
                onClick={() => setDraftPoints([])}
              >
                <RotateCcw aria-hidden="true" />重选白 2 / 黑 3
              </Button>
              <Button
                type="button"
                className="min-h-11 flex-1 sm:flex-none"
                data-testid="gomoku-submit-opening"
                disabled={!actionEnabled || draftPoints.length !== 2}
                onClick={confirmOpening}
              >
                <Check aria-hidden="true" />提交指定开局
              </Button>
            </div>
          </div>
        )}

        {phase === 'swap_choice' && (
          <div className="mt-2.5 grid min-w-0 gap-2 sm:grid-cols-2" data-testid="gomoku-swap-controls">
            <Button
              type="button"
              variant="outline"
              className="min-h-11 whitespace-normal"
              disabled={!actionEnabled}
              onClick={() => onSubmit({ response: { action: 'swap', swap: false } })}
            >
              <Check aria-hidden="true" />不交换，继续执白
            </Button>
            <Button
              type="button"
              className="min-h-11 whitespace-normal"
              data-testid="gomoku-submit-swap"
              disabled={!actionEnabled}
              onClick={() => onSubmit({ response: { action: 'swap', swap: true } })}
            >
              <Shuffle aria-hidden="true" />交换棋色，改执黑
            </Button>
          </div>
        )}

        {phase === 'black5_candidates' && (
          <div className="mt-2.5 space-y-2" data-testid="gomoku-candidate-controls">
            {draftPoints.length > 0 && (
              <div className="grid min-w-0 grid-cols-2 gap-2 sm:grid-cols-3">
                {draftPoints.map((point, index) => (
                  <Button
                    key={`${point.x},${point.y}`}
                    type="button"
                    variant="outline"
                    className="min-h-11 min-w-0 justify-between px-2 font-mono"
                    disabled={!actionEnabled}
                    onClick={() => setDraftPoints((current) => current.filter((_, i) => i !== index))}
                    aria-label={`移除候选 ${index + 1}，坐标 ${point.x},${point.y}`}
                  >
                    <span className="truncate">#{index + 1} {pointLabel(point)}</span>
                    <Eraser aria-hidden="true" className="size-3.5" />
                  </Button>
                ))}
              </div>
            )}
            <div className="flex min-w-0 flex-wrap gap-2">
              <Button
                type="button"
                variant="outline"
                className="min-h-11"
                disabled={!actionEnabled || draftPoints.length === 0}
                onClick={() => setDraftPoints([])}
              >
                <RotateCcw aria-hidden="true" />清空候选
              </Button>
              <Button
                type="button"
                className="min-h-11 flex-1 sm:flex-none"
                data-testid="gomoku-submit-candidates"
                disabled={!actionEnabled || draftPoints.length !== BLACK5_CANDIDATE_COUNT}
                onClick={confirmCandidates}
              >
                <Check aria-hidden="true" />提交 2 个候选
              </Button>
            </div>
          </div>
        )}

        {phase === 'black5_select' && (
          <div className="mt-2.5 grid min-w-0 grid-cols-2 gap-2 sm:grid-cols-3" data-testid="gomoku-select-controls">
            {candidates.map((point, index) => (
              <Button
                key={`${point.x},${point.y}`}
                type="button"
                variant="outline"
                className="min-h-11 min-w-0 px-2"
                disabled={!actionEnabled}
                onClick={() => onSubmit({ response: { action: 'black5_select', index } })}
              >
                保留 #{index + 1} · {pointLabel(point)}
              </Button>
            ))}
          </div>
        )}

        {phase === 'normal_play' && Boolean(request?.pass_allowed) && (
          <div className="mt-2.5 flex min-w-0 flex-wrap items-center gap-2">
            <Button
              type="button"
              variant="outline"
              className="min-h-11"
              data-testid="gomoku-submit-pass"
              disabled={!actionEnabled}
              onClick={() => onSubmit({ response: { action: 'pass' } })}
            >
              <FastForward aria-hidden="true" />PASS，让行
            </Button>
            <span className="text-xs text-muted-foreground">双方连续 PASS 时，本局判和。</span>
          </div>
        )}
      </div>
    </section>
  )
}
