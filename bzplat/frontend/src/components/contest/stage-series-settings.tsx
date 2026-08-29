import { Clock3, Scale, Swords } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import {
  formatContestDuration,
  projectStageSeriesEstimate,
  type StageSeriesConfig,
  type StageSeriesEstimate,
  type StageSeriesSetting,
  type StageSeriesSettings,
} from '@/components/contest/stage-series'

export * from '@/components/contest/stage-series'

function StageMetrics({ estimate }: { estimate?: StageSeriesEstimate }) {
  if (!estimate) {
    return <p className="text-xs leading-relaxed text-muted-foreground">报名人数确定后，详情页会实时估算计分场数与耗时。</p>
  }
  return (
    <dl className="grid min-w-0 grid-cols-2 gap-x-3 gap-y-2 text-xs sm:grid-cols-4">
      <div className="min-w-0">
        <dt className="text-muted-foreground">对手交锋</dt>
        <dd className="mt-0.5 font-mono font-semibold tabular-nums text-foreground">{estimate.conceptual_pairings} 组</dd>
      </div>
      <div className="min-w-0">
        <dt className="text-muted-foreground">计分场</dt>
        <dd className="mt-0.5 font-mono font-semibold tabular-nums text-foreground">{estimate.estimated_matches} 场</dd>
      </div>
      <div className="min-w-0">
        <dt className="text-muted-foreground">{estimate.effective_rounds != null ? '有效瑞士轮' : '执行计分场'}</dt>
        <dd className="mt-0.5 font-mono font-semibold tabular-nums text-foreground">
          {estimate.effective_rounds != null ? `${estimate.effective_rounds} 轮` : `${estimate.estimated_execution_legs} 场`}
        </dd>
      </div>
      <div className="min-w-0">
        <dt className="text-muted-foreground">预计耗时</dt>
        <dd className="mt-0.5 font-mono font-semibold tabular-nums text-foreground">{formatContestDuration(estimate.eta_seconds)}</dd>
      </div>
    </dl>
  )
}

interface StageSeriesSettingsEditorProps {
  configs: StageSeriesConfig[]
  value: StageSeriesSettings
  onChange?: (value: StageSeriesSettings) => void
  estimates?: StageSeriesEstimate[]
  disabled?: boolean
  frozen?: boolean
}

export function StageSeriesSettingsEditor({
  configs,
  value,
  onChange,
  estimates = [],
  disabled = false,
  frozen = false,
}: StageSeriesSettingsEditorProps) {
  const projected = configs.map((config) => projectStageSeriesEstimate(
    estimates.find((item) => item.stage_key === config.stage_key),
    value[config.stage_key],
  ))
  const totalSeconds = projected.reduce((sum, item) => sum + (item?.eta_seconds ?? 0), 0)
  const update = (stageKey: string, patch: Partial<StageSeriesSetting>) => {
    if (!onChange) return
    onChange({
      ...value,
      [stageKey]: { ...value[stageKey], ...patch },
    })
  }

  return (
    <div className="min-w-0">
      <div className="flex min-w-0 flex-wrap items-start justify-between gap-2 px-3 py-2.5">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <Scale aria-hidden="true" className="size-4 shrink-0 text-primary" />
            <h3 className="text-sm font-semibold text-foreground">逐阶段公平性</h3>
            {frozen && <Badge variant="outline">已冻结</Badge>}
          </div>
          <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
            同组多场会交替座位；瑞士轮额外轮数增加对手覆盖。发布排期后配置冻结。
          </p>
        </div>
        {totalSeconds > 0 && (
          <span className="inline-flex min-h-7 items-center gap-1.5 rounded-md bg-muted px-2 text-xs font-medium text-foreground">
            <Clock3 aria-hidden="true" className="size-3.5 text-muted-foreground" />总计 {formatContestDuration(totalSeconds)}
          </span>
        )}
      </div>
      <div className="divide-y border-t">
        {configs.map((config, index) => {
          const setting = value[config.stage_key] || {
            games_per_pair: config.games_per_pair.default,
            swiss_extra_rounds: config.swiss_extra_rounds?.default,
          }
          const estimate = projected[index]
          const gamesLabelId = `series-${config.stage_key}-games-label`
          const roundsLabelId = `series-${config.stage_key}-rounds-label`
          return (
            <fieldset key={config.stage_key} className="grid min-w-0 gap-3 px-3 py-3 lg:grid-cols-[minmax(10rem,0.65fr)_minmax(13rem,0.8fr)_minmax(18rem,1.5fr)] lg:items-center">
              <legend className="sr-only">{config.label}</legend>
              <div className="min-w-0">
                <p className="text-sm font-semibold text-foreground">{config.label}</p>
                <p className="mt-0.5 text-xs text-muted-foreground">
                  {estimate?.participant_count ? `${estimate.participant_count} 名选手` : '参赛人数待定'}
                </p>
              </div>
              <div className="grid min-w-0 gap-2 sm:grid-cols-2 lg:grid-cols-1 xl:grid-cols-2">
                <div className="min-w-0 space-y-1.5">
                  <span id={gamesLabelId} className="block text-xs font-medium text-muted-foreground">每对选手计分场数</span>
                  <Select
                    value={String(setting.games_per_pair)}
                    onValueChange={(next) => update(config.stage_key, { games_per_pair: Number(next) })}
                    disabled={disabled || frozen}
                  >
                    <SelectTrigger className="min-h-11 w-full" aria-labelledby={gamesLabelId}><SelectValue /></SelectTrigger>
                    <SelectContent>
                      {config.games_per_pair.allowed_values.map((count) => (
                        <SelectItem key={count} value={String(count)}>{count} 场计分</SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                {config.swiss_extra_rounds && (
                  <div className="min-w-0 space-y-1.5">
                    <span id={roundsLabelId} className="block text-xs font-medium text-muted-foreground">额外瑞士轮</span>
                    <Select
                      value={String(setting.swiss_extra_rounds ?? config.swiss_extra_rounds.default)}
                      onValueChange={(next) => update(config.stage_key, { swiss_extra_rounds: Number(next) })}
                      disabled={disabled || frozen}
                    >
                      <SelectTrigger className="min-h-11 w-full" aria-labelledby={roundsLabelId}><SelectValue /></SelectTrigger>
                      <SelectContent>
                        {Array.from(
                          { length: config.swiss_extra_rounds.max - config.swiss_extra_rounds.min + 1 },
                          (_, offset) => config.swiss_extra_rounds!.min + offset,
                        ).map((count) => (
                          <SelectItem key={count} value={String(count)}>{count === 0 ? '不增加' : `增加 ${count} 轮`}</SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                )}
              </div>
              <StageMetrics estimate={estimate} />
            </fieldset>
          )
        })}
      </div>
      {!frozen && (
        <p className="flex items-center gap-1.5 border-t px-3 py-2 text-xs text-muted-foreground">
          <Swords aria-hidden="true" className="size-3.5 shrink-0" />
          “对手交锋”是一对选手相遇一次；每场独立按胜 3 / 平 1 / 负 0 计入积分榜，K 场全部终结后再推进。
        </p>
      )}
    </div>
  )
}
