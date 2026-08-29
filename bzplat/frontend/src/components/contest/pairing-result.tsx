import type { ComponentProps } from 'react'

import { MatchOutcome } from '@/components/MatchOutcome'
import {
  resolveMatchParticipant,
  type MatchParticipantSource,
} from '@/lib/match-participants'
import {
  describeMatchOutcome,
  type MatchOutcomeSource,
} from '@/lib/match-outcome'
import { cn } from '@/lib/utils'

interface PairingResultSource extends MatchParticipantSource, MatchOutcomeSource {
  is_bye?: boolean
  status?: string | null
  display_status?: string | null
  match_winner?: number | null
}

/** Prefer the backend's match-aware public status when pairing persistence lags. */
function effectivePairingStatus(pairing: PairingResultSource): string | null {
  return pairing.display_status ?? pairing.status ?? null
}

function pairingSeatLabels(pairing: PairingResultSource): readonly [string, string] {
  return ([0, 1] as const).map((side) => {
    const participant = resolveMatchParticipant(pairing, side)
    return participant.isHuman ? participant.ownerLabel : participant.botLabel
  }) as [string, string]
}

function pairingResultLabel(pairing: PairingResultSource): string {
  const labels = pairingSeatLabels(pairing)
  const status = effectivePairingStatus(pairing)
  if (pairing.is_bye === true && status === 'completed') return `${labels[0]} 轮空晋级`
  return describeMatchOutcome({ ...pairing, status }, { seatLabels: labels }).primary
}

function PairingResult({
  pairing,
  primaryOnly = false,
  className,
  ...props
}: Omit<ComponentProps<'div'>, 'children'> & {
  pairing: PairingResultSource
  /** Hide per-match scoring progress where a frozen legacy series settles only once. */
  primaryOnly?: boolean
}) {
  const status = effectivePairingStatus(pairing)
  const label = pairingResultLabel(pairing)
  const seatLabels = pairingSeatLabels(pairing)
  if (pairing.is_bye === true && status === 'completed') {
    return (
      <div
        data-pairing-result="decided"
        aria-label={`赛果：${label}`}
        className={cn('text-xs font-medium text-foreground', className)}
        {...props}
      >
        {label}
      </div>
    )
  }
  return (
    <MatchOutcome
      source={{ ...pairing, status }}
      seatLabels={seatLabels}
      primaryOnly={primaryOnly}
      data-pairing-result={status === 'completed' ? 'decided' : 'pending'}
      aria-label={`赛果：${label}`}
      className={cn(
        status === 'completed' ? 'text-foreground' : 'text-muted-foreground',
        className,
      )}
      {...props}
    />
  )
}

export { effectivePairingStatus, PairingResult, pairingResultLabel }
export type { PairingResultSource }
