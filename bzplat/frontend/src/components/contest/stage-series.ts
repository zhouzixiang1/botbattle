export interface StageSeriesConfig {
  stage_key: string
  label: string
  games_per_pair: {
    default: number
    allowed_values: number[]
  }
  swiss_extra_rounds?: {
    default: number
    min: number
    max: number
  }
}

export interface StageSeriesSetting {
  games_per_pair: number
  swiss_extra_rounds?: number
}

export type StageSeriesSettings = Record<string, StageSeriesSetting>

export interface StageSeriesEstimate {
  stage_key: string
  participant_count: number
  conceptual_pairings: number
  effective_rounds?: number | null
  games_per_pair: number
  estimated_matches: number
  estimated_execution_legs: number
  eta_seconds: number
}

export interface ContestEstimate {
  estimated_matches?: number
  eta_seconds?: number
  stages?: StageSeriesEstimate[]
}

const BUILTIN_STAGE_SERIES_LABELS: Record<string, string> = {
  prelim: '预赛瑞士轮',
  qualify: '决赛全员循环排位',
  final8: 'Top 8 决胜',
}

export function stageSeriesDisplayLabel(
  stageKey: string | null | undefined,
  fallback?: string | null,
): string {
  if (stageKey && BUILTIN_STAGE_SERIES_LABELS[stageKey]) {
    return BUILTIN_STAGE_SERIES_LABELS[stageKey]
  }
  return fallback || stageKey || '等待赛程'
}

export function defaultStageSeriesSettings(
  configs: StageSeriesConfig[],
  persisted?: StageSeriesSettings | null,
): StageSeriesSettings {
  return Object.fromEntries(configs.map((config) => {
    const saved = persisted?.[config.stage_key]
    return [config.stage_key, {
      games_per_pair: saved?.games_per_pair ?? config.games_per_pair.default,
      ...(config.swiss_extra_rounds
        ? { swiss_extra_rounds: saved?.swiss_extra_rounds ?? config.swiss_extra_rounds.default }
        : {}),
    }]
  }))
}

export function stageSeriesSettingsValid(
  configs: StageSeriesConfig[],
  settings: StageSeriesSettings,
): boolean {
  return configs.every((config) => {
    const value = settings[config.stage_key]
    if (!value || !config.games_per_pair.allowed_values.includes(value.games_per_pair)) return false
    if (!config.swiss_extra_rounds) return value.swiss_extra_rounds == null
    const extra = value.swiss_extra_rounds
    return Number.isInteger(extra) && extra! >= config.swiss_extra_rounds.min && extra! <= config.swiss_extra_rounds.max
  })
}

export function sameStageSeriesSettings(
  configs: StageSeriesConfig[],
  left: StageSeriesSettings,
  right: StageSeriesSettings,
): boolean {
  return configs.every((config) => {
    const a = left[config.stage_key]
    const b = right[config.stage_key]
    return a?.games_per_pair === b?.games_per_pair &&
      (a?.swiss_extra_rounds ?? 0) === (b?.swiss_extra_rounds ?? 0)
  })
}

function safeRatio(numerator: number, denominator: number, fallback = 1): number {
  return denominator > 0 ? numerator / denominator : fallback
}

/** Project unsaved K/Swiss-round changes from the authoritative saved estimate. */
export function projectStageSeriesEstimate(
  estimate: StageSeriesEstimate | undefined,
  setting: StageSeriesSetting | undefined,
): StageSeriesEstimate | undefined {
  if (!estimate || !setting) return estimate
  const previousRounds = estimate.effective_rounds ?? null
  const selectedExtra = setting.swiss_extra_rounds ?? 0
  const participantCount = Math.max(0, estimate.participant_count)
  const baseRounds = participantCount <= 2
    ? 1
    : Math.max(1, Math.ceil(Math.log2(participantCount)))
  const coverageCap = participantCount <= 2
    ? 1
    : participantCount % 2 === 0
      ? participantCount - 1
      : participantCount
  const effectiveRounds = previousRounds == null
    ? null
    : Math.min(baseRounds + selectedExtra, coverageCap)
  const conceptualPairings = effectiveRounds == null
    ? estimate.conceptual_pairings
    : Math.floor(participantCount / 2) * effectiveRounds
  const estimatedMatches = conceptualPairings * setting.games_per_pair
  const legsPerMatch = safeRatio(estimate.estimated_execution_legs, estimate.estimated_matches)
  const secondsPerMatch = safeRatio(estimate.eta_seconds, estimate.estimated_matches, 0)
  return {
    ...estimate,
    conceptual_pairings: conceptualPairings,
    effective_rounds: effectiveRounds,
    games_per_pair: setting.games_per_pair,
    estimated_matches: estimatedMatches,
    estimated_execution_legs: Math.round(estimatedMatches * legsPerMatch),
    eta_seconds: Math.round(estimatedMatches * secondsPerMatch),
  }
}

export function formatContestDuration(seconds: number | undefined): string {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return '待估算'
  const minutes = Math.max(1, Math.ceil(seconds / 60))
  if (minutes < 60) return `约 ${minutes} 分钟`
  const hours = Math.floor(minutes / 60)
  const remainder = minutes % 60
  return remainder > 0 ? `约 ${hours} 小时 ${remainder} 分` : `约 ${hours} 小时`
}
