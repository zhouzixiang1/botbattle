export type MatchOutcomeSeat = 0 | 1

export interface PublicMatchOutcomeGame {
  index: number
  winner: MatchOutcomeSeat | null
  rounds_played: number | null
  normalized_delta_a: number
}

export interface PublicMatchOutcome {
  kind: 'single' | 'duplicate'
  planned_games: number
  completed_games: number
  score: {
    wins_a: number
    draws: number
    wins_b: number
  }
  rounds_played: number
  normalized_delta_a: number
  games: PublicMatchOutcomeGame[]
  termination: {
    kind: 'normal' | 'technical'
    reason: string
    loser: MatchOutcomeSeat | null
  }
}

export interface MatchOutcomeSource {
  status?: string | null
  outcome?: PublicMatchOutcome | null
}

export interface MatchOutcomeDescription {
  availability: 'pending' | 'available' | 'unavailable' | 'aborted'
  kind: PublicMatchOutcome['kind'] | null
  primary: string
  secondary: string | null
  games: string[]
  /** Only a single-game outcome can have one overall winner. */
  winner: MatchOutcomeSeat | null | undefined
  technical: boolean
}

export interface DescribeMatchOutcomeOptions {
  seatLabels?: readonly [string, string]
  /** Game-specific normalized unit, for example `BB`. Omit to hide the delta. */
  normalizedUnit?: string
}

/**
 * Distinguish the new public contract's explicit `outcome: null` from an old
 * server response that predates the field. Only the latter may use a legacy
 * winner as a compatibility fallback.
 */
export function hasPublicMatchOutcomeField(source: MatchOutcomeSource | null | undefined): boolean {
  return source != null && Object.prototype.hasOwnProperty.call(source, 'outcome')
}

const DEFAULT_SEAT_LABELS = ['座位 1', '座位 2'] as const
const TECHNICAL_REASON_LABELS: Record<string, string> = {
  bot_deleted: 'Bot 已删除',
  contest_bot_unavailable: '赛事 Bot 不可用',
  crash: 'Bot 崩溃',
  error: 'Bot 运行异常',
  illegal: '非法动作',
  illegal_candidates: '候选点不合法',
  illegal_opening: '开局响应不合法',
  illegal_selection: '选择不合法',
  illegal_swap: '交换响应不合法',
  protocol_error: '协议错误',
  technical_loss: '技术判负',
  timeout: '超时',
}

function nonNegativeInteger(value: unknown): value is number {
  return Number.isInteger(value) && Number(value) >= 0
}

function finiteNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value)
}

function validWinner(value: unknown): value is MatchOutcomeSeat | null {
  return value === 0 || value === 1 || value === null
}

function winnerMatchesDelta(winner: MatchOutcomeSeat | null, deltaA: number): boolean {
  if (winner === 0) return deltaA > 0
  if (winner === 1) return deltaA < 0
  return deltaA === 0
}

/**
 * Public endpoints intentionally return `outcome=null` when no authoritative
 * result can be projected. Keep this runtime guard at the display boundary so
 * malformed or historical payloads never turn a missing winner into a draw.
 */
