import type { ContestEstimate } from '@/components/contest/stage-series'

export type ContestTemplatePurpose = 'fairness' | 'speed' | 'ranking' | 'championship'
export type ContestTemplateTimeClass = 'short' | 'medium' | 'long'

export interface ContestTemplateGuidance {
  id: string
  name: string
  recommended_min?: number | null
  recommended_max?: number | null
  participant_range_is_strict?: boolean
  purpose?: ContestTemplatePurpose | null
  time_class?: ContestTemplateTimeClass | null
  stages?: Array<{
    type?: string
    tiebreak?: string | null
  }>
}

export const PAIRED_SWAP_TIEBREAK = 'paired_swap_until_decided'

const PURPOSE_LABELS: Record<ContestTemplatePurpose, string> = {
  fairness: '公平优先',
  speed: '速度优先',
  ranking: '全员排名',
  championship: '冠军赛',
}

const TIME_CLASS_LABELS: Record<ContestTemplateTimeClass, string> = {
  short: '短赛程',
  medium: '中等赛程',
  long: '长赛程',
}

export function templatePurposeLabel(value: ContestTemplateGuidance['purpose']): string | null {
  return value ? PURPOSE_LABELS[value] ?? null : null
}

export function templateTimeClassLabel(value: ContestTemplateGuidance['time_class']): string | null {
  return value ? TIME_CLASS_LABELS[value] ?? null : null
}

export function recommendedRangeLabel(template: ContestTemplateGuidance): string | null {
  const minimum = template.recommended_min
  const maximum = template.recommended_max
  if (typeof minimum !== 'number' || !Number.isInteger(minimum) || minimum < 2) return null
  const prefix = template.participant_range_is_strict ? '限' : '建议'
  if (maximum == null) return `${prefix} ${minimum} 人以上`
  if (typeof maximum !== 'number' || !Number.isInteger(maximum) || maximum < minimum) return null
  return minimum === maximum ? `${prefix} ${minimum} 人` : `${prefix} ${minimum}–${maximum} 人`
}

export type TemplateParticipantFit = 'within' | 'below' | 'above' | 'unknown'

export function templateParticipantFit(
  template: ContestTemplateGuidance,
  participantCount: number | null | undefined,
): TemplateParticipantFit {
  if (participantCount == null || !Number.isInteger(participantCount) || participantCount < 0) return 'unknown'
  const minimum = template.recommended_min
  const maximum = template.recommended_max
  if (typeof minimum !== 'number' || !Number.isInteger(minimum)) return 'unknown'
  if (participantCount < minimum) return 'below'
  if (typeof maximum === 'number' && Number.isInteger(maximum) && participantCount > maximum) return 'above'
  return 'within'
}

export function templateFitMessage(
  template: ContestTemplateGuidance,
  participantCount: number | null | undefined,
): string | null {
  const range = recommendedRangeLabel(template)
  if (!range) return null
  const fit = templateParticipantFit(template, participantCount)
  if (template.participant_range_is_strict) {
    if (fit === 'unknown') return `${range}；报名截止并发布时严格校验人数。`
    if (fit === 'within') return `当前 ${participantCount} 人符合发布人数要求。`
    return `当前 ${participantCount} 人不符合发布人数要求；必须调整到 ${range.replace(/^限\s*/, '')}。`
  }
  if (fit === 'unknown') return `${range}；人数仅影响推荐，不限制创建或发布。`
  if (fit === 'within') return `当前 ${participantCount} 人符合建议范围；人数仅影响推荐，不限制发布。`
  return `当前 ${participantCount} 人${fit === 'below' ? '低于' : '超过'}建议范围；仍可发布，请结合基础场数和耗时选择。`
}

export function matchingTemplateAlternatives(
  templates: ContestTemplateGuidance[],
  selectedTemplateId: string,
  participantCount: number | null | undefined,
  limit = 2,
): ContestTemplateGuidance[] {
  if (participantCount == null || participantCount < 2) return []
  return templates
    .filter((template) => (
      template.id !== selectedTemplateId
      && templateParticipantFit(template, participantCount) === 'within'
    ))
    .slice(0, Math.max(0, limit))
}

export function templateHasUnboundedTiebreak(
  template: Pick<ContestTemplateGuidance, 'stages'> | null | undefined,
): boolean {
  return template?.stages?.some((stage) => (
    stage.type === 'single_elimination' && stage.tiebreak === PAIRED_SWAP_TIEBREAK
  )) === true
}

export function estimatedScoringGames(estimate: ContestEstimate | null | undefined): number | undefined {
  if (typeof estimate?.estimated_scoring_games === 'number') return estimate.estimated_scoring_games
  if (!estimate?.stages?.length) return estimate?.estimated_matches
  return estimate.stages.reduce((sum, stage) => sum + stage.estimated_execution_legs, 0)
}

export type ContestScheduleRisk = 'none' | 'long' | 'very_long'

export function contestScheduleRisk(seconds: number | null | undefined): ContestScheduleRisk {
  if (seconds == null || !Number.isFinite(seconds) || seconds <= 8 * 60 * 60) return 'none'
  return seconds > 24 * 60 * 60 ? 'very_long' : 'long'
}
