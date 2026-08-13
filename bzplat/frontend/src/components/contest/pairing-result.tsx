import type { ComponentProps } from 'react'

import {
  resolveMatchParticipant,
  type MatchParticipantSource,
} from '@/lib/match-participants'
import { cn } from '@/lib/utils'

interface PairingResultSource extends MatchParticipantSource {
  is_bye?: boolean
  status?: string
  match_winner?: number | null
}

function pairingResultLabel(pairing: PairingResultSource): string {
  if (pairing.status !== 'completed') return '赛果待定'
  const label = (side: 0 | 1) => {
    const participant = resolveMatchParticipant(pairing, side)
    return participant.isHuman ? participant.ownerLabel : participant.botLabel
  }
  if (pairing.is_bye === true) return `${label(0)} 轮空晋级`
  if (pairing.match_winner === 0) return `${label(0)} 胜`
  if (pairing.match_winner === 1) return `${label(1)} 胜`
  return '平局'
}

function PairingResult({
  pairing,
  className,
  ...props
}: Omit<ComponentProps<'span'>, 'children'> & { pairing: PairingResultSource }) {
  const label = pairingResultLabel(pairing)
  return (
    <span
      data-pairing-result={pairing.status === 'completed' ? 'decided' : 'pending'}
      aria-label={`赛果：${label}`}
      className={cn(
        'text-xs font-medium',
        pairing.status === 'completed' ? 'text-foreground' : 'text-muted-foreground',
        className,
      )}
      {...props}
    >
      {label}
    </span>
  )
}

export { PairingResult, pairingResultLabel }
export type { PairingResultSource }