export function isPublicMatchOutcome(value: unknown): value is PublicMatchOutcome {
  if (!value || typeof value !== 'object') return false
  const candidate = value as Partial<PublicMatchOutcome>
  if (candidate.kind !== 'single' && candidate.kind !== 'duplicate') return false
  if (!nonNegativeInteger(candidate.planned_games) || !nonNegativeInteger(candidate.completed_games)) return false
  if (candidate.completed_games > candidate.planned_games) return false
  if (!candidate.score || typeof candidate.score !== 'object') return false
  if (!nonNegativeInteger(candidate.score.wins_a)
    || !nonNegativeInteger(candidate.score.draws)
    || !nonNegativeInteger(candidate.score.wins_b)) return false
  if (candidate.score.wins_a + candidate.score.draws + candidate.score.wins_b !== candidate.completed_games) return false
  if (!nonNegativeInteger(candidate.rounds_played) || !finiteNumber(candidate.normalized_delta_a)) return false
  if (!candidate.termination || typeof candidate.termination !== 'object') return false
  if (candidate.termination.kind !== 'normal' && candidate.termination.kind !== 'technical') return false
  if (typeof candidate.termination.reason !== 'string' || !validWinner(candidate.termination.loser)) return false
  if (candidate.termination.kind === 'normal' && candidate.termination.loser !== null) return false
  if (candidate.termination.kind === 'technical'
    && candidate.termination.loser !== 0
    && candidate.termination.loser !== 1) return false
  if (!Array.isArray(candidate.games) || candidate.games.length !== candidate.completed_games) return false
  if (!candidate.games.every((game, position) => {
    const previousIndex = position > 0 ? candidate.games?.[position - 1]?.index : 0
    const validIndex = candidate.termination?.kind === 'technical'
      ? nonNegativeInteger(game?.index)
        && Number(game.index) >= 1
        && Number(game.index) <= Number(candidate.planned_games)
        && Number(game.index) > Number(previousIndex)
      : nonNegativeInteger(game?.index) && game.index === position + 1
    if (game == null
      || typeof game !== 'object'
      || !validIndex
      || !validWinner(game.winner)
      || (game.rounds_played !== null && !nonNegativeInteger(game.rounds_played))
      || !finiteNumber(game.normalized_delta_a)) return false
    // A technical winner is assigned by the referee, not by the chip delta at
    // the interruption point. Normal games must still be directionally exact.
    return candidate.termination?.kind === 'technical'
      || winnerMatchesDelta(game.winner, game.normalized_delta_a)
  })) return false
  const projectedScore = candidate.games.reduce(
    (score, game) => {
      if (game.winner === 0) score.wins_a += 1
      else if (game.winner === 1) score.wins_b += 1
      else score.draws += 1
      return score
    },
    { wins_a: 0, draws: 0, wins_b: 0 },
  )
  if (projectedScore.wins_a !== candidate.score.wins_a
    || projectedScore.draws !== candidate.score.draws
    || projectedScore.wins_b !== candidate.score.wins_b) return false
  const projectedDelta = candidate.games.reduce((sum, game) => sum + game.normalized_delta_a, 0)
  if (Math.abs(projectedDelta - candidate.normalized_delta_a) > 1e-9) return false
  if (candidate.games.every((game) => game.rounds_played !== null)) {
    const projectedRounds = candidate.games.reduce((sum, game) => sum + (game.rounds_played ?? 0), 0)
    if (projectedRounds !== candidate.rounds_played) return false
  }
  if (candidate.kind === 'single') {
    if (candidate.planned_games !== 1 || candidate.completed_games !== 1) return false
  } else {
    if (candidate.planned_games !== 2) return false
    if (candidate.termination.kind === 'normal') {
      if (candidate.completed_games !== candidate.planned_games) return false
    } else {
      if (candidate.completed_games !== 1) return false
    }
  }
  if (candidate.termination.kind === 'technical') {
    const expectedWinner = candidate.termination.loser === 0 ? 1 : 0
    if (candidate.games[0]?.winner !== expectedWinner) return false
  }
  return true
}

function signed(value: number): string {
  if (value === 0) return '0'
  return `${value > 0 ? '+' : ''}${value.toLocaleString('en-US', { maximumFractionDigits: 2 })}`
}

function gameOutcomeLabel(
  game: PublicMatchOutcomeGame,
  labels: readonly [string, string],
): string {
  const result = game.winner === 0
    ? `${labels[0]}胜`
    : game.winner === 1
      ? `${labels[1]}胜`
      : '平局'
  return `第 ${game.index} 场：${result}`
}

export function singleOutcomeWinner(
  outcome: PublicMatchOutcome | null | undefined,
): MatchOutcomeSeat | null | undefined {
  if (!isPublicMatchOutcome(outcome) || outcome.kind !== 'single') return undefined
  return outcome.games[0]?.winner
}

export function outcomeParticipantStates(
  outcome: PublicMatchOutcome | null | undefined,
): readonly ['winner' | 'loser' | 'neutral', 'winner' | 'loser' | 'neutral'] {
  const winner = singleOutcomeWinner(outcome)
  if (winner === 0) return ['winner', 'loser']
  if (winner === 1) return ['loser', 'winner']
  return ['neutral', 'neutral']
}

