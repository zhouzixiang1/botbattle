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
  /** 名称与 owner 的最大展示行数；移动数据卡可放宽，表格默认仍保持单行。 */
  textLines?: 1 | 2 | 3
}

function OwnerIdentity({
  participant,
  links,
  lines,
}: {
  participant: ResolvedMatchParticipant
  links: boolean
  lines: 1 | 2 | 3
}) {
  const label = participantOwnerText(participant)
  if (!participant.ownerName || !links) {
    return (
      <OverflowText lines={lines} tooltip={label} tooltipFocusable={false} className="text-xs text-muted-foreground">
        {label}
      </OverflowText>
    )
  }
  return (
    <Link
      to={`/user/${encodeURIComponent(participant.ownerName)}`}
      className="min-w-0 text-xs text-muted-foreground hover:text-primary"
    >
      <OverflowText lines={lines} tooltip={label} tooltipFocusable={false}>{label}</OverflowText>
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
  textLines = 1,
}: MatchParticipantIdentityProps) {
  const participant = resolveMatchParticipant(source, side)
  const explicitEmpty = Boolean(emptyLabel && !participant.isHuman && participant.botId == null)
  const stateClass = state === 'winner'
    ? 'text-success'
    : state === 'loser'
      ? 'text-muted-foreground'
      : 'text-foreground'
  const subject = participant.isHuman ? participant.ownerLabel : participant.botLabel

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
        <EntityName lines={textLines} tooltip={emptyLabel} tooltipFocusable={false} className="text-sm italic text-muted-foreground">
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
      <div className="flex min-w-0 items-start gap-2">
        {participant.isHuman && participant.ownerName && links ? (
          <Link
            to={`/user/${encodeURIComponent(participant.ownerName)}`}
            className="block min-w-0 flex-1 hover:text-primary"
          >
            <EntityName
              lines={textLines}
              tooltip={subject}
              tooltipFocusable={false}
              className={cn('text-sm font-semibold hover:text-primary', stateClass)}
            >
              {subject}
            </EntityName>
          </Link>
        ) : participant.isHuman ? (
          <EntityName
            lines={textLines}
            tooltip={subject}
            tooltipFocusable={false}
            className={cn('min-w-0 flex-1 text-sm font-semibold', stateClass)}
          >
            {subject}
          </EntityName>
        ) : participant.botId != null && links ? (
          <Link to={`/bot/${participant.botId}`} className="block min-w-0 flex-1 hover:text-primary">
            <EntityName
              lines={textLines}
              tooltip={participant.botLabel}
              tooltipFocusable={false}
              className={cn('text-sm font-semibold hover:text-primary', stateClass)}
            >
              {participant.botLabel}
            </EntityName>
          </Link>
        ) : (
          <EntityName
            lines={textLines}
            tooltip={participant.botLabel}
            tooltipFocusable={false}
            className={cn(
              'min-w-0 flex-1 text-sm font-semibold',
              participant.botId == null ? 'text-muted-foreground' : stateClass,
            )}
          >
            {participant.botLabel}
          </EntityName>
        )}
        {state === 'winner' && <Badge className="mt-0.5 h-4 shrink-0 px-1 text-[9px]">胜</Badge>}
      </div>
      {!participant.isHuman && (
        <div className="mt-0.5 min-w-0">
          <OwnerIdentity participant={participant} links={links} lines={textLines} />
        </div>
      )}
      <div className="mt-0.5 flex min-w-0 flex-wrap items-center gap-x-1.5 text-[10px] font-medium text-muted-foreground">
        <span>{participant.isHuman ? '真人' : 'Bot'}</span>
        <span aria-hidden="true">·</span>
        <span>{participant.seatLabel}</span>
        {seatDetail && <><span aria-hidden="true">·</span><span>{seatDetail}</span></>}
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
