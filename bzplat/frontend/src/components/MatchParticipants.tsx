import { Link } from 'react-router-dom'

import { Badge } from '@/components/ui/badge'
import { EntityName, OverflowText } from '@/components/ui/overflow-text'
import { matchTypeBadge } from '@/lib/games'
import {
  isBotSelfPlay,
  participantOwnerText,
  resolveMatchParticipant,
  type MatchParticipantSource,
  type ResolvedMatchParticipant,
} from '@/lib/match-participants'
import { cn } from '@/lib/utils'

type ParticipantState = 'winner' | 'loser' | 'neutral'

interface MatchParticipantIdentityProps {
  source: MatchParticipantSource
  side: 0 | 1
  variant?: 'compact' | 'panel'
  state?: ParticipantState
  className?: string
  /** 赛事轮空等明确的非参与者占位。 */
  emptyLabel?: string
  links?: boolean
  seatDetail?: string
}

function OwnerIdentity({
  participant,
  links,
}: {
  participant: ResolvedMatchParticipant
  links: boolean
}) {
  const label = participantOwnerText(participant)
  if (!participant.ownerName || !links) {
    return (
      <OverflowText tooltip={label} tooltipFocusable={false} className="text-xs text-muted-foreground">
        {label}
      </OverflowText>
    )
  }
  return (
    <Link
      to={`/user/${encodeURIComponent(participant.ownerName)}`}
      className="min-w-0 text-xs text-muted-foreground hover:text-primary"
    >
      <OverflowText tooltip={label} tooltipFocusable={false}>{label}</OverflowText>
    </Link>
  )
}

export function MatchParticipantIdentity({
  source,
  side,
  variant = 'compact',
  state = 'neutral',
  className,
  emptyLabel,
  links = true,
  seatDetail,
}: MatchParticipantIdentityProps) {
  const participant = resolveMatchParticipant(source, side)
  const explicitEmpty = Boolean(emptyLabel && !participant.isHuman && participant.botId == null)
  const stateClass = state === 'winner'
    ? 'text-success'
    : state === 'loser'
      ? 'text-muted-foreground'
      : 'text-foreground'

  if (explicitEmpty) {
    return (
      <div
        data-match-participant="true"
        data-participant-kind="empty"
        data-seat={side + 1}
        className={cn(
          'min-w-0 py-0.5',
          variant === 'panel' && 'rounded-lg bg-muted/35 px-2.5 py-2',
          className,
        )}
      >
        <div className="text-[10px] font-medium text-muted-foreground">{participant.seatLabel}</div>
        <EntityName lines={1} tooltip={emptyLabel} tooltipFocusable={false} className="text-sm italic text-muted-foreground">
          {emptyLabel}
        </EntityName>
      </div>
    )
  }

  return (
    <div
      data-match-participant="true"
      data-participant-kind={participant.isHuman ? 'human' : 'bot'}
      data-participant-state={state}
      data-seat={side + 1}
      className={cn(
        'min-w-0 py-0.5',
        variant === 'panel' && 'rounded-lg bg-muted/35 px-2.5 py-2',
        className,
      )}
    >
      <div className="mb-0.5 flex min-w-0 items-center gap-1.5 text-[10px] font-medium text-muted-foreground">
        <span>{participant.seatLabel}</span>
        {seatDetail && <span>· {seatDetail}</span>}
        <span aria-hidden="true">·</span>
        <span>{participant.isHuman ? '真人' : 'Bot'}</span>
        {state === 'winner' && <Badge className="ml-auto h-4 px-1 text-[9px]">胜</Badge>}
      </div>
      {participant.isHuman ? (
        <EntityName lines={1} tooltip="真人" tooltipFocusable={false} className={cn('text-sm font-semibold', stateClass)}>
          真人
        </EntityName>
      ) : participant.botId != null && links ? (
        <Link to={`/bot/${participant.botId}`} className="block min-w-0 hover:text-primary">
          <EntityName
            lines={1}
            tooltip={participant.botLabel}
            tooltipFocusable={false}
            className={cn('text-sm font-semibold hover:text-primary', stateClass)}
          >
            {participant.botLabel}
          </EntityName>
        </Link>
      ) : (
        <EntityName
          lines={1}
          tooltip={participant.botLabel}
          tooltipFocusable={false}
          className={cn(
            'text-sm font-semibold',
            participant.botId == null ? 'text-muted-foreground' : stateClass,
          )}
        >
          {participant.botLabel}
        </EntityName>
      )}
      <div className="mt-0.5 grid min-w-0 grid-cols-[auto_minmax(0,1fr)] items-center gap-1">
        <span className="shrink-0 text-[10px] text-muted-foreground">
          {participant.isHuman ? '用户' : '所属'}
        </span>
        <OwnerIdentity participant={participant} links={links} />
      </div>
    </div>
  )
}

interface MatchParticipantsProps {
  source: MatchParticipantSource
  variant?: 'compact' | 'panel'
  className?: string
  states?: readonly [ParticipantState, ParticipantState]
  secondEmptyLabel?: string
  links?: boolean
}

export function MatchParticipants({
  source,
  variant = 'compact',
  className,
  states = ['neutral', 'neutral'],
  secondEmptyLabel,
  links = true,
}: MatchParticipantsProps) {
  return (
    <div
      data-match-participants="true"
      className={cn(
        'grid min-w-0 grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)] items-start gap-2',
        className,
      )}
    >
      <MatchParticipantIdentity source={source} side={0} variant={variant} state={states[0]} links={links} />
      <span className="self-center text-[10px] font-medium text-muted-foreground">VS</span>
      <MatchParticipantIdentity
        source={source}
        side={1}
        variant={variant}
        state={states[1]}
        emptyLabel={secondEmptyLabel}
        links={links}
      />
    </div>
  )
}

export function MatchNatureBadge({
  matchType,
  source,
  className,
}: {
  matchType?: string
  source?: MatchParticipantSource
  className?: string
}) {
  const selfPlay = source ? isBotSelfPlay(source) : false
  const nature = matchTypeBadge(matchType)
  return (
    <Badge
      data-match-nature={selfPlay ? 'self_play' : matchType || 'unknown'}
      variant="outline"
      className={cn('shrink-0 text-[10px]', nature?.cls, className)}
    >
      {selfPlay ? '自博弈' : nature?.label || '性质未知'}
    </Badge>
  )
}
