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

/** 桌面密度：建议徽标、公平性数字、并发说明与风险警示合并为紧凑行，不损失文案。 */
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
    <div className={`min-w-0 space-y-1.5 ${className}`.trim()}>
      {(metadata.length > 0 || fitMessage || (fit !== 'within' && alternatives.length > 0)) && (
        <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1">
          {metadata.length > 0 && (
            <div className="flex min-w-0 flex-wrap gap-1.5" aria-label="赛制建议">
              {metadata.map((label) => <Badge key={label} variant="outline">{label}</Badge>)}
            </div>
          )}
          {fitMessage && (
            <p className="flex min-w-0 items-center gap-1.5 text-xs leading-relaxed text-muted-foreground">
              <UsersRound aria-hidden="true" className="size-3.5 shrink-0" />
              <span className="min-w-0 break-words">{fitMessage}</span>
            </p>
          )}
          {fit !== 'within' && alternatives.length > 0 && (
            <p className="min-w-0 text-xs leading-relaxed text-foreground">
              更符合当前人数：{alternatives.map((item) => item.name).join('、')}。
            </p>
          )}
        </div>
      )}

      {estimate && effectiveSwissRounds.length > 0 && (
        <p className="flex min-w-0 items-center gap-1.5 text-xs leading-relaxed text-foreground">
          <ListOrdered aria-hidden="true" className="size-3.5 shrink-0 text-muted-foreground" />
          <span className="min-w-0 break-words">
            当前有效轮数：{effectiveSwissRounds.join('；')}。{frozen ? '已按发布名单冻结。' : '发布排期后按报名人数冻结。'}
          </span>
        </p>
      )}

      {/* 公平性数字、并发说明与风险警示并作一行流式排布；无估算时仍保留警示。 */}
      {(estimate || risk !== 'none' || hasUnboundedTiebreak) && (
        <div className="flex min-w-0 flex-wrap items-baseline gap-x-4 gap-y-1 text-xs">
          {estimate && (
            <dl className="flex min-w-0 flex-wrap items-baseline gap-x-4 gap-y-1">
              <div className="inline-flex min-w-0 items-baseline gap-1.5">
                <dt className="text-muted-foreground">基础对局记录</dt>
                <dd className="font-mono font-semibold tabular-nums text-foreground">
                  {estimate.estimated_matches == null ? '待估算' : `${estimate.estimated_matches} 场`}
                </dd>
              </div>
              <div className="inline-flex min-w-0 items-baseline gap-1.5">
                <dt className="text-muted-foreground">基础计分场</dt>
                <dd className="font-mono font-semibold tabular-nums text-foreground">
                  {scoringGames == null ? '待估算' : `${scoringGames} 场`}
                </dd>
              </div>
              <div className="inline-flex min-w-0 items-baseline gap-1.5">
                <dt className="text-muted-foreground">并发上限</dt>
                <dd className="font-mono font-semibold tabular-nums text-foreground">
                  {estimate.max_concurrent == null ? '按容量准入' : `${estimate.max_concurrent} 场`}
                </dd>
              </div>
              <div className="inline-flex min-w-0 items-baseline gap-1.5">
                <dt className="text-muted-foreground">基础 ETA</dt>
                <dd className="inline-flex items-baseline gap-1 font-mono font-semibold tabular-nums text-foreground">
                  <Clock3 aria-hidden="true" className="size-3.5 self-center text-muted-foreground" />
                  {formatContestDuration(estimate.eta_seconds)}
                </dd>
              </div>
              <div className="min-w-0 max-w-full text-xs leading-relaxed text-muted-foreground">
                并发数是代码槽位上限；每条任务的冻结 CPU、内存和 sandbox 向量可能降低实际并发并延长用时。
              </div>
            </dl>
          )}
          {(risk !== 'none' || hasUnboundedTiebreak) && (
            <p role="alert" className="flex min-w-0 items-start gap-1.5 text-xs leading-relaxed text-warning-foreground">
              <AlertTriangle aria-hidden="true" className="mt-0.5 size-3.5 shrink-0 text-warning" />
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
            </p>
          )}
        </div>
      )}
    </div>
  )
}
