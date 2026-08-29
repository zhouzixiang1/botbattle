import { AlertTriangle, Clock3, ListOrdered, UsersRound } from 'lucide-react'

import {
  formatContestDuration,
  stageSeriesDisplayLabel,
  type ContestEstimate,
} from '@/components/contest/stage-series'
import {
  contestScheduleRisk,
  estimatedScoringGames,
  matchingTemplateAlternatives,
  recommendedRangeLabel,
  templateFitMessage,
  templateHasUnboundedTiebreak,
  templateParticipantFit,
  templatePurposeLabel,
  templateTimeClassLabel,
  type ContestTemplateGuidance,
} from '@/components/contest/template-guidance'
import { Badge } from '@/components/ui/badge'

interface TemplateGuidancePanelProps {
  template?: ContestTemplateGuidance | null
  templates?: ContestTemplateGuidance[]
  participantCount?: number | null
  estimate?: ContestEstimate | null
  unboundedTiebreak?: boolean
  frozen?: boolean
  className?: string
}

export function TemplateGuidancePanel({
  template,
  templates = [],
  participantCount,
  estimate,
  unboundedTiebreak,
  frozen = false,
  className = '',
}: TemplateGuidancePanelProps) {
  if (!template && !estimate) return null
  const fit = template ? templateParticipantFit(template, participantCount) : 'unknown'
  const fitMessage = template ? templateFitMessage(template, participantCount) : null
  const alternatives = template
    ? matchingTemplateAlternatives(templates, template.id, participantCount)
    : []
  const scoringGames = estimatedScoringGames(estimate)
  const effectiveSwissRounds = (estimate?.stages || [])
    .filter((stage) => stage.effective_rounds != null)
    .map((stage) => `${stageSeriesDisplayLabel(stage.stage_key)} ${stage.effective_rounds} 轮`)
  const risk = contestScheduleRisk(estimate?.eta_seconds)
  const hasUnboundedTiebreak = unboundedTiebreak ?? templateHasUnboundedTiebreak(template)
  const metadata = template
    ? [
        recommendedRangeLabel(template),
        templatePurposeLabel(template.purpose),
        templateTimeClassLabel(template.time_class),
      ].filter((value): value is string => Boolean(value))
    : []

  return (
    <div className={`min-w-0 space-y-3 ${className}`.trim()}>
      {(metadata.length > 0 || fitMessage) && (
        <div className="min-w-0 space-y-2">
          {metadata.length > 0 && (
            <div className="flex min-w-0 flex-wrap gap-1.5" aria-label="赛制建议">
              {metadata.map((label) => <Badge key={label} variant="outline">{label}</Badge>)}
            </div>
          )}
          {fitMessage && (
            <p className="flex min-w-0 items-start gap-2 text-xs leading-relaxed text-muted-foreground">
              <UsersRound aria-hidden="true" className="mt-0.5 size-4 shrink-0" />
              <span className="min-w-0 break-words">{fitMessage}</span>
            </p>
          )}
          {fit !== 'within' && alternatives.length > 0 && (
            <p className="text-xs leading-relaxed text-foreground">
              更符合当前人数：{alternatives.map((item) => item.name).join('、')}。
            </p>
          )}
        </div>
      )}

      {estimate && (
        <div className="min-w-0 space-y-3 border-t pt-3">
          {effectiveSwissRounds.length > 0 && (
            <p className="flex min-w-0 items-start gap-2 text-xs leading-relaxed text-foreground">
              <ListOrdered aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
              <span className="min-w-0 break-words">
                当前有效轮数：{effectiveSwissRounds.join('；')}。{frozen ? '已按发布名单冻结。' : '发布排期后按报名人数冻结。'}
              </span>
            </p>
          )}
          <dl className="grid min-w-0 grid-cols-2 gap-x-3 gap-y-2 text-xs sm:grid-cols-4">
          <div className="min-w-0">
            <dt className="text-muted-foreground">基础对局记录</dt>
            <dd className="mt-0.5 font-mono font-semibold tabular-nums text-foreground">
              {estimate.estimated_matches == null ? '待估算' : `${estimate.estimated_matches} 场`}
            </dd>
          </div>
          <div className="min-w-0">
            <dt className="text-muted-foreground">基础计分场</dt>
            <dd className="mt-0.5 font-mono font-semibold tabular-nums text-foreground">
              {scoringGames == null ? '待估算' : `${scoringGames} 场`}
            </dd>
          </div>
          <div className="min-w-0">
            <dt className="text-muted-foreground">并发上限</dt>
            <dd className="mt-0.5 font-mono font-semibold tabular-nums text-foreground">
              {estimate.max_concurrent == null ? '按容量准入' : `${estimate.max_concurrent} 场`}
            </dd>
          </div>
          <div className="min-w-0">
            <dt className="text-muted-foreground">基础 ETA</dt>
            <dd className="mt-0.5 inline-flex items-center gap-1 font-mono font-semibold tabular-nums text-foreground">
              <Clock3 aria-hidden="true" className="size-3.5 text-muted-foreground" />
              {formatContestDuration(estimate.eta_seconds)}
            </dd>
          </div>
          </dl>
          <p className="text-xs leading-relaxed text-muted-foreground">
            并发数是代码槽位上限；每条任务的冻结 CPU、内存和 sandbox 向量可能降低实际并发并延长用时。
          </p>
        </div>
      )}

      {(risk !== 'none' || hasUnboundedTiebreak) && (
        <div role="alert" className="flex min-w-0 items-start gap-2 border-t border-warning/25 pt-3 text-xs leading-relaxed text-warning-foreground">
          <AlertTriangle aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-warning" />
          <span className="min-w-0 break-words">
            {risk === 'very_long'
              ? '基础赛程预计超过 24 小时，请优先比较更短模板并核对跨日排期。'
              : risk === 'long'
                ? '基础赛程预计超过 8 小时，请核对开赛时间、容量与阶段衔接。'
                : null}
            {risk !== 'none' && hasUnboundedTiebreak ? ' ' : null}
            {hasUnboundedTiebreak
              ? '淘汰平局会追加换边的两场决胜组，直到决出晋级者；加赛次数不封顶，不计入基础场数与 ETA。'
              : null}
          </span>
        </div>
      )}
    </div>
  )
}
