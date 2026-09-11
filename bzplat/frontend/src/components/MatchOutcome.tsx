import type { ComponentProps } from 'react'

import { cn } from '@/lib/utils'
import {
  describeMatchOutcome,
  type DescribeMatchOutcomeOptions,
  type MatchOutcomeSource,
} from '@/lib/match-outcome'

interface MatchOutcomeProps extends Omit<ComponentProps<'div'>, 'children'>,
  DescribeMatchOutcomeOptions {
  source: MatchOutcomeSource
  /** Show each authoritative scoring game below the compact score line. */
  showGames?: boolean
  /** Keep only the primary line in dense tables and command results. */
  primaryOnly?: boolean
  /** flow-wide：宽表格（≥@5xl 容器）主/次赛果折成一行（· 分隔）；默认 stacked 两行。 */
  layout?: 'stacked' | 'flow-wide'
}

export function MatchOutcome({
  source,
  seatLabels,
  normalizedUnit,
  showGames = false,
  primaryOnly = false,
  layout = 'stacked',
  className,
  ...props
}: MatchOutcomeProps) {
  const description = describeMatchOutcome(source, { seatLabels, normalizedUnit })
  const primary = primaryOnly && description.kind === 'duplicate'
    ? `复式交锋 · ${description.primary}`
    : description.primary
  const flowWide = layout === 'flow-wide'
  return (
    <div
      data-match-outcome={description.availability}
      data-match-outcome-kind={description.kind ?? 'none'}
      className={cn('min-w-0 text-xs leading-relaxed', className)}
      {...props}
    >
      <div className={cn('font-medium text-foreground', flowWide && '@5xl:inline')}>{primary}</div>
      {!primaryOnly && description.secondary && (
        <div
          className={cn(
            'text-muted-foreground',
            flowWide && '@5xl:inline @5xl:before:mr-1.5 @5xl:before:content-["·"]',
          )}
        >
          {description.secondary}
        </div>
      )}
      {!primaryOnly && showGames && description.kind === 'duplicate' && description.games.length > 0 && (
        <div className="mt-0.5 flex min-w-0 flex-wrap gap-x-2 gap-y-0.5 text-muted-foreground">
          {description.games.map((game) => <span key={game}>{game}</span>)}
        </div>
      )}
    </div>
  )
}

export type { MatchOutcomeProps }