export function outcomeLabelForSeat(
  outcome: PublicMatchOutcome | null | undefined,
  seat: MatchOutcomeSeat,
): string {
  if (!isPublicMatchOutcome(outcome)) return '赛果暂不可用'
  const wins = seat === 0 ? outcome.score.wins_a : outcome.score.wins_b
  const losses = seat === 0 ? outcome.score.wins_b : outcome.score.wins_a
  if (outcome.kind === 'duplicate') {
    return `复式 · ${wins}胜 / ${outcome.score.draws}平 / ${losses}负`
  }
  const winner = outcome.games[0]?.winner
  if (winner === undefined) return '赛果暂不可用'
  if (winner === null) return '平'
  return winner === seat ? '胜' : '负'
}

export function describeMatchOutcome(
  source: MatchOutcomeSource,
  options: DescribeMatchOutcomeOptions = {},
): MatchOutcomeDescription {
  const labels = options.seatLabels ?? DEFAULT_SEAT_LABELS
  const outcome = isPublicMatchOutcome(source.outcome) ? source.outcome : null
  if (!outcome) {
    if (source.status === 'aborted') {
      return {
        availability: 'aborted', kind: null, primary: '对局已中止', secondary: null,
        games: [], winner: undefined, technical: false,
      }
    }
    const terminal = source.status === 'completed'
    return {
      availability: terminal ? 'unavailable' : 'pending',
      kind: null,
      primary: terminal ? '赛果暂不可用' : '赛果待定',
      secondary: null,
      games: [],
      winner: undefined,
      technical: false,
    }
  }

  const gameLabels = outcome.games.map((game) => gameOutcomeLabel(game, labels))
  const progress = `已完成 ${outcome.completed_games}/${outcome.planned_games} 场计分`
  const delta = options.normalizedUnit
    ? `${outcome.kind === 'duplicate' ? '交锋组合计分差' : '本场分差'}（${labels[0]}） ${signed(outcome.normalized_delta_a)} ${options.normalizedUnit}`
    : null
  const technical = outcome.termination.kind === 'technical'
  const technicalLabel = technical
    ? outcome.termination.loser === 0 || outcome.termination.loser === 1
      ? `${labels[outcome.termination.loser]} 技术判负`
      : '技术终局'
    : null
  const reasonLabel = technical
    ? outcome.termination.reason === 'technical_loss' || outcome.termination.reason === 'completed'
      ? null
      : TECHNICAL_REASON_LABELS[outcome.termination.reason]
        ?? (outcome.termination.reason || null)
    : null
  const secondary = [progress, delta, technicalLabel, reasonLabel && `原因：${reasonLabel}`]
    .filter(Boolean)
    .join(' · ') || null

  if (outcome.kind === 'duplicate') {
    if (technical) {
      const currentScore = `${labels[0]} ${outcome.score.wins_a}胜 · 平 ${outcome.score.draws} · ${labels[1]} ${outcome.score.wins_b}胜`
      const technicalSecondary = [currentScore, delta, technicalLabel, reasonLabel && `原因：${reasonLabel}`]
        .filter(Boolean)
        .join(' · ') || null
      return {
        availability: 'available',
        kind: 'duplicate',
        primary: `技术终局 · 已计 ${outcome.completed_games}/${outcome.planned_games} 场`,
        secondary: technicalSecondary,
        games: gameLabels,
        winner: undefined,
        technical: true,
      }
    }
    return {
      availability: 'available',
      kind: 'duplicate',
      primary: `${labels[0]} ${outcome.score.wins_a}胜 · 平 ${outcome.score.draws} · ${labels[1]} ${outcome.score.wins_b}胜`,
      secondary,
      games: gameLabels,
      winner: undefined,
      technical,
    }
  }

  const winner = outcome.games[0]?.winner
  if (winner === undefined) {
    return {
      availability: source.status === 'completed' ? 'unavailable' : 'pending',
      kind: 'single',
      primary: source.status === 'completed' ? '赛果暂不可用' : '赛果待定',
      secondary,
      games: gameLabels,
      winner: undefined,
      technical,
    }
  }
  return {
    availability: 'available',
    kind: 'single',
    primary: winner === 0
      ? `${labels[0]}胜`
      : winner === 1
        ? `${labels[1]}胜`
        : '平局',
    secondary,
    games: gameLabels,
    winner,
    technical,
  }
}
