import { ArrowRight } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import type { HumanActionPanelProps } from '@/games/base'

export function isPencilPassRequest(request: Record<string, unknown> | null): boolean {
  return Number(request?.pass) === 1
}

/**
 * 对手成格后仍由对手连走；Pencil 行协议要求当前座位先回 (-1,-1) 让行。
 * 这是协议动作，不是棋盘上的边，因此必须独立于 canvas pick 明确提交。
 */
export function PencilHumanActions({
  disabled,
  legal,
  request,
  onSubmit,
}: HumanActionPanelProps) {
  if (!isPencilPassRequest(request)) return null

  return (
    <Card
      data-testid="pencil-pass-action"
      className="flex flex-col gap-3 border-primary/30 bg-primary/5 px-4 py-3 sm:flex-row sm:items-center sm:justify-between"
    >
      <div className="min-w-0">
        <p className="font-medium text-foreground">对手围成了格，将继续连边</p>
        <p className="text-sm text-muted-foreground">本回合无需选择边，请确认让行。</p>
      </div>
      <Button
        type="button"
        className="shrink-0"
        disabled={disabled || !legal}
        onClick={() => onSubmit({ response: { x: -1, y: -1 } })}
      >
        确认让行
        <ArrowRight className="size-4" />
      </Button>
    </Card>
  )
}
