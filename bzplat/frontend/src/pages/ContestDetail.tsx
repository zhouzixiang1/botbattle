import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { Trophy, Users, Swords, ListOrdered, Play, DoorOpen, RefreshCw, Timer, ChevronDown, ChevronRight, Plus, Download, AlertTriangle, ArrowLeft, CalendarClock, Radio } from 'lucide-react'
import { DataRegion, PageFrame, PageHeader, StickyToolbar } from '@/components/layout'
import { MatchParticipants } from '@/components/MatchParticipants'
import { AdminContestRosterAssign } from '@/components/contest/AdminContestRosterAssign'
import { effectivePairingStatus, PairingResult } from '@/components/contest/pairing-result'
import {
  EliminationTiebreakStatus,
  type EliminationTiebreakProjection,
} from '@/components/contest/elimination-tiebreak-status'
import { Card } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs'
import {
  DataTable,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { EntityName, OverflowText } from '@/components/ui/overflow-text'
import { ErrorMsg, EmptyState, Loading, StatusBadge } from '@/components/ui/status'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import BracketTree from '@/components/contest/BracketTree'
import ScheduleTable from '@/components/contest/ScheduleTable'
import Countdown from '@/components/Countdown'
import { useConfirm } from '@/hooks/use-confirm'
import { useAuth } from '@/components/useAuth'
import Pagination from '@/components/Pagination'
import { apiGet, apiJson, errMsg } from '@/api'
import { findGame, gameLabel } from '@/lib/games'
import { fmtTime } from '@/lib/format'
import type { MatchParticipantSource } from '@/lib/match-participants'
import { outcomeParticipantStates, type PublicMatchOutcome } from '@/lib/match-outcome'
import { toast } from 'sonner'
import {
  StageSeriesSettingsEditor,
  defaultStageSeriesSettings,
  formatContestDuration,
  projectStageSeriesEstimate,
  sameStageSeriesSettings,
  stageSeriesDisplayLabel,
  stageSeriesSettingsValid,
  type ContestEstimate,
  type StageSeriesConfig,
  type StageSeriesSettings,
} from '@/components/contest/stage-series-settings'
import { TemplateGuidancePanel } from '@/components/contest/template-guidance-panel'
import {
  PAIRED_SWAP_TIEBREAK,
  estimatedScoringGames,
  templateHasUnboundedTiebreak,
  type ContestTemplateGuidance,
} from '@/components/contest/template-guidance'

const STAGE_TYPE_LABEL: Record<string, string> = {
  swiss: '瑞士轮',
  round_robin: '单循环',
  double_round_robin: '双循环',
  group_round_robin: '分组单循环',
  group_double_round_robin: '分组双循环',
  single_elimination: '单败淘汰',
}
const SCORING_LABEL: Record<string, string> = {
  poker_3_1_0: '胜 3 / 平 1 / 负 0',
  ccgc_2_1_0: '胜 2 / 平 1 / 负 0',
}

interface Contest {
  id: number
  title: string
  description?: string
  status: string
  organizer_id: number
  template_id?: string
  /** 可选：后端/模板表解析出的可读名 */
  template_name?: string
  game_id?: string
  stages_json?: string
  current_stage_idx?: number
  rest_ends_at?: string | null
  require_real_name?: number
  // 时间编排
  registration_opens_at?: string | null
  registration_closes_at?: string | null
  starts_at?: string | null
  ends_at?: string | null
  official_results_ready?: number
  showcase_key?: string | null
  games_per_pair?: number
  stage_series_settings?: StageSeriesSettings
}
interface Stage {
  /** Internal read-model sentinel for malformed historical stage JSON. */
  _display_invalid?: true
  key?: string
  type?: string
  scoring?: string
  rounds?: number
  group_count?: number
  advance_count?: number
  advance_per_group?: number
  rest_after_minutes?: number
  allow_bot_swap_in_rest?: boolean
  /** 复式交锋：每个运行对阵含两场同牌换座的独立计分场（仅 holdem）。 */
  duplicate?: boolean
  series_scoring?: string | null
  games_per_pair?: number
  swiss_extra_rounds?: number
  effective_rounds?: number
  swiss_round_bands?: Array<{
    min_participants: number
    max_participants: number | null
    rounds: number
  }>
  ranking_mode?: string
  ranking_scope?: number
  round_stagger_minutes?: number
  allow_large_round_robin?: boolean
  tiebreak?: string
}

type StageDisplayContract = 'plain' | 'independent' | 'aggregate' | 'invalid'

const STAGE_TYPES = new Set([
  'round_robin',
  'double_round_robin',
  'group_round_robin',
  'group_double_round_robin',
  'swiss',
  'single_elimination',
])
const PAIR_SERIES_STAGE_TYPES = new Set(['round_robin', 'double_round_robin', 'swiss'])
const COMMON_STAGE_FIELDS = new Set([
  'key', 'type', 'scoring', 'advance_count', 'rest_after_minutes',
  'allow_bot_swap_in_rest', 'round_stagger_minutes',
])
const STAGE_TYPE_FIELDS: Record<string, Set<string>> = {
  round_robin: new Set(['duplicate', 'allow_large_round_robin', 'games_per_pair', 'series_scoring']),
  double_round_robin: new Set(['duplicate', 'allow_large_round_robin', 'ranking_mode', 'ranking_scope', 'games_per_pair', 'series_scoring']),
  group_round_robin: new Set(['group_count', 'advance_per_group']),
  group_double_round_robin: new Set(['group_count', 'advance_per_group']),
  swiss: new Set(['rounds', 'duplicate', 'games_per_pair', 'series_scoring', 'swiss_extra_rounds', 'effective_rounds', 'swiss_round_bands']),
  single_elimination: new Set(['tiebreak']),
}
const DEFAULT_SCORING_BY_GAME: Record<string, string> = {
  holdem: 'poker_3_1_0',
  gomoku: 'ccgc_2_1_0',
  pencil: 'ccgc_2_1_0',
}

function hasOwn(source: object, key: string): boolean {
  return Object.prototype.hasOwnProperty.call(source, key)
}

function exactIntegerAtLeast(value: unknown, minimum: number): boolean {
  return typeof value === 'number' && Number.isInteger(value) && value >= minimum
}

function swissRoundBandsValid(value: unknown): boolean {
  if (!Array.isArray(value) || value.length === 0) return false
  let previousMaximum: number | null = null
  for (const [index, band] of value.entries()) {
    if (!band || typeof band !== 'object' || Array.isArray(band)) return false
    const row = band as Record<string, unknown>
    if (
      Object.keys(row).length !== 3
      || !hasOwn(row, 'min_participants')
      || !hasOwn(row, 'max_participants')
      || !hasOwn(row, 'rounds')
      || !exactIntegerAtLeast(row.min_participants, 1)
      || !exactIntegerAtLeast(row.rounds, 1)
      || (row.max_participants !== null && !exactIntegerAtLeast(row.max_participants, Number(row.min_participants)))
    ) return false
    if (index > 0 && (previousMaximum === null || Number(row.min_participants) <= previousMaximum)) return false
    previousMaximum = row.max_participants as number | null
  }
  return true
}

/** Mirror the backend's frozen read contract before enabling irreversible UI actions. */
function stageDisplayContract(
  stage: Stage | undefined,
  gameId?: string,
  contestStatus?: string,
): StageDisplayContract {
  if (!stage || stage._display_invalid) return 'invalid'
  if (hasOwn(stage, 'duplicate') && typeof stage.duplicate !== 'boolean') return 'invalid'
  if (stage.duplicate === true && gameId !== 'holdem') return 'invalid'

  const type = stage.type
  if (type !== undefined && (typeof type !== 'string' || !STAGE_TYPES.has(type))) return 'invalid'
  const expectedScoring = gameId ? DEFAULT_SCORING_BY_GAME[gameId] : undefined
  if (gameId && !expectedScoring) return 'invalid'
  if (hasOwn(stage, 'scoring') && (
    typeof stage.scoring !== 'string'
    || (expectedScoring !== undefined && stage.scoring !== expectedScoring)
  )) return 'invalid'

  const exactIntegers: Array<[keyof Stage, number]> = [
    ['rounds', 0],
    ['effective_rounds', 1],
    ['swiss_extra_rounds', 0],
    ['advance_count', 1],
    ['advance_per_group', 1],
    ['group_count', 1],
    ['ranking_scope', 1],
    ['rest_after_minutes', 0],
    ['round_stagger_minutes', 0],
  ]
  if (exactIntegers.some(([key, minimum]) => (
    hasOwn(stage, key) && !exactIntegerAtLeast(stage[key], minimum)
  ))) return 'invalid'
  if (hasOwn(stage, 'ranking_mode') && stage.ranking_mode !== 'replace_top') return 'invalid'
  if (hasOwn(stage, 'ranking_scope') && stage.ranking_mode !== 'replace_top') return 'invalid'
  if (hasOwn(stage, 'allow_bot_swap_in_rest') && typeof stage.allow_bot_swap_in_rest !== 'boolean') return 'invalid'
  if (hasOwn(stage, 'allow_large_round_robin') && typeof stage.allow_large_round_robin !== 'boolean') return 'invalid'
  if (hasOwn(stage, 'tiebreak') && (
    type !== 'single_elimination' || stage.tiebreak !== PAIRED_SWAP_TIEBREAK
  )) return 'invalid'
  if (hasOwn(stage, 'swiss_round_bands') && (
    type !== 'swiss'
    || !hasOwn(stage, 'rounds')
    || !exactIntegerAtLeast(stage.rounds, 0)
    || !swissRoundBandsValid(stage.swiss_round_bands)
  )) return 'invalid'
  if (type !== 'swiss' && ['rounds', 'effective_rounds', 'swiss_extra_rounds', 'swiss_round_bands'].some((key) => hasOwn(stage, key))) {
    return 'invalid'
  }
  const hasEffectiveRounds = hasOwn(stage, 'effective_rounds')
  const hasDynamicRoundStrategy = hasOwn(stage, 'swiss_round_bands') || hasOwn(stage, 'swiss_extra_rounds')
  if (
    contestStatus !== 'draft'
    && contestStatus !== 'open'
    && hasDynamicRoundStrategy
    && !hasEffectiveRounds
  ) return 'invalid'

  const hasGames = hasOwn(stage, 'games_per_pair')
  if (hasGames && !exactIntegerAtLeast(stage.games_per_pair, 1)) return 'invalid'
  if (hasGames && (!type || !PAIR_SERIES_STAGE_TYPES.has(type))) return 'invalid'
  if (hasGames && (gameId !== 'holdem' || Number(stage.games_per_pair) > 10)) return 'invalid'
  if (type === 'swiss' && hasGames) {
    const games = Number(stage.games_per_pair)
    if (games !== 1 && (games < 2 || games % 2 !== 0)) return 'invalid'
  }

  const hasSeriesMarker = hasOwn(stage, 'series_scoring')
  if (!hasSeriesMarker) return 'plain'
  if (stage.series_scoring !== 'independent_scoring_game_points_v1'
    && stage.series_scoring !== 'aggregate_match_points_v1') return 'invalid'
  if (!hasGames) return 'invalid'
  // An explicit marker is a frozen read contract, including the legacy
  // aggregate marker.  Pre-marker history stays on the permissive `plain`
  // path above; once a marker exists, never fill missing schema fields or
  // accept fields that are illegal for the declared stage type.
  if (
    typeof type !== 'string' || !STAGE_TYPES.has(type)
    || typeof stage.scoring !== 'string' || stage.scoring !== expectedScoring
  ) return 'invalid'
  // `rounds=0` freezes Swiss automatic-round semantics.  Once a scoring
  // marker exists, an omitted field is damaged history, not permission to
  // recompute a different round plan from today's participant count.
  if (type === 'swiss' && !hasOwn(stage, 'rounds')) return 'invalid'
  const allowed = new Set([...COMMON_STAGE_FIELDS, ...(STAGE_TYPE_FIELDS[type] || [])])
  if (Object.keys(stage).some((key) => key !== '_display_invalid' && !allowed.has(key))) return 'invalid'
  return stage.series_scoring === 'aggregate_match_points_v1' ? 'aggregate' : 'independent'
}
interface Entry {
  id: number
  user_id: number
  bot_id: number | null
  registered_at?: string
  group_id?: string
  seed?: number
  eliminated?: number
  bot_name?: string
  bot_display?: string
  owner_name?: string
  owner_display?: string
  // 实名信息（仅组织者可见——后端对非组织者脱敏剔除）
  real_name?: string
  phone?: string
  school?: string
  student_id?: string
  identity_source?: 'registration_profile' | 'current_profile_legacy'
  identity_captured_at?: string | null
}

const EXPORT_LINK_CLASS = 'max-sm:h-auto max-sm:min-h-[44px] max-sm:w-full max-sm:whitespace-normal max-sm:py-2 max-sm:text-center max-sm:leading-snug'

function identitySourceLabel(source: Entry['identity_source']): string {
  if (source === 'registration_profile') return '报名时资料快照'
  if (source === 'current_profile_legacy') return '历史报名：当前资料回退（非快照）'
  return '报名资料'
}
interface Pairing extends MatchParticipantSource {
  id: number
  round_num?: number
  bracket_slot?: number | null
  bot_a_id: number | null
  bot_b_id: number | null
  is_bye?: boolean
  match_id?: string | null
  status?: string
  display_status?: string | null
  stage_idx?: number
  stage_key?: string
  group_id?: string
  match_winner?: number | null
  outcome?: PublicMatchOutcome | null
  scheduled_at?: string | null
  series_index?: number | null
  series_size?: number | null
  tiebreak_group?: number | null
  tiebreak_game?: number | null
}
interface Standing {
  bot_id: number | null
  points: number
  wins: number
  draws: number
  losses: number
  byes: number
  delta_total: number
  group_id?: string
  bot_name?: string
  counts?: ContestStandingCounts
}
interface ContestStandingCounts {
  unique_opponents?: number
  encounter_groups: number
  match_jobs: number
  scoring_games: number
}
interface StageContestCounts {
  encounter_groups?: { completed: number; total: number }
  match_jobs?: { completed: number; total: number }
  scoring_games?: { completed: number; planned: number; terminal_unplayed: number }
}
interface StageStandingRow {
  entry_id: number
  bot_id?: number | null
  bot_name?: string
  owner_name?: string
  owner_display?: string
  points: number
  wins: number
  draws: number
  losses: number
  byes: number
  delta_total: number
  group_id?: string
  rank: number
  advancement?: 'advanced' | 'in_zone' | 'eliminated' | 'outside_zone' | null
  counts?: ContestStandingCounts
}
interface StageStandingSummary {
  stage_idx: number
  stage_key: string
  status: string
  source: 'persisted' | 'live' | 'scheduled' | 'pending'
  completed_pairings: number
  total_pairings: number
  advancement_final: boolean
  counts?: StageContestCounts
  elimination_tiebreak?: EliminationTiebreakProjection | null
  rows: StageStandingRow[]
}
interface OfficialResult {
  rank: number
  entry_id: number
  bot_id?: number | null
  user_id?: number | null
  points: number
  bot_name?: string
  bot_display?: string
  owner_name?: string
  owner_display?: string
  awarded?: string
  source_stage?: number
  ranking_cohort?: string
  tiebreaks?: {
    points?: number
    buchholz_cut1?: number
    sonneborn_berger?: number
    head_to_head?: number
    normalized_delta?: number
    technical_losses?: number
    seed?: number
  }
}

function parseStages(c: Contest | null): Stage[] {
  if (!c?.stages_json) return []
  try {
    const parsed: unknown = JSON.parse(c.stages_json)
    if (!Array.isArray(parsed)) return [{ _display_invalid: true }]
    return parsed.map((stage) => (
      stage != null && typeof stage === 'object' && !Array.isArray(stage)
        ? stage as Stage
        : { _display_invalid: true }
    ))
  } catch {
    return [{ _display_invalid: true }]
  }
}

function matchesTemplateStageTopology(
  templateStages: Stage[] | undefined,
  contestStages: Stage[],
): boolean {
  if (!Array.isArray(templateStages) || templateStages.length !== contestStages.length) return false
  const configurable = new Set([
    'games_per_pair',
    'series_scoring',
    'swiss_extra_rounds',
    'effective_rounds',
  ])
  const projection = (stage: Stage | undefined) => {
    if (!stage || stage._display_invalid) return null
    return JSON.stringify(Object.fromEntries(
      Object.entries(stage)
        .filter(([key]) => !configurable.has(key) && key !== '_display_invalid')
        .sort(([left], [right]) => left.localeCompare(right)),
    ))
  }
  return templateStages.every((stage, index) => (
    projection(stage) === projection(contestStages[index])
  ))
}

function persistedSeriesSettings(c: Contest): StageSeriesSettings {
  if (c.stage_series_settings && Object.keys(c.stage_series_settings).length > 0) {
    return c.stage_series_settings
  }
  // Progressive rollout safety: old detail projections may omit the derived
  // map while the frozen stage snapshot already contains the authoritative
  // values. Only the two stage-configurable templates use this editor fallback;
  // scalar RR/duplicate-RR stages keep their unit-aware summary and are read
  // directly by the publish confirmation below.
  if (c.template_id !== 'holdem_prelim_swiss' && c.template_id !== 'holdem_final_ranked') {
    return {}
  }
  const out: StageSeriesSettings = {}
  for (const [index, stage] of parseStages(c).entries()) {
    if (!Number.isInteger(stage.games_per_pair)) continue
    const stageKey = stage.key || `stage${index + 1}`
    out[stageKey] = {
      games_per_pair: Number(stage.games_per_pair),
      ...(Number.isInteger(stage.swiss_extra_rounds)
        ? { swiss_extra_rounds: Number(stage.swiss_extra_rounds) }
        : {}),
    }
  }
  return out
}

function stageSeriesLabel(stageKey: string, stages: Stage[]): string {
  const stage = stages.find((item) => item.key === stageKey)
  return stageSeriesDisplayLabel(
    stageKey,
    STAGE_TYPE_LABEL[stage?.type || ''] || stageKey,
  )
}

function contestPairingSeriesKey(pairing: Pairing, stageType?: string): string {
  if (pairing.is_bye || !pairing.series_size || pairing.series_size <= 1) return `match:${pairing.id}`
  const players = [
    pairing.bot_a_id ?? pairing.owner_a_name ?? pairing.bot_a_name ?? `unknown-a-${pairing.id}`,
    pairing.bot_b_id ?? pairing.owner_b_name ?? pairing.bot_b_name ?? `unknown-b-${pairing.id}`,
  ].sort().join(':')
  const round = stageType === 'swiss' ? pairing.round_num ?? 1 : 0
  return `${pairing.stage_key || pairing.stage_idx || 0}:${round}:${pairing.group_id || ''}:${players}`
}

function scheduleIssue(c: Contest): string {
  const opens = c.registration_opens_at ? new Date(c.registration_opens_at).getTime() : null
  const closes = c.registration_closes_at ? new Date(c.registration_closes_at).getTime() : null
  const starts = c.starts_at ? new Date(c.starts_at).getTime() : null
  if (opens != null && closes != null && opens > closes) return '开放报名时间晚于报名截止时间'
  if (closes != null && starts != null && closes > starts) return '报名截止时间晚于比赛开始时间'
  if (opens != null && starts != null && opens > starts) return '开放报名时间晚于比赛开始时间'
  return ''
}

/** 状态相关的时间编排提示（报名窗口/开赛/rest 倒计时） */
function ContestScheduleInfo({ c }: { c: Contest }) {
  const now = Date.now()
  const items: { label: string; time?: string | null }[] = []
  if (c.registration_opens_at) items.push({ label: '开放报名', time: c.registration_opens_at })
  if (c.registration_closes_at) items.push({ label: '报名截止', time: c.registration_closes_at })
  if (c.starts_at) items.push({ label: '比赛开始', time: c.starts_at })
  if (c.ends_at) items.push({ label: '比赛结束', time: c.ends_at })
  if (items.length === 0) return null
  return (
    <div
      role="region"
      aria-label="赛事时间安排"
      className="flex min-w-0 flex-wrap items-center gap-x-4 gap-y-1 border-t pt-2 text-xs"
    >
      <CalendarClock aria-hidden="true" className="size-3.5 shrink-0 text-muted-foreground" />
      {items.map((it) => {
        const future = it.time && new Date(it.time).getTime() > now
        return (
          <span key={it.label} className="inline-flex min-w-0 flex-wrap items-center gap-1">
            <span className="text-muted-foreground">{it.label}</span>
            <time className="font-mono font-medium tabular-nums text-foreground">
              {it.time ? fmtTime(it.time) : '—'}
            </time>
            {future && it.time && <Countdown endsAt={it.time} className="text-primary" />}
          </span>
        )
      })}
    </div>
  )
}

const TIEBREAK_NUMBER = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 })

function scoreBreakdown({
  wins,
  draws,
  losses,
  byes,
}: Pick<Standing, 'wins' | 'draws' | 'losses' | 'byes'>) {
  return `${wins} 胜 / ${draws} 平 / ${losses} 负 · 轮空 ${byes || 0}`
}

function scoringCountBreakdown(
  row: Pick<Standing, 'wins' | 'draws' | 'losses' | 'counts'>,
  duplicate: boolean,
  legacyAggregate = false,
): string {
  const scoringGames = row.counts?.scoring_games ?? row.wins + row.draws + row.losses
  const opponents = row.counts?.unique_opponents
  const matchJobs = row.counts?.match_jobs
  if (legacyAggregate) {
    return [
      opponents == null ? null : `面对 ${opponents} 位对手`,
      matchJobs == null ? null : `${matchJobs} 场历史系列对局`,
      `${scoringGames} 次旧版系列结算`,
    ].filter(Boolean).join(' · ')
  }
  if (!duplicate) {
    return [
      opponents == null ? null : `面对 ${opponents} 位对手`,
      matchJobs == null ? null : `${matchJobs} 条对局记录`,
      `${scoringGames} 场计分`,
    ].filter(Boolean).join(' · ')
  }
  return [
    opponents == null ? null : `面对 ${opponents} 位对手`,
    matchJobs == null ? null : `${matchJobs} 组复式交锋`,
    `${scoringGames} 场计分`,
  ].filter(Boolean).join(' · ')
}

function stageStandingProgressLabel(
  summary: StageStandingSummary,
  duplicate: boolean,
  legacyAggregate = false,
): string {
  const encounterGroups = summary.counts?.encounter_groups
  const matchJobs = summary.counts?.match_jobs
  const scoringGames = summary.counts?.scoring_games
  if (matchJobs) {
    const encounterLabel = encounterGroups
      ? `${encounterGroups.completed}/${encounterGroups.total} 个对手系列`
      : null
    if (legacyAggregate) {
      return [
        encounterLabel,
        `${matchJobs.completed}/${matchJobs.total} 场历史系列对局`,
        scoringGames ? `${scoringGames.completed}/${scoringGames.planned} 次旧版系列结算` : null,
      ].filter(Boolean).join(' · ')
    }
    return [
      encounterLabel,
      duplicate
        ? `${matchJobs.completed}/${matchJobs.total} 组复式交锋`
        : `${matchJobs.completed}/${matchJobs.total} 条对局记录`,
      scoringGames
        ? `${scoringGames.completed}/${scoringGames.planned} 场计分`
        : duplicate
          ? null
          : `${matchJobs.completed}/${matchJobs.total} 场计分`,
    ].filter(Boolean).join(' · ')
  }
  if (legacyAggregate) return `${summary.completed_pairings}/${summary.total_pairings} 场历史系列对局`
  return duplicate
    ? `${summary.completed_pairings}/${summary.total_pairings} 组复式交锋`
    : `${summary.completed_pairings}/${summary.total_pairings} 场计分`
}

function OfficialTiebreakDetail({
  result,
  hasPointTie,
  sourceLabel,
}: {
  result: OfficialResult
  hasPointTie: boolean
  sourceLabel?: string | null
}) {
  if (!hasPointTie) {
    return (
      <span className="text-xs text-muted-foreground">
        {sourceLabel ? `${sourceLabel}内名次已确定` : '积分已区分'}
      </span>
    )
  }
  const tiebreaks = result.tiebreaks
  if (!tiebreaks) return <span className="text-xs text-warning">破同分明细缺失</span>
  const values: string[] = []
  if (typeof tiebreaks.buchholz_cut1 === 'number') values.push(`对手分 Cut1 ${TIEBREAK_NUMBER.format(tiebreaks.buchholz_cut1)}`)
  if (typeof tiebreaks.sonneborn_berger === 'number') values.push(`胜者分 SB ${TIEBREAK_NUMBER.format(tiebreaks.sonneborn_berger)}`)
  if (typeof tiebreaks.head_to_head === 'number') values.push(`直接交手 ${TIEBREAK_NUMBER.format(tiebreaks.head_to_head * 100)}%`)
  if (typeof tiebreaks.normalized_delta === 'number') values.push(`归一分差 ${TIEBREAK_NUMBER.format(tiebreaks.normalized_delta)}`)
  if (typeof tiebreaks.technical_losses === 'number') values.push(`技术负 ${tiebreaks.technical_losses}`)
  if (typeof tiebreaks.seed === 'number') values.push(`报名序 ${tiebreaks.seed}`)
  return (
    <span className="block min-w-0 whitespace-normal text-xs leading-relaxed text-muted-foreground">
      {values.length > 0 ? values.join(' · ') : '破同分明细缺失'}
    </span>
  )
}

export default function ContestDetail() {
  const { id } = useParams()
  const { user, isLoggedIn } = useAuth()
  const [contest, setContest] = useState<Contest | null>(null)
  const [entries, setEntries] = useState<Entry[]>([])
  const [pairings, setPairings] = useState<Pairing[]>([])
  const [standings, setStandings] = useState<Standing[]>([])
  const [stageStandings, setStageStandings] = useState<StageStandingSummary[]>([])
  const [officialResults, setOfficialResults] = useState<OfficialResult[]>([])
  const [serverIsOrganizer, setServerIsOrganizer] = useState(false)
  const [myEntry, setMyEntry] = useState<Entry | null>(null)
  const [estimate, setEstimate] = useState<ContestEstimate | null>(null)
  const [templateCatalog, setTemplateCatalog] = useState<ContestTemplateGuidance[]>([])
  const [stageSeriesConfigs, setStageSeriesConfigs] = useState<StageSeriesConfig[]>([])
  const [savedStageSeriesSettings, setSavedStageSeriesSettings] = useState<StageSeriesSettings>({})
  const [draftStageSeriesSettings, setDraftStageSeriesSettings] = useState<StageSeriesSettings>({})
  const [seriesSettingsError, setSeriesSettingsError] = useState('')
  const [seriesSettingsNotice, setSeriesSettingsNotice] = useState('')
  const [bots, setBots] = useState<Array<{ id: number; name: string; display_name?: string }>>([])
  const [botId, setBotId] = useState('')
  const [stageTab, setStageTab] = useState(0)
  // 对阵视图：'tree'（对阵树）/ 'table'（一览表）。淘汰赛默认 tree，其余默认 table，可手动切换。
  const [pairingView, setPairingView] = useState<'tree' | 'table'>('tree')
  const [error, setError] = useState('')
  const [busyAction, setBusyAction] = useState(false)
  const actionLockRef = useRef(false)
  const activeContestIdRef = useRef<string | undefined>(id)
  const loadGenerationRef = useRef(0)
  const lastLoadedStatusRef = useRef<string | null>(null)
  const [confirm, confirmDialog, cancelConfirm] = useConfirm()
  // Params can change while this component instance is reused. Keep the authority
  // ref current during render, before effects run, so an old async handler cannot
  // act in the render-to-effect window of the next contest.
  activeContestIdRef.current = id
  // 报名列表分页（115 人赛事场景：服务端分页，避免一次性渲染全部）
  const [entriesPage, setEntriesPage] = useState(1)
  const [entriesTotal, setEntriesTotal] = useState(0)
  const entriesPerPage = 20
  // 内容区按生命周期显示：报名期优先选手，赛中优先对阵，完赛优先正式名次。
  const [contentTab, setContentTab] = useState<'matchups' | 'entries' | 'standings' | 'official'>('entries')
  // 积分榜客户端分页（量级通常 < 200，客户端 slice 足够；每页 30 行）
  const [standingsPage, setStandingsPage] = useState(1)
  const standingsPerPage = 30

  const stages = useMemo(() => parseStages(contest), [contest])
  const displayStageSeriesConfigs = useMemo<StageSeriesConfig[]>(() => {
    if (stageSeriesConfigs.length > 0) return stageSeriesConfigs
    return Object.entries(savedStageSeriesSettings).map(([stageKey, setting]) => ({
      stage_key: stageKey,
      label: stageSeriesLabel(stageKey, stages),
      games_per_pair: {
        default: setting.games_per_pair,
        allowed_values: [setting.games_per_pair],
      },
      ...(setting.swiss_extra_rounds != null
        ? { swiss_extra_rounds: {
            default: setting.swiss_extra_rounds,
            min: setting.swiss_extra_rounds,
            max: setting.swiss_extra_rounds,
          } }
        : {}),
    }))
  }, [savedStageSeriesSettings, stageSeriesConfigs, stages])
  const seriesSettingsDirty = stageSeriesConfigs.length > 0 && !sameStageSeriesSettings(
    stageSeriesConfigs,
    savedStageSeriesSettings,
    draftStageSeriesSettings,
  )
  const seriesSettingsReady = stageSeriesConfigs.length === 0 || stageSeriesSettingsValid(
    stageSeriesConfigs,
    draftStageSeriesSettings,
  )
  const projectedStageEstimates = useMemo(() => (
    (estimate?.stages || []).map((stageEstimate) => projectStageSeriesEstimate(
      stageEstimate,
      draftStageSeriesSettings[stageEstimate.stage_key] || savedStageSeriesSettings[stageEstimate.stage_key],
    ) || stageEstimate)
  ), [draftStageSeriesSettings, estimate?.stages, savedStageSeriesSettings])
  const projectedEstimatedMatches = projectedStageEstimates.length > 0
    ? projectedStageEstimates.reduce((sum, stage) => sum + stage.estimated_matches, 0)
    : estimate?.estimated_matches
  const projectedEstimatedScoringGames = projectedStageEstimates.length > 0
    ? projectedStageEstimates.reduce((sum, stage) => sum + stage.estimated_execution_legs, 0)
    : estimatedScoringGames(estimate)
  const projectedEtaSeconds = projectedStageEstimates.length > 0
    ? projectedStageEstimates.reduce((sum, stage) => sum + stage.eta_seconds, 0)
    : estimate?.eta_seconds
  const selectedTemplateGuidance = templateCatalog.find((template) => (
    template.id === contest?.template_id
  ))
  const hasUnboundedTiebreak = templateHasUnboundedTiebreak({ stages })
  const projectedEstimate: ContestEstimate | null = estimate
    ? {
        ...estimate,
        estimated_matches: projectedEstimatedMatches,
        estimated_scoring_games: projectedEstimatedScoringGames,
        eta_seconds: projectedEtaSeconds,
        stages: projectedStageEstimates,
      }
    : null

  // 赛制提示只读取赛事已冻结的阶段配置。不从模板 id 或数量反推新口径。
  // 单条对阵的赛果必须另外读取后端 outcome，不得根据 match_winner=null 推断。
  const stageContracts = useMemo(
    () => stages.map((stage) => stageDisplayContract(stage, contest?.game_id, contest?.status)),
    [contest?.game_id, contest?.status, stages],
  )
  const hasInvalidStageContract = stageContracts.includes('invalid')
  const isDuplicate = useMemo(
    () => stages.some((stage, index) => (
      stageContracts[index] !== 'invalid' && stage.duplicate === true
    )),
    [stageContracts, stages],
  )
  const hasLegacyAggregateStage = useMemo(
    () => stageContracts.includes('aggregate'),
    [stageContracts],
  )
  const hasIndependentSingleStage = useMemo(
    () => stages.some((stage, index) => (
      (stageContracts[index] === 'plain' || stageContracts[index] === 'independent')
      && stage.duplicate !== true
    )),
    [stageContracts, stages],
  )
  const overviewScheduleKinds = Number(isDuplicate) + Number(hasLegacyAggregateStage) + Number(hasIndependentSingleStage)
  const overviewScheduleLabel = hasInvalidStageContract
    ? '赛制配置'
    : overviewScheduleKinds > 1
    ? '对阵任务'
    : hasLegacyAggregateStage
      ? '历史系列对局'
      : isDuplicate
        ? '复式交锋组'
        : '计分场'
  const overviewScheduleSuffix = hasInvalidStageContract
    ? ''
    : overviewScheduleKinds > 1
    ? '项'
    : hasLegacyAggregateStage || !isDuplicate
      ? '场'
      : '组'
  const overviewScheduleMeasure = hasInvalidStageContract
    ? '项对局记录'
    : overviewScheduleKinds > 1
    ? '项对阵任务'
    : hasLegacyAggregateStage
      ? '场历史系列对局'
      : isDuplicate
        ? '组复式交锋'
        : '场计分'

  const load = useCallback(async () => {
    if (!id) return Promise.resolve()
    const targetId = id
    const targetEntriesPage = entriesPage
    const generation = ++loadGenerationRef.current
    try {
      const d = await apiGet<{
        contest: Contest
        entries: Entry[]
        pairings: Pairing[]
        standings: Standing[]
        stage_standings: StageStandingSummary[]
        estimate?: ContestEstimate
        entries_page?: number
        entries_per_page?: number
        entries_total?: number
        my_entry?: Entry | null
        is_organizer?: boolean
      }>(`/api/contests/${targetId}?entries_page=${targetEntriesPage}&entries_per_page=${entriesPerPage}`)
      let nextStageSeriesConfigs: StageSeriesConfig[] = []
      let nextTemplateCatalog: ContestTemplateGuidance[] = []
      let nextSeriesSettingsError = ''
      let nextSeriesSettingsNotice = ''
      if (
        d.is_organizer === true &&
        (d.contest.status === 'draft' || d.contest.status === 'open') &&
        d.contest.game_id && d.contest.template_id
      ) {
        try {
          const templateResponse = await apiGet<{ templates: Array<ContestTemplateGuidance & {
            stages?: Stage[]
            stage_series_configs?: StageSeriesConfig[]
          }> }>(`/api/contests/templates?game=${encodeURIComponent(d.contest.game_id)}`)
          nextTemplateCatalog = templateResponse.templates || []
          const matchedTemplate = templateResponse.templates
            .find((template) => template.id === d.contest.template_id)
          const topologyMatches = matchesTemplateStageTopology(
            matchedTemplate?.stages,
            parseStages(d.contest),
          )
          nextStageSeriesConfigs = topologyMatches
            ? matchedTemplate?.stage_series_configs || []
            : []
          if (!topologyMatches && (matchedTemplate?.stage_series_configs?.length ?? 0) > 0) {
            nextSeriesSettingsNotice = '冻结阶段拓扑与内置模板不一致，已停用公平性设置编辑；发布时将保留当前冻结阶段。'
          }
        } catch (cause) {
          nextSeriesSettingsError = `公平性配置加载失败：${errMsg(cause)}`
        }
      }
      let nextOfficialResults: OfficialResult[] = []
      let officialResultsError = ''
      if (d.contest.status === 'finished') {
        try {
          const official = await apiGet<{ results: OfficialResult[] }>(
            `/api/contests/${targetId}/official-results`,
          )
          nextOfficialResults = official.results || []
        } catch (e) {
          officialResultsError = `正式名次加载失败：${errMsg(e)}`
        }
      }
      if (
        activeContestIdRef.current !== targetId ||
        loadGenerationRef.current !== generation
      ) return
      setContest(d.contest)
      setEntries(d.entries || [])
      setPairings(d.pairings || [])
      setStandings(d.standings || [])
      setStageStandings(d.stage_standings || [])
      setOfficialResults(nextOfficialResults)
      setEstimate(d.estimate || null)
      setTemplateCatalog(nextTemplateCatalog)
      const persistedSettings = persistedSeriesSettings(d.contest)
      setStageSeriesConfigs(nextStageSeriesConfigs)
      setSavedStageSeriesSettings(persistedSettings)
      setDraftStageSeriesSettings(nextStageSeriesConfigs.length > 0
        ? defaultStageSeriesSettings(nextStageSeriesConfigs, persistedSettings)
        : persistedSettings)
      setSeriesSettingsError(nextSeriesSettingsError)
      setSeriesSettingsNotice(nextSeriesSettingsNotice)
      setStageTab(d.contest.current_stage_idx ?? 0)
      setEntriesTotal(d.entries_total ?? d.entries.length)
      setMyEntry(d.my_entry ?? null)
      setServerIsOrganizer(d.is_organizer === true)
      const status = d.contest.status
      const previousStatus = lastLoadedStatusRef.current
      if (previousStatus === null || previousStatus !== status) {
        setContentTab(
          status === 'finished'
            ? 'official'
            : (status === 'published' || status === 'running' || status === 'rest')
              ? 'matchups'
              : 'entries',
        )
      }
      lastLoadedStatusRef.current = status
      setError(officialResultsError)
    } catch (e) {
      if (
        activeContestIdRef.current === targetId &&
        loadGenerationRef.current === generation
      ) setError(errMsg(e))
    }
  }, [id, entriesPage, entriesPerPage])

  // React Router reuses this component for /contests/A → /contests/B. Invalidate
  // every A request/action before B can render, and clear A's privileged controls.
  useEffect(() => {
    // Resolve an outstanding confirmation as cancelled. Without this, useConfirm's
    // state survives the reused route component and the old destructive dialog can
    // reappear after the next contest finishes loading.
    cancelConfirm()
    activeContestIdRef.current = id
    loadGenerationRef.current += 1
    setContest(null)
    setEntries([])
    setPairings([])
    setStandings([])
    setStageStandings([])
    setOfficialResults([])
    setServerIsOrganizer(false)
    setMyEntry(null)
    setEstimate(null)
    setTemplateCatalog([])
    setStageSeriesConfigs([])
    setSavedStageSeriesSettings({})
    setDraftStageSeriesSettings({})
    setSeriesSettingsError('')
    setBots([])
    setBotId('')
    setError('')
    setEntriesPage(1)
    setStageTab(0)
    setStandingsPage(1)
    setContentTab('entries')
    lastLoadedStatusRef.current = null
    actionLockRef.current = false
    setBusyAction(false)
    return () => {
      if (activeContestIdRef.current === id) activeContestIdRef.current = undefined
      loadGenerationRef.current += 1
    }
  }, [id, cancelConfirm])

  useEffect(() => {
    void load()
  }, [load])

  useEffect(() => {
    let cancelled = false
    const targetId = id
    if (isLoggedIn && contest?.game_id && !contest.showcase_key) {
      apiGet<{ bots: Array<{ id: number; name: string; display_name?: string }> }>(
        `/api/bots/mine?game_id=${contest.game_id}`,
      )
        .then((d) => {
          if (!cancelled && activeContestIdRef.current === targetId) {
            setBots(d.bots || [])
          }
        })
        .catch(() => undefined)
    }
    return () => { cancelled = true }
  }, [id, isLoggedIn, contest?.game_id, contest?.showcase_key])

  const isOrg = !!user && !!contest && (user.role === 'admin' || user.id === contest.organizer_id)
  const canExportIdentity = Boolean(contest?.require_real_name && serverIsOrganizer)
  const canAssignEntries = Boolean(
    contest && serverIsOrganizer && (!contest.require_real_name || user?.role === 'admin'),
  )
  // myEntry 来自后端 my_entry 字段（不分页，休息换 Bot UI 依赖；entries 分页后前端 find 不可靠）
  // 实名校验：赛事要求实名且当前用户未填完整 → 提示去设置页补填
  const needsRealName = !!contest?.require_real_name && !!user && !(
    user.real_name && user.phone && user.school && user.student_id
  )

  const act = async (path: string, body?: unknown, okMsg?: string) => {
    const targetId = id
    if (!targetId || activeContestIdRef.current !== targetId || actionLockRef.current) return
    actionLockRef.current = true
    setBusyAction(true)
    setError('')
    try {
      await apiJson(path, 'POST', body)
      if (activeContestIdRef.current !== targetId) return
      await load()
      if (okMsg) toast.success(okMsg)
    } catch (e) {
      if (activeContestIdRef.current === targetId) setError(errMsg(e))
    } finally {
      if (activeContestIdRef.current === targetId) {
        actionLockRef.current = false
        setBusyAction(false)
      }
    }
  }

  const saveSeriesSettings = async () => {
    const targetId = id
    if (
      !targetId ||
      activeContestIdRef.current !== targetId ||
      actionLockRef.current ||
      stageSeriesConfigs.length === 0 ||
      hasInvalidStageContract ||
      !seriesSettingsReady
    ) return
    actionLockRef.current = true
    setBusyAction(true)
    setError('')
    try {
      await apiJson(`/api/contests/${targetId}`, 'PATCH', {
        stage_series_settings: draftStageSeriesSettings,
      })
      if (activeContestIdRef.current !== targetId) return
      await load()
      toast.success('公平性设置已保存')
    } catch (cause) {
      if (activeContestIdRef.current === targetId) setError(errMsg(cause))
    } finally {
      if (activeContestIdRef.current === targetId) {
        actionLockRef.current = false
        setBusyAction(false)
      }
    }
  }

  const publishContest = async () => {
    const targetId = id
    if (!targetId || !contest || hasInvalidStageContract || !seriesSettingsReady || seriesSettingsError) return
    const totalMatches = projectedEstimatedMatches
    const totalScoringGames = projectedEstimatedScoringGames
    const totalSeconds = projectedEtaSeconds
    const frozenStageScopes = displayStageSeriesConfigs.length > 0
      ? displayStageSeriesConfigs.map((config) => {
          const setting = draftStageSeriesSettings[config.stage_key] || savedStageSeriesSettings[config.stage_key]
          return {
            label: config.label,
            gamesPerPair: setting?.games_per_pair ?? 1,
            stage: stages.find((item) => item.key === config.stage_key),
            extra: setting?.swiss_extra_rounds,
          }
        })
      : stages.flatMap((stage, index) => (
          exactIntegerAtLeast(stage.games_per_pair, 1)
            ? [{
                label: stageSeriesLabel(stage.key || `stage${index + 1}`, stages),
                gamesPerPair: Number(stage.games_per_pair),
                stage,
                extra: exactIntegerAtLeast(stage.swiss_extra_rounds, 0)
                  ? Number(stage.swiss_extra_rounds)
                  : undefined,
              }]
            : []
        ))
    const frozenStages = frozenStageScopes.map(({ label, gamesPerPair, stage, extra }) => {
      const scoringScope = stage?.duplicate === true
        ? `${gamesPerPair} 组复式交锋（${gamesPerPair * 2} 场计分，每组两场同牌换座、独立计分）`
        : `${gamesPerPair} 场计分`
      return `${label}每对选手 ${scoringScope}${extra != null ? `、额外 ${extra} 轮` : ''}`
    }).join('；')
    const scale = totalMatches != null
      ? `基础赛程共 ${totalMatches} ${overviewScheduleMeasure}${
          totalScoringGames != null && (totalScoringGames !== totalMatches || overviewScheduleMeasure !== '场计分')
            ? `、${totalScoringGames} 场计分`
            : ''
        }，${formatContestDuration(totalSeconds)}`
      : `${overviewScheduleLabel}数量将在排期生成时按报名人数核定`
    const tiebreakNotice = hasUnboundedTiebreak
      ? '淘汰平局将追加换边的两场决胜组，直到决出晋级者；加赛次数不封顶，不计入基础场数与 ETA。'
      : ''
    if (!await confirm({
      title: '截止报名并发布排期？',
      desc: `${frozenStages ? `${frozenStages}。` : ''}${scale}。${tiebreakNotice}发布后公平性设置与参赛名单冻结。`,
      confirmText: '确认发布',
      dismissOnOutside: false,
      buttonClassName: 'min-h-11',
    })) return
    if (activeContestIdRef.current !== targetId || actionLockRef.current) return
    actionLockRef.current = true
    setBusyAction(true)
    setError('')
    try {
      if (stageSeriesConfigs.length > 0) {
        await apiJson(`/api/contests/${targetId}`, 'PATCH', {
          stage_series_settings: draftStageSeriesSettings,
        })
      }
      if (activeContestIdRef.current !== targetId) return
      await apiJson(`/api/contests/${targetId}/publish`, 'POST')
      if (activeContestIdRef.current !== targetId) return
      await load()
      toast.success('排期已发布')
    } catch (cause) {
      if (activeContestIdRef.current === targetId) setError(errMsg(cause))
    } finally {
      if (activeContestIdRef.current === targetId) {
        actionLockRef.current = false
        setBusyAction(false)
      }
    }
  }

  const forceFinish = async () => {
    const targetId = id
    if (!targetId) return
    if (!await confirm({
      title: '强制结束赛事？',
      desc: '后端将根据关联对局的实际终态执行恢复性收尾，并计算正式名次；若仍有运行中的对局，请求会被拒绝。此操作不可撤销。',
      danger: true,
      confirmText: '确认结束',
    })) return
    // Navigation may have reused this component while the non-blocking dialog was
    // open. The old closure must neither POST its contest nor lock the new page.
    if (activeContestIdRef.current !== targetId) return
    await act(`/api/contests/${targetId}/finish`, undefined, '赛事已结束')
  }

  const removeEntry = async (entry: Entry) => {
    const targetId = id
    if (!targetId || activeContestIdRef.current !== targetId) return
    const entryLabel = entry.bot_display || entry.bot_name || entry.owner_display || entry.owner_name || '该选手'
    if (!await confirm({
      title: '移除报名选手？',
      desc: `将从本赛事移除「${entryLabel}」的报名记录。`,
      danger: true,
      confirmText: '确认移除',
    })) return
    if (activeContestIdRef.current !== targetId || actionLockRef.current) return
    actionLockRef.current = true
    setBusyAction(true)
    setError('')
    try {
      await apiJson(`/api/contests/${targetId}/entries/${entry.user_id}`, 'DELETE')
      if (activeContestIdRef.current !== targetId) return
      await load()
      toast.success('已移除报名选手')
    } catch (e) {
      if (activeContestIdRef.current === targetId) setError(errMsg(e))
    } finally {
      if (activeContestIdRef.current === targetId) {
        actionLockRef.current = false
        setBusyAction(false)
      }
    }
  }

  const stagePairings = pairings.filter((p) => (p.stage_idx ?? 0) === stageTab)
  const selectedStageStanding = stageStandings.find((stage) => stage.stage_idx === stageTab)
  const curStageType = stages[stageTab]?.type as string | undefined
  const currentStageContract = stageContracts[stageTab] ?? 'invalid'
  const currentStageContractAvailable = currentStageContract !== 'invalid'
  const currentStageDuplicate = currentStageContractAvailable && stages[stageTab]?.duplicate === true
  const currentStageLegacyAggregate = currentStageContract === 'aggregate'
  const stageConceptualPairings = new Set(stagePairings.map((pairing) => contestPairingSeriesKey(pairing, curStageType))).size
  const stageEncounterTotal = selectedStageStanding?.counts?.encounter_groups?.total ?? stageConceptualPairings
  const stageEncounterCompleted = selectedStageStanding?.counts?.encounter_groups?.completed
  const stageMatchJobTotal = selectedStageStanding?.counts?.match_jobs?.total ?? stagePairings.length
  const stageMatchJobCompleted = selectedStageStanding?.counts?.match_jobs?.completed
  const stageScoringGamePlanned = selectedStageStanding?.counts?.scoring_games?.planned
  const stageScoringGameCompleted = selectedStageStanding?.counts?.scoring_games?.completed
  const isElimStage = curStageType === 'single_elimination' || curStageType === 'double_elimination'
  // 阶段切换时，按赛制重置对阵视图默认值（淘汰→对阵树；swiss/循环→一览表），用户仍可手动切换
  useEffect(() => {
    setPairingView(isElimStage ? 'tree' : 'table')
  }, [stageTab, isElimStage])

  // 每阶段的对阵进度（已完成 / 总数）+ 已进行到的最大轮次，供 stage tab 进度条与「第 N 轮」展示
  const stageProgress = useMemo(() => {
    const map = new Map<number, { completed: number; total: number; maxRound: number }>()
    for (const p of pairings) {
      const idx = p.stage_idx ?? 0
      const cur = map.get(idx) ?? { completed: 0, total: 0, maxRound: 0 }
      cur.total += 1
      if (effectivePairingStatus(p) === 'completed') cur.completed += 1
      const r = p.round_num ?? 1
      if (r > cur.maxRound) cur.maxRound = r
      map.set(idx, cur)
    }
    return map
  }, [pairings])

  // 积分榜客户端分页：当前页 slice + 越界回退（如 standings 缩短到当前页之外）
  const standingsTotal = standings.length
  const standingsTotalPages = Math.max(1, Math.ceil(standingsTotal / standingsPerPage))
  const safeStandingsPage = Math.min(standingsPage, standingsTotalPages)
  const standingsPageItems = standings.slice(
    (safeStandingsPage - 1) * standingsPerPage,
    safeStandingsPage * standingsPerPage,
  )
  // 行号需要按全量排序位置计算（而非当前页内序），保证翻页后名次连续
  const standingsPageBase = (safeStandingsPage - 1) * standingsPerPage
  const officialCohortPointCounts = useMemo(() => {
    const counts = new Map<string, number>()
    for (const result of officialResults) {
      const cohort = result.ranking_cohort || `stage:${result.source_stage ?? 0}`
      const key = `${cohort}:${Number(result.points ?? 0)}`
      counts.set(key, (counts.get(key) ?? 0) + 1)
    }
    return counts
  }, [officialResults])
  const stageRowsByEntry = useMemo(() => {
    const rows = new Map<string, StageStandingRow>()
    for (const summary of stageStandings) {
      for (const row of summary.rows) {
        rows.set(`${summary.stage_idx}:${row.entry_id}`, row)
      }
    }
    return rows
  }, [stageStandings])

  if (!contest) {
    return (
      <PageFrame width="wide" layout="public-contest-detail-loading">
        <PageHeader
          title="锦标赛详情"
          description="查看赛事安排、参赛选手、对阵进度与正式名次。"
          actions={
            <Button asChild variant="outline" size="sm" className="min-h-11 sm:min-h-[var(--control-height)]">
              <Link to="/contests"><ArrowLeft aria-hidden="true" className="size-4" />返回赛事</Link>
            </Button>
          }
        />
        <DataRegion title="赛事概览" contentClassName="px-4 py-6">
          {error ? <ErrorMsg msg={error} /> : <Loading text="正在加载赛事详情…" />}
        </DataRegion>
      </PageFrame>
    )
  }

  const contestScheduleIssue = scheduleIssue(contest)
  const contestGame = findGame(contest.game_id)
  const isShowcase = Boolean(contest.showcase_key)
  const showMatchups = pairings.length > 0 || ['published', 'running', 'rest', 'finished'].includes(contest.status)
  const showStandings = standings.length > 0 || ['running', 'rest', 'finished'].includes(contest.status)
  const showOfficial = contest.status === 'finished'
  const showLiveCta = ['published', 'running', 'rest'].includes(contest.status)
  const selectedContestStageContract = stageContracts[contest.current_stage_idx ?? 0] ?? 'invalid'
  const currentScoringLabel = selectedContestStageContract === 'invalid'
    ? '赛制配置暂不可用'
    : SCORING_LABEL[
        stages[contest.current_stage_idx ?? 0]?.scoring || ''
      ] || '按本阶段规则计分'
  const templateLabel = contest.template_name || contest.template_id || '未指定模板'
  const stageLabel = stages.length > 0
    ? `${Math.min((contest.current_stage_idx ?? 0) + 1, stages.length)} / ${stages.length}`
    : '未配置'
  const canRegister = isLoggedIn && !myEntry && contest.status === 'open'
  const canSwapBot = isLoggedIn && Boolean(myEntry) && ['rest', 'draft', 'open', 'published'].includes(contest.status)
  const canManageLifecycle = isOrg && ['draft', 'open', 'published', 'running', 'rest'].includes(contest.status)
  const showActionRegion = isShowcase || canRegister || canSwapBot || canManageLifecycle
  const rosterDescription = contest.require_real_name
    ? serverIsOrganizer
      ? `每页 ${entriesPerPage} 人；导出按报名 ID、用户 ID 与 Bot ID 稳定关联账号和显示名。新报名使用报名时资料快照；历史报名若无快照会明确标注当前资料回退。${user?.role === 'admin' ? '管理员代报名会写入审计。' : '实名赛事仅允许选手本人报名，组织者不能代报名或批量指派。'}`
      : `每页 ${entriesPerPage} 人；本赛事要求实名报名，报名资料仅对赛事组织者可见。`
    : serverIsOrganizer
      ? `每页 ${entriesPerPage} 人；导出按报名 ID、用户 ID 与 Bot ID 稳定关联账号和显示名。`
      : `每页 ${entriesPerPage} 人；公开显示账号和 Bot 身份。`
  const officialDescription = [
    '完赛后固化的权威结果；赛事积分不改变平台 Rating。计分场战绩不把瑞士轮轮空记作胜场，轮空次数单独列出；同分行显示实际使用的破同分链。',
    hasInvalidStageContract
      ? '部分冻结赛制配置不可用，页面不会推断其复式、系列或计分单位。'
      : null,
    isDuplicate
      ? '复式交锋每组包含两场同牌换座的独立计分场，两场分别记胜、平、负。'
      : null,
    hasLegacyAggregateStage
      ? '历史阶段标注为“旧版系列结算”，继续按完整系列一次性结算，不会改写为新版独立计分场。'
      : null,
    contest.require_real_name
      ? '公开成绩 CSV 永不包含报名时实名资料。'
      : '公开成绩 CSV 只含公开身份与赛果。',
    canExportIdentity
      ? '组织者成绩明细按报名 ID、用户 ID 与 Bot ID 关联报名资料，并标明资料来自报名快照或历史回退。'
      : null,
  ].filter(Boolean).join(' ')

  return (
    <PageFrame width="wide" layout="public-contest-detail">
      <PageHeader
        title="锦标赛详情"
        description="查看赛程、选手和比赛结果。"
        actions={
          <>
            {showLiveCta && (
              <Button asChild size="sm" className="min-h-11 sm:min-h-[var(--control-height)]">
                <Link to={`/contests/${contest.id}/live`}>
                  <Radio aria-hidden="true" className="size-4" />进入赛事直播
                </Link>
              </Button>
            )}
            <Button asChild variant="outline" size="sm" className="min-h-11 sm:min-h-[var(--control-height)]">
              <Link to="/contests"><ArrowLeft aria-hidden="true" className="size-4" />返回赛事</Link>
            </Button>
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  type="button"
                  variant="outline"
                  size="icon-sm"
                  className="min-h-11 min-w-11 sm:min-h-[var(--control-height)] sm:min-w-[var(--control-height)]"
                  onClick={() => void load()}
                  aria-label="刷新赛事详情"
                >
                  <RefreshCw aria-hidden="true" className="size-4" />
                </Button>
              </TooltipTrigger>
              <TooltipContent>刷新赛事详情</TooltipContent>
            </Tooltip>
          </>
        }
      />
      <DataRegion
        data-testid="contest-overview"
        title={
          <EntityName lines={2} tooltip={contest.title} className="text-base leading-snug sm:text-lg">
            {contest.title}
          </EntityName>
        }
        description={
          <OverflowText
            lines={3}
            tooltip={contest.description || false}
            className="whitespace-pre-wrap break-words text-sm leading-relaxed [overflow-wrap:anywhere]"
          >
            {contest.description || '暂无赛事说明'}
          </OverflowText>
        }
        actions={
          <>
            <StatusBadge status={contest.status} />
            {isShowcase && <Badge variant="secondary">演示快照</Badge>}
            {hasInvalidStageContract && <Badge variant="destructive">赛制配置暂不可用</Badge>}
            {isDuplicate && <Badge variant="secondary">复式交锋 · 每组 2 场计分</Badge>}
            {hasLegacyAggregateStage && <Badge variant="outline">旧版系列结算</Badge>}
          </>
        }
        contentClassName="space-y-2 p-3"
      >
          <dl className="flex min-w-0 flex-wrap gap-x-5 gap-y-1 text-xs">
            <div className="inline-flex min-w-0 items-baseline gap-1.5">
              <dt className="text-muted-foreground">游戏</dt>
              <dd className="font-medium text-foreground">{gameLabel(contest.game_id)} · {contestGame?.matchFormatLabel || '规则不可用'}</dd>
            </div>
            <div className="inline-flex min-w-0 items-baseline gap-1.5">
              <dt className="text-muted-foreground">赛制</dt>
              <dd className="font-medium text-foreground">
                {hasInvalidStageContract
                  ? `${templateLabel} · 配置暂不可用`
                  : `${templateLabel}${isDuplicate ? ' · 同牌换座，两场独立计分' : ''}`}
              </dd>
            </div>
            <div className="inline-flex min-w-0 items-baseline gap-1.5">
              <dt className="text-muted-foreground">选手</dt>
              <dd className="font-mono font-medium tabular-nums text-foreground">{entriesTotal}</dd>
            </div>
            <div className="inline-flex min-w-0 items-baseline gap-1.5">
              <dt className="text-muted-foreground">阶段</dt>
              <dd className="font-mono font-medium tabular-nums text-foreground">{stageLabel}</dd>
            </div>
            <div className="inline-flex min-w-0 items-baseline gap-1.5">
              <dt className="text-muted-foreground">{overviewScheduleLabel}</dt>
              <dd className="font-mono font-medium tabular-nums text-foreground">
                {hasInvalidStageContract
                  ? '暂不可用'
                  : <>{projectedEstimatedMatches != null ? `预计 ${projectedEstimatedMatches}` : pairings.length}{' '}{overviewScheduleSuffix}</>}
              </dd>
            </div>
            {!hasInvalidStageContract && contest.games_per_pair != null && displayStageSeriesConfigs.length === 0 && (
              <div className="inline-flex min-w-0 items-baseline gap-1.5">
                <dt className="text-muted-foreground">
                  {hasLegacyAggregateStage ? '每对历史系列' : isDuplicate ? '每对复式交锋' : '每对计分场'}
                </dt>
                <dd className="font-mono font-medium tabular-nums text-foreground">
                  {hasLegacyAggregateStage
                    ? `${contest.games_per_pair} 场 · 完整系列 1 次结算`
                    : isDuplicate
                    ? `${contest.games_per_pair} 组 · ${contest.games_per_pair * 2} 场计分`
                    : `${contest.games_per_pair} 场计分`}
                </dd>
              </div>
            )}
          </dl>
          {contest.status === 'rest' && (isShowcase || contest.rest_ends_at) && (
            <div className="flex min-w-0 items-start gap-2 rounded-lg border border-warning/30 bg-warning/10 px-3 py-2 text-sm text-warning-foreground">
              <Timer aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-warning" />
              {isShowcase ? (
                <span className="min-w-0 break-words">小组赛已结束，等待进入下一阶段；演示快照不会自动倒计时推进。</span>
              ) : (
                <span className="min-w-0 break-words">
                  阶段休息中，倒计时 <Countdown endsAt={contest.rest_ends_at!} className="text-warning" />（可更换派遣 Bot）
                </span>
              )}
            </div>
          )}
          <ContestScheduleInfo c={contest} />
          {contestScheduleIssue && (
            <div role="alert" className="flex min-w-0 items-start gap-2 rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm text-destructive">
              <AlertTriangle aria-hidden="true" className="mt-0.5 size-4 shrink-0" />
              <span className="min-w-0 break-words">
                时间配置异常：{contestScheduleIssue}。该历史数据需要组织者或管理员修正，系统不会据此倒退赛事状态。
              </span>
            </div>
          )}
      </DataRegion>

      {error && <ErrorMsg msg={error} />}
      {seriesSettingsError && <ErrorMsg msg={seriesSettingsError} />}
      {seriesSettingsNotice && (
        <div role="status" className="flex min-w-0 items-start gap-2 rounded-lg border border-warning/25 bg-warning/10 px-3 py-2.5 text-sm leading-relaxed text-warning-foreground">
          <AlertTriangle aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-warning" />
          <span className="min-w-0 break-words">{seriesSettingsNotice}</span>
        </div>
      )}

      {(selectedTemplateGuidance || projectedEstimate || displayStageSeriesConfigs.length > 0) && (
        <DataRegion
          title="赛制公平性与规模"
          description={displayStageSeriesConfigs.length > 0
            ? contest.status === 'draft' || contest.status === 'open'
              ? '人数只影响推荐；组织者可在发布前调整系列强度，页面会立即投影基础计分场数与耗时。'
              : '人数只影响推荐；排期发布时系列设置已冻结，后续阶段沿用这组设置。'
            : '人数区间仅用于推荐，不限制创建或发布；基础场数按当前报名名单估算。'}
          actions={isOrg && (contest.status === 'draft' || contest.status === 'open') && stageSeriesConfigs.length > 0 ? (
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="min-h-11 sm:min-h-[var(--control-height)]"
              disabled={busyAction || !seriesSettingsDirty || !seriesSettingsReady}
              onClick={() => void saveSeriesSettings()}
            >
              {seriesSettingsDirty ? '保存设置' : '设置已保存'}
            </Button>
          ) : undefined}
          contentClassName="min-w-0"
        >
          <TemplateGuidancePanel
            template={selectedTemplateGuidance}
            templates={templateCatalog}
            participantCount={entriesTotal}
            estimate={projectedEstimate}
            unboundedTiebreak={hasUnboundedTiebreak}
            frozen={!['draft', 'open'].includes(contest.status)}
            className="p-3"
          />
          {displayStageSeriesConfigs.length > 0 && (
            <div className="border-t">
              <StageSeriesSettingsEditor
                configs={displayStageSeriesConfigs}
                value={draftStageSeriesSettings}
                onChange={isOrg && stageSeriesConfigs.length > 0 && (contest.status === 'draft' || contest.status === 'open') ? setDraftStageSeriesSettings : undefined}
                estimates={estimate?.stages}
                disabled={busyAction || stageSeriesConfigs.length === 0 && (contest.status === 'draft' || contest.status === 'open')}
                frozen={!['draft', 'open'].includes(contest.status)}
              />
            </div>
          )}
        </DataRegion>
      )}

      {showActionRegion && (
        <DataRegion
          title={isShowcase ? '演示说明' : '赛事操作'}
          description={isShowcase ? '演示快照与真实业务数据完全隔离。' : busyAction ? '正在处理操作，请稍候…' : '可用操作随赛事状态与当前身份变化。'}
          contentClassName="flex min-w-0 flex-wrap items-center gap-2 p-3"
          aria-busy={busyAction || undefined}
        >
        {isShowcase ? (
          <div className="flex min-w-0 w-full items-start gap-2 rounded-lg border border-primary/20 bg-primary/5 px-3 py-2 text-sm text-muted-foreground">
            <AlertTriangle aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-primary" />
            <span className="min-w-0 break-words"><span className="font-medium text-foreground">合成演示 · 只读。</span> 此页面用于展示赛事生命周期，报名、换 Bot、时间和阶段推进均已冻结。</span>
          </div>
        ) : (
          <>
        {isOrg && contest.status === 'draft' && (
          <Button disabled={busyAction} onClick={() => void act(`/api/contests/${id}/open`, undefined, '已开放报名')} className="gap-1.5">
            <DoorOpen className="size-4" />开放报名
          </Button>
        )}
        {isOrg && contest.status === 'open' && (
          <Button variant="outline" disabled={busyAction || hasInvalidStageContract || !seriesSettingsReady || Boolean(seriesSettingsError)} onClick={() => void publishContest()} className="min-h-11 gap-1.5 sm:min-h-[var(--control-height)]">
            <ListOrdered className="size-4" />截止报名·出排期
          </Button>
        )}
        {isOrg && contest.status === 'published' && (
          <Button variant="outline" disabled={busyAction || hasInvalidStageContract} onClick={() => void act(`/api/contests/${id}/start`, undefined, '比赛已开始')} className="gap-1.5">
            <Play className="size-4" />立即开赛
          </Button>
        )}
        {isOrg && contest.status === 'rest' && (
          <Button disabled={busyAction || hasInvalidStageContract} onClick={() => void act(`/api/contests/${id}/resume`, undefined, '已进入下一阶段')}>结束休息 / 下一阶段</Button>
        )}
        {isOrg && (contest.status === 'running' || contest.status === 'rest') && (
          <Tooltip>
            <TooltipTrigger asChild>
              <span>
                <Button
                  variant="destructive"
                  disabled={busyAction}
                  onClick={() => void forceFinish()}
                  className="gap-1.5"
                >
                  强制结束赛事
                </Button>
              </span>
            </TooltipTrigger>
            <TooltipContent>
              由后端核验关联对局终态并执行恢复性收尾
            </TooltipContent>
          </Tooltip>
        )}
        {canRegister && (
          <div className="flex min-w-0 flex-1 flex-wrap items-center gap-2">
            {needsRealName && (
              <span className="w-full min-w-0 break-words text-sm text-warning">
                本赛事要求实名报名，请先{' '}
                <Link to="/settings" className="font-medium underline">填写实名信息</Link>
              </span>
            )}
            <Select value={botId} onValueChange={setBotId} disabled={busyAction}>
              <SelectTrigger className="w-full sm:w-[14rem]">
                <SelectValue placeholder="选择我的 Bot" />
              </SelectTrigger>
              <SelectContent>
                {bots.map((b) => (<SelectItem key={b.id} value={String(b.id)}>{b.display_name || b.name}</SelectItem>))}
              </SelectContent>
            </Select>
            <Button variant="outline" disabled={busyAction || !botId || needsRealName} onClick={() => void act(`/api/contests/${id}/register`, { bot_id: Number(botId) }, '报名成功')}>
              报名派遣
            </Button>
          </div>
        )}
        {canSwapBot && (
          <div className="flex min-w-0 flex-1 flex-wrap items-center gap-2">
            <Select value={botId} onValueChange={setBotId} disabled={busyAction}>
              <SelectTrigger className="w-full sm:w-[14rem]">
                <SelectValue placeholder="更换当前派遣 Bot" />
              </SelectTrigger>
              <SelectContent>
                {bots.map((b) => (<SelectItem key={b.id} value={String(b.id)}>{b.display_name || b.name}</SelectItem>))}
              </SelectContent>
            </Select>
            <Button variant="outline" disabled={busyAction || !botId} onClick={() => void act(`/api/contests/${id}/dispatch`, { bot_id: Number(botId) }, '派遣 Bot 已更换')}>
              确认更换
            </Button>
          </div>
        )}
          </>
        )}
        </DataRegion>
      )}

      {/* 内容区按赛事阶段展示，避免草稿期出现空对阵、完赛后仍以临时积分为主。 */}
      <Tabs value={contentTab} onValueChange={(v) => setContentTab(v as typeof contentTab)} className="min-w-0">
        <StickyToolbar label="赛事内容导航" className="p-1.5">
          <TabsList variant="line" className="w-full justify-start overflow-y-hidden pb-1">
          {showMatchups && (
            <TabsTrigger value="matchups" className="min-h-11 gap-1.5 sm:min-h-[var(--control-height)]">
              <Swords className="size-4" />对阵
            </TabsTrigger>
          )}
          <TabsTrigger value="entries" className="min-h-11 gap-1.5 sm:min-h-[var(--control-height)]">
            <Users className="size-4" />选手
            {entriesTotal > 0 && (
              <span className="text-xs text-muted-foreground">{entriesTotal}</span>
            )}
          </TabsTrigger>
          {showStandings && (
            <TabsTrigger value="standings" className="min-h-11 gap-1.5 sm:min-h-[var(--control-height)]">
              <ListOrdered className="size-4" />阶段积分
              {standings.length > 0 && (
                <span className="text-xs text-muted-foreground">{standings.length}</span>
              )}
            </TabsTrigger>
          )}
          {showOfficial && (
            <TabsTrigger value="official" className="min-h-11 gap-1.5 sm:min-h-[var(--control-height)]">
              <Trophy className="size-4" />正式名次
              {officialResults.length > 0 && (
                <span className="text-xs text-muted-foreground">{officialResults.length}</span>
              )}
            </TabsTrigger>
          )}
          </TabsList>
        </StickyToolbar>

        {/* Tab「对阵」：阶段切换(S4) + 阶段配置 + 对阵视图(S6a) + 正式名次(finished) */}
        <TabsContent value="matchups" className="mt-2 space-y-3">
          {stages.length > 0 && (
            <div className="min-w-0 space-y-2 rounded-xl border bg-card p-2">
              <Tabs value={String(stageTab)} onValueChange={(v) => setStageTab(Number(v))}>
                <TabsList variant="line" className="w-full justify-start overflow-y-hidden pb-1">
                  {stages.map((s, i) => {
                    const stageContract = stageContracts[i] ?? 'invalid'
                    const prog = stageProgress.get(i)
                    const summary = stageStandings.find((item) => item.stage_idx === i)
                    const matchJobs = summary?.counts?.match_jobs
                    const scoringGames = summary?.counts?.scoring_games
                    const completed = matchJobs?.completed ?? prog?.completed ?? 0
                    const total = matchJobs?.total ?? prog?.total ?? 0
                    const typeLabel = STAGE_TYPE_LABEL[s.type || ''] || `阶段${i + 1}`
                    const roundTag = prog && prog.maxRound > 0 && completed < total
                      ? `第${prog.maxRound}轮`
                      : null
                    const progressLabel = stageContract === 'invalid'
                      ? '赛制配置暂不可用'
                      : summary && (summary.counts?.match_jobs || total > 0)
                      ? stageStandingProgressLabel(
                          summary,
                          s.duplicate === true,
                          s.series_scoring === 'aggregate_match_points_v1',
                        )
                      : total > 0
                        ? s.series_scoring === 'aggregate_match_points_v1'
                          ? `${completed}/${total} 场历史系列对局`
                          : s.duplicate
                            ? `${completed}/${total} 组复式交锋${scoringGames ? ` · ${scoringGames.completed}/${scoringGames.planned} 场计分` : ''}`
                            : `${scoringGames?.completed ?? completed}/${scoringGames?.planned ?? total} 场计分`
                        : null
                    return (
                      <TabsTrigger key={s.key || i} value={String(i)} className="min-h-11 gap-1.5 sm:min-h-[var(--control-height)]">
                        <span>{typeLabel}</span>
                        {roundTag && <span className="text-xs text-muted-foreground">· {roundTag}</span>}
                        {contest.current_stage_idx === i && contest.status !== 'finished' && (
                          <Badge variant="outline" className="ml-1 text-[9px] text-primary">当前</Badge>
                        )}
                        {progressLabel && (
                          <span className="text-[10px] text-muted-foreground">
                            {progressLabel}
                          </span>
                        )}
                      </TabsTrigger>
                    )
                  })}
                </TabsList>
              </Tabs>

              {stages[stageTab] && (
                <OverflowText
                  lines={3}
                  tooltip={false}
                  className="border-t px-2 pt-2 text-xs leading-relaxed text-muted-foreground [overflow-wrap:anywhere]"
                >
                  <span className="font-medium text-foreground">本阶段配置：</span>
                  {currentStageContractAvailable ? [
                    STAGE_TYPE_LABEL[stages[stageTab].type || ''] || (stages[stageTab].type ? '自定义赛制' : null),
                    SCORING_LABEL[stages[stageTab].scoring || ''] || (stages[stageTab].scoring ? '自定义计分' : null),
                    stages[stageTab].duplicate ? '复式交锋（同牌换座，两场独立计分）' : null,
                    stages[stageTab].series_scoring === 'aggregate_match_points_v1'
                      ? '旧版系列结算（冻结历史口径）'
                      : null,
                    stages[stageTab].group_count ? `分组 ${stages[stageTab].group_count}` : null,
                    stages[stageTab].rounds !== undefined ? `轮数 ${stages[stageTab].rounds}` : null,
                    stages[stageTab].advance_count ? `晋级 ${stages[stageTab].advance_count}` : null,
                    stages[stageTab].advance_per_group ? `每组晋级 ${stages[stageTab].advance_per_group}` : null,
                    stages[stageTab].rest_after_minutes ? `休息 ${stages[stageTab].rest_after_minutes} 分` : null,
                    stages[stageTab].allow_bot_swap_in_rest ? '休息可换 Bot' : null,
                  ].filter(Boolean).join(' · ') : '赛制配置暂不可用；页面不会推断复式、系列或计分单位。'}
                </OverflowText>
              )}
            </div>
          )}

          <EliminationTiebreakStatus
            value={selectedStageStanding?.elimination_tiebreak}
            className="rounded-xl border"
          />

          {/* 宽屏把对阵与阶段榜并排，窄屏自然上下排列。 */}
          <div className="grid min-w-0 items-stretch gap-3 xl:grid-cols-[minmax(0,1fr)_22rem]">
          <DataRegion
            title={`对阵${stages.length ? ` · ${STAGE_TYPE_LABEL[stages[stageTab]?.type || ''] || `阶段${stageTab + 1}`}` : ''}`}
            description={!currentStageContractAvailable
              ? '赛制配置暂不可用；以下仅保留已有记录，不推断计分口径或阶段进度。'
              : stagePairings.length > 0
              ? currentStageLegacyAggregate
                ? `当前阶段 ${stageEncounterCompleted == null ? stageEncounterTotal : `${stageEncounterCompleted}/${stageEncounterTotal}`} 个对手系列 · ${stageMatchJobCompleted == null ? stageMatchJobTotal : `${stageMatchJobCompleted}/${stageMatchJobTotal}`} 场历史系列对局${stageScoringGamePlanned == null ? '' : ` · ${stageScoringGameCompleted == null ? stageScoringGamePlanned : `${stageScoringGameCompleted}/${stageScoringGamePlanned}`} 次旧版系列结算`}。完整系列按冻结规则只结算 1 次胜、平、负，不作为新版独立计分场。`
                : currentStageDuplicate
                ? `当前阶段 ${stageEncounterCompleted == null ? stageEncounterTotal : `${stageEncounterCompleted}/${stageEncounterTotal}`} 个对手系列 · ${stageMatchJobCompleted == null ? stageMatchJobTotal : `${stageMatchJobCompleted}/${stageMatchJobTotal}`} 组复式交锋${stageScoringGamePlanned == null ? '' : ` · ${stageScoringGameCompleted == null ? `计划 ${stageScoringGamePlanned}` : `${stageScoringGameCompleted}/${stageScoringGamePlanned}`} 场计分`}。`
                : `当前阶段 ${stageEncounterCompleted == null ? stageEncounterTotal : `${stageEncounterCompleted}/${stageEncounterTotal}`} 个对手系列 · ${stageMatchJobCompleted == null ? stageMatchJobTotal : `${stageMatchJobCompleted}/${stageMatchJobTotal}`} 条对局记录${stageScoringGamePlanned == null ? ` · ${stageMatchJobTotal} 场计分` : ` · ${stageScoringGameCompleted == null ? stageScoringGamePlanned : `${stageScoringGameCompleted}/${stageScoringGamePlanned}`} 场计分`}。`
              : '排期生成后将在这里显示。'}
            actions={currentStageContractAvailable && stagePairings.length > 0 ? (
              <div role="group" aria-label="对阵视图" className="flex items-center gap-1 rounded-lg bg-muted p-0.5">
                <Button
                  type="button"
                  size="sm"
                  className="min-h-11 sm:min-h-8"
                  variant={pairingView === 'tree' ? 'secondary' : 'ghost'}
                  aria-pressed={pairingView === 'tree'}
                  onClick={() => setPairingView('tree')}
                >
                  {isElimStage ? '对阵树' : '分组视图'}
                </Button>
                <Button
                  type="button"
                  size="sm"
                  className="min-h-11 sm:min-h-8"
                  variant={pairingView === 'table' ? 'secondary' : 'ghost'}
                  aria-pressed={pairingView === 'table'}
                  onClick={() => setPairingView('table')}
                >
                  一览表
                </Button>
              </div>
            ) : undefined}
            className="h-full"
            contentClassName="min-w-0 p-3"
          >
            {!currentStageContractAvailable ? (
              <EmptyState text="赛制配置暂不可用，已停止推断对阵单位与赛果。" icon={<AlertTriangle className="size-6 opacity-50" />} className="py-8" />
            ) : stagePairings.length === 0 ? (
              <EmptyState text="暂无对阵" icon={<Swords className="size-6 opacity-40" />} className="py-8" />
            ) : pairingView === 'table' ? (
              <ScheduleTable
                pairings={stagePairings}
                stageType={curStageType}
                duplicate={currentStageDuplicate}
                legacyAggregate={currentStageLegacyAggregate}
              />
            ) : isElimStage ? (
              <BracketTree pairings={stagePairings} duplicate={currentStageDuplicate} />
            ) : (
              <PairingFoldedList
                pairings={stagePairings}
                stageType={curStageType}
                duplicate={currentStageDuplicate}
                legacyAggregate={currentStageLegacyAggregate}
              />
            )}
          </DataRegion>
          {currentStageContractAvailable ? (
            <StageStandingPanel
              summary={selectedStageStanding}
              duplicate={currentStageDuplicate}
              legacyAggregate={currentStageLegacyAggregate}
            />
          ) : (
            <DataRegion title="阶段排名与晋级" description="赛制配置暂不可用" className="h-full min-w-0">
              <EmptyState text="已停止推断本阶段积分与晋级。" className="py-8" />
            </DataRegion>
          )}
          </div>

        </TabsContent>

        {/* Tab「选手」：报名列表（非实名组织者/admin 可批量指派；实名赛仅 admin 可审计代报名） */}
        <TabsContent value="entries" className="mt-2">
          <DataRegion
            title={`报名选手（${entriesTotal}）`}
            description={rosterDescription}
            actions={
              <>
                {canAssignEntries && user?.role !== 'admin' && !isShowcase && (contest.status === 'draft' || contest.status === 'open') && (
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => void act(`/api/contests/${id}/entries/bulk`, { assign_all: true, game_id: contest.game_id }, '批量指派完成')}
                  >
                    <Plus aria-hidden="true" className="size-3.5" />批量指派
                  </Button>
                )}
                {serverIsOrganizer && (
                  <Button asChild size="sm" variant="outline" className={EXPORT_LINK_CLASS}>
                    <a href={`/api/contests/${id}/export?format=csv&schema=2`} download>
                      <Download aria-hidden="true" className="size-3.5" />
                      {contest.require_real_name ? '导出实名报名名单' : '导出报名名单'}
                    </a>
                  </Button>
                )}
              </>
            }
            contentClassName="min-w-0"
          >
                {canAssignEntries && user?.role === 'admin' && !isShowcase && (contest.status === 'draft' || contest.status === 'open') && (
                  <AdminContestRosterAssign
                    contestId={contest.id}
                    gameId={contest.game_id}
                    existingUserIds={entries.map((entry) => entry.user_id)}
                    onDone={load}
                    className="m-3"
                  />
                )}
                {entries.length === 0 ? (
                  <EmptyState text="暂无报名" className="py-8" />
                ) : (
                  <ul className="divide-y text-sm" aria-label="报名选手列表">
                    {entries.map((e) => (
                      <li key={e.id} className="grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-center gap-2 px-3 py-2.5">
                        <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
                          {e.bot_id != null ? (
                            <Link to={`/bot/${e.bot_id}`} className="min-w-0 max-w-full hover:text-primary sm:max-w-xs">
                              <EntityName
                                lines={2}
                                tooltip={e.bot_display || e.bot_name || 'Bot 名称不可用'}
                                tooltipFocusable={false}
                                className="text-sm [overflow-wrap:anywhere]"
                              >
                                {e.bot_display || e.bot_name || 'Bot 名称不可用'}
                              </EntityName>
                            </Link>
                          ) : (
                            <span className="text-sm text-muted-foreground">已删除 Bot</span>
                          )}
                          {e.owner_name && (
                            <Link to={`/user/${encodeURIComponent(e.owner_name)}`} className="min-w-0 max-w-full text-xs text-muted-foreground hover:text-primary sm:max-w-56">
                              <OverflowText lines={2} tooltip={`@${e.owner_display || e.owner_name}`} tooltipFocusable={false} className="[overflow-wrap:anywhere]">
                                @{e.owner_display || e.owner_name}
                              </OverflowText>
                            </Link>
                          )}
                          {e.seed ? <Badge variant="outline">报名序 {e.seed}</Badge> : null}
                          {e.group_id && (
                            <Badge variant="secondary" className="min-w-0 max-w-full">
                              <OverflowText tooltip={e.group_id} tooltipFocusable={false} className="max-w-32">{e.group_id}</OverflowText>
                            </Badge>
                          )}
                          {e.eliminated ? <Badge variant="destructive">淘汰</Badge> : null}
                          {serverIsOrganizer && contest.require_real_name && e.real_name && (
                            <div className="basis-full min-w-0 text-xs leading-relaxed text-muted-foreground">
                              <span className="font-medium text-foreground">
                                {identitySourceLabel(e.identity_source)}
                              </span>
                              {e.identity_captured_at && (
                                <span> · 采集于 {fmtTime(e.identity_captured_at)}</span>
                              )}
                              <span className="block break-words [overflow-wrap:anywhere]">
                                {[e.real_name, e.phone, e.school, e.student_id].filter(Boolean).join(' / ')}
                              </span>
                            </div>
                          )}
                        </div>
                        {isOrg && !isShowcase && (contest.status === 'draft' || contest.status === 'open') && (
                          <Button
                            size="xs"
                            variant="ghost"
                            className="text-destructive"
                            disabled={busyAction}
                            onClick={() => void removeEntry(e)}
                          >
                            移除
                          </Button>
                        )}
                      </li>
                    ))}
                  </ul>
                )}
                <div className="border-t px-3 py-2">
                  <Pagination
                  page={entriesPage}
                  perPage={entriesPerPage}
                  total={entriesTotal}
                  onPageChange={setEntriesPage}
                  />
                </div>
          </DataRegion>
        </TabsContent>

        {/* Tab「排行」：积分榜（客户端分页，per_page=30） */}
        <TabsContent value="standings" className="mt-2">
          <DataRegion
            title="阶段积分"
            description={currentStageContractAvailable
              ? `赛事积分与平台 Rating 相互独立；本阶段计分：${currentScoringLabel}。${currentStageLegacyAggregate ? '本历史阶段沿用旧版系列结算，完整系列只产生 1 次胜、平、负，页面不会将其改写为独立计分场；' : currentStageDuplicate ? '复式每组的两场 70 手计分场分别记胜、平、负；' : ''}计分场战绩不包含瑞士轮轮空，轮空分与次数单独列出。正式结果以完赛后的名次为准。`
              : '赛制配置暂不可用；已停止推断本阶段积分、计分场战绩和晋级。'}
          >
              <DataTable className="rounded-none border-0 border-b" scrollLabel="阶段积分表">
                <Table className="min-w-[38rem]">
                  <TableHeader>
                    <TableRow>
                      <TableHead className="w-14">名次</TableHead>
                      <TableHead className="min-w-[6rem]">Bot</TableHead>
                      <TableHead>积分</TableHead>
                      <TableHead className="min-w-[13rem]">{currentStageContractAvailable ? (currentStageLegacyAggregate ? '旧版系列战绩 / 轮空' : '计分场战绩 / 轮空') : '计分构成'}</TableHead>
                      <TableHead className="hidden md:table-cell">阶段合计分差</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {!currentStageContractAvailable || standings.length === 0 ? (
                      <TableRow><TableCell colSpan={5}><EmptyState text={currentStageContractAvailable ? '暂无积分数据' : '赛制配置暂不可用，积分未展示'} icon={<Trophy className="size-7 opacity-40" />} /></TableCell></TableRow>
                    ) : (
                      standingsPageItems.map((s, i) => (
                        <TableRow key={`${s.bot_id ?? 'deleted'}-${standingsPageBase + i}`}>
                          <TableCell className="font-mono text-xs text-muted-foreground">{standingsPageBase + i + 1}</TableCell>
                          <TableCell className="max-w-[10rem]">
                            {s.bot_id != null ? (
                              <Link to={`/bot/${s.bot_id}`} className="block min-w-0 hover:text-primary">
                                <EntityName tooltip={s.bot_name || 'Bot 名称不可用'} tooltipFocusable={false}>
                                  {s.bot_name || 'Bot 名称不可用'}
                                </EntityName>
                              </Link>
                            ) : (
                              <span className="text-sm text-muted-foreground">已删除 Bot</span>
                            )}
                          </TableCell>
                          <TableCell className="font-mono font-semibold text-primary">{s.points}</TableCell>
                          <TableCell className="font-mono text-xs text-muted-foreground">
                            <span className="block font-sans font-medium text-foreground">
                              {scoringCountBreakdown(s, currentStageDuplicate, currentStageLegacyAggregate)}
                            </span>
                            {scoreBreakdown(s)}
                          </TableCell>
                          <TableCell className="hidden font-mono text-xs text-muted-foreground md:table-cell">{s.delta_total}</TableCell>
                        </TableRow>
                      ))
                    )}
                  </TableBody>
                </Table>
              </DataTable>
              {currentStageContractAvailable && <div className="px-3 py-2">
                <Pagination
                  page={safeStandingsPage}
                  perPage={standingsPerPage}
                  total={standingsTotal}
                  onPageChange={setStandingsPage}
                />
              </div>}
          </DataRegion>
        </TabsContent>

        {/* 完赛后的权威、已固化名次；与赛中动态积分明确分开。 */}
        <TabsContent value="official" className="mt-2">
          <DataRegion
            title="正式名次"
            description={officialDescription}
            actions={
              <>
                <Button asChild variant="outline" size="sm" className={EXPORT_LINK_CLASS}>
                  <a href={`/api/contests/${id}/official-results?format=csv`} download>
                    <Download aria-hidden="true" className="size-3.5" />导出公开成绩 CSV
                  </a>
                </Button>
                {canExportIdentity && (
                  <Button asChild variant="outline" size="sm" className={EXPORT_LINK_CLASS}>
                    <a href={`/api/contests/${id}/export?format=csv&schema=2`} download>
                      <Download aria-hidden="true" className="size-3.5" />
                      导出组织者成绩明细（含实名报名资料）
                    </a>
                  </Button>
                )}
              </>
            }
          >
              <DataTable className="rounded-none border-0" scrollLabel="赛事正式名次表">
                <Table className="min-w-[72rem]">
                  <TableHeader>
                    <TableRow>
                      <TableHead className="w-14">名次</TableHead>
                      <TableHead>Bot</TableHead>
                      <TableHead>选手</TableHead>
                      <TableHead>积分</TableHead>
                      <TableHead className="min-w-[13rem]">{hasInvalidStageContract ? '计分构成' : hasLegacyAggregateStage ? '计分构成（含旧版系列）' : '计分场构成'}</TableHead>
                      <TableHead className="min-w-[22rem]">破同分依据</TableHead>
                      <TableHead className="hidden md:table-cell">奖项</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {officialResults.length === 0 ? (
                      <TableRow>
                        <TableCell colSpan={7}>
                          <EmptyState
                            text={contest.official_results_ready ? '正式名次为空' : '正式名次尚未生成'}
                            icon={<Trophy className="size-7 opacity-40" />}
                          />
                        </TableCell>
                      </TableRow>
                    ) : officialResults.map((result) => {
                      const cohort = result.ranking_cohort || `stage:${result.source_stage ?? 0}`
                      const tieKey = `${cohort}:${Number(result.points ?? 0)}`
                      const hasPointTie = (officialCohortPointCounts.get(tieKey) ?? 0) > 1
                      const sourceStage = result.source_stage
                      const scoreRow = stageRowsByEntry.get(`${sourceStage ?? 0}:${result.entry_id}`)
                      const sourceLabel = typeof sourceStage === 'number'
                        ? (STAGE_TYPE_LABEL[stages[sourceStage]?.type || ''] || `阶段 ${sourceStage + 1}`)
                        : null
                      const sourceContract = typeof sourceStage === 'number'
                        ? stageContracts[sourceStage] ?? 'invalid'
                        : hasInvalidStageContract ? 'invalid' : 'plain'
                      const sourceDuplicate = typeof sourceStage === 'number'
                        ? sourceContract !== 'invalid' && stages[sourceStage]?.duplicate === true
                        : isDuplicate
                      const sourceLegacyAggregate = typeof sourceStage === 'number'
                        ? sourceContract === 'aggregate'
                        : hasLegacyAggregateStage
                      return (
                      <TableRow key={result.entry_id}>
                        <TableCell className="font-mono text-base font-semibold text-primary">
                          {result.rank}
                        </TableCell>
                        <TableCell className="max-w-[12rem]">
                          {result.bot_id ? (
                            <Link
                              to={`/bot/${result.bot_id}`}
                              className="block min-w-0 hover:text-primary"
                            >
                              <EntityName
                                tooltip={result.bot_display || result.bot_name || '未命名 Bot'}
                                tooltipFocusable={false}
                              >
                                {result.bot_display || result.bot_name || '未命名 Bot'}
                              </EntityName>
                            </Link>
                          ) : <span className="text-muted-foreground">已删除 Bot</span>}
                        </TableCell>
                        <TableCell className="max-w-[12rem]">
                          {result.owner_name ? (
                            <Link
                              to={`/user/${encodeURIComponent(result.owner_name)}`}
                              className="block min-w-0 text-muted-foreground hover:text-primary"
                            >
                              <OverflowText
                                tooltip={`@${result.owner_display || result.owner_name}`}
                                tooltipFocusable={false}
                              >
                                @{result.owner_display || result.owner_name}
                              </OverflowText>
                            </Link>
                          ) : <span className="text-muted-foreground">—</span>}
                        </TableCell>
                        <TableCell className="font-mono font-semibold">
                          <span>{result.points ?? 0}</span>
                          {sourceLabel && stages.length > 1 && (
                            <span className="mt-0.5 block font-sans text-[10px] font-normal text-muted-foreground">
                              {sourceLabel}
                            </span>
                          )}
                        </TableCell>
                        <TableCell className="font-mono text-xs text-muted-foreground">
                          {scoreRow && sourceContract !== 'invalid' ? (
                            <>
                              <span className="block font-sans font-medium text-foreground">
                                {scoringCountBreakdown(scoreRow, sourceDuplicate, sourceLegacyAggregate)}
                              </span>
                              {scoreBreakdown(scoreRow)}
                            </>
                          ) : sourceContract === 'invalid' ? '赛制配置暂不可用' : '历史计分构成不可用'}
                        </TableCell>
                        <TableCell className="min-w-[22rem] max-w-[30rem]">
                          <OfficialTiebreakDetail
                            result={result}
                            hasPointTie={hasPointTie}
                            sourceLabel={stages.length > 1 ? sourceLabel : null}
                          />
                        </TableCell>
                        <TableCell className="hidden text-muted-foreground md:table-cell">
                          {result.awarded || '—'}
                        </TableCell>
                      </TableRow>
                      )
                    })}
                  </TableBody>
                </Table>
              </DataTable>
          </DataRegion>
        </TabsContent>
      </Tabs>
      {confirmDialog}
    </PageFrame>
  )
}

function StageStandingPanel({
  summary,
  duplicate,
  legacyAggregate,
}: {
  summary?: StageStandingSummary
  duplicate: boolean
  legacyAggregate: boolean
}) {
  const sourceLabel = summary?.source === 'persisted'
    ? '阶段结果已固化'
    : summary?.source === 'live'
      ? '实时积分'
      : summary?.source === 'scheduled'
        ? '排期名单'
        : '等待本阶段'
  const advancementLabel = (value: StageStandingRow['advancement']) => {
    if (value === 'advanced') return <Badge className="text-[9px]">已晋级</Badge>
    if (value === 'in_zone') return <Badge variant="outline" className="text-[9px] text-primary">暂列晋级区</Badge>
    if (value === 'eliminated') return <span className="text-[10px] text-muted-foreground">未晋级</span>
    if (value === 'outside_zone') return <span className="text-[10px] text-muted-foreground">暂列区外</span>
    return null
  }

  return (
    <DataRegion
      title="阶段排名与晋级"
      description={sourceLabel}
      className="h-full min-w-0"
      actions={summary && summary.total_pairings > 0 ? (
          <Badge variant="outline" className="text-[9px]">
            {stageStandingProgressLabel(summary, duplicate, legacyAggregate)}
          </Badge>
      ) : undefined}
    >
      {!summary || summary.rows.length === 0 ? (
        <EmptyState text="本阶段暂无排名" className="py-8" />
      ) : (
        <DataTable
          className="rounded-none border-0"
          scrollLabel="阶段排名与晋级表"
        >
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-12 px-2">名次</TableHead>
                <TableHead className="px-2">Bot</TableHead>
                <TableHead className="w-14 px-2 text-right">积分</TableHead>
                <TableHead className="w-20 px-2 text-right">晋级</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {summary.rows.map((row) => (
                <TableRow key={row.entry_id}>
                  <TableCell className="px-2 py-2 font-mono text-xs text-muted-foreground">
                    {row.group_id ? `${row.group_id}-${row.rank}` : row.rank}
                  </TableCell>
                  <TableCell className="max-w-0 px-2 py-2">
                    {row.bot_id ? (
                      <Link to={`/bot/${row.bot_id}`} className="block min-w-0 text-xs hover:text-primary">
                        <EntityName tooltip={row.bot_name || '未命名 Bot'} tooltipFocusable={false} className="text-xs">
                          {row.bot_name || '未命名 Bot'}
                        </EntityName>
                      </Link>
                    ) : <span className="text-xs text-muted-foreground">已删除 Bot</span>}
                    <OverflowText tooltip={false} className="text-[10px] text-muted-foreground">
                      {row.owner_name ? `@${row.owner_display || row.owner_name}` : '参赛身份不可用'}
                    </OverflowText>
                    <span className="block font-mono text-xs leading-relaxed text-muted-foreground">
                      <span className="block font-sans font-medium text-foreground">
                        {scoringCountBreakdown(row, duplicate, legacyAggregate)}
                      </span>
                      {scoreBreakdown(row)}
                    </span>
                  </TableCell>
                  <TableCell className="px-2 py-2 text-right font-mono text-xs font-semibold text-primary">
                    {row.points}
                  </TableCell>
                  <TableCell className="px-2 py-2 text-right">
                    {advancementLabel(row.advancement)}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </DataTable>
      )}
    </DataRegion>
  )
}

/** swiss/循环/分组的对阵展示：按 round_num（或 group_id）折叠分组，大规模默认收起。 */
function PairingFoldedList({
  pairings,
  stageType,
  duplicate,
  legacyAggregate,
}: {
  pairings: Pairing[]
  stageType?: string
  duplicate: boolean
  legacyAggregate: boolean
}) {
  // 按 group_id 优先分组（分组赛），否则按 round_num（swiss/循环）
  const groups = useMemo(() => {
    const hasSeries = pairings.some((pairing) => (pairing.series_size ?? 1) > 1)
    const hasGroup = pairings.some((p) => p.group_id)
    const keyFn = hasSeries
      ? (pairing: Pairing) => contestPairingSeriesKey(pairing, stageType)
      : hasGroup
      ? (p: Pairing) => p.group_id || '—'
      : (p: Pairing) => `第 ${(p.round_num ?? 1)} 轮`
    const map = new Map<string, Pairing[]>()
    for (const p of pairings) {
      const k = keyFn(p)
      if (!map.has(k)) map.set(k, [])
      map.get(k)!.push(p)
    }
    return Array.from(map.entries()).map(([key, items]) => ({
      key,
      label: hasSeries
        ? duplicate
          ? `本对交锋 ${items[0]?.series_size ?? items.length} 组复式`
          : legacyAggregate
            ? `本对交锋 ${items[0]?.series_size ?? items.length} 场历史系列对局`
          : `本对交锋 ${items[0]?.series_size ?? items.length} 场计分`
        : key,
      items: hasSeries
        ? items.sort((a, b) => (a.series_index ?? 1) - (b.series_index ?? 1))
        : items,
    }))
  }, [duplicate, legacyAggregate, pairings, stageType])

  // 大规模（>6 组或任一组 >12 场）默认全部收起
  const big = groups.length > 6 || pairings.length > 60
  const [open, setOpen] = useState<Record<string, boolean>>(() =>
    Object.fromEntries(groups.map(({ key }) => [key, !big])),
  )
  const groupKey = groups.map(({ key }) => key).join('\u0000')

  // React 会在同一详情组件内切换阶段；新阶段不应继承上一阶段的折叠状态。
  useEffect(() => {
    const keys = groupKey ? groupKey.split('\u0000') : []
    setOpen(Object.fromEntries(keys.map((key) => [key, !big])))
  }, [groupKey, big])

  return (
    <div className="space-y-2">
      {groups.map(({ key: k, label, items: ps }, groupIndex) => {
        const panelId = `pairing-group-${groupIndex}`
        return (
        <Card key={k} density="compact" className="gap-0 overflow-hidden shadow-none">
          <Button
            type="button"
            variant="ghost"
            onClick={() => setOpen((o) => ({ ...o, [k]: !o[k] }))}
            aria-expanded={Boolean(open[k])}
            aria-controls={panelId}
            className="h-auto min-h-11 w-full justify-start whitespace-normal rounded-none px-3 py-2 text-left sm:min-h-[var(--control-height)]"
          >
            {open[k] ? <ChevronDown aria-hidden="true" className="size-4" /> : <ChevronRight aria-hidden="true" className="size-4" />}
            <OverflowText tooltip={label} tooltipFocusable={false} className="min-w-0 flex-1">{label}</OverflowText>
            <Badge variant="secondary" className="shrink-0">
              {ps.length} {duplicate ? '组' : legacyAggregate ? '场历史系列对局' : '场'}
            </Badge>
            <span className="ml-auto hidden shrink-0 text-xs text-muted-foreground sm:inline">
              {ps.filter((p) => effectivePairingStatus(p) === 'completed').length} {duplicate ? '组' : legacyAggregate ? '场历史系列对局' : '场'}已完成
            </span>
          </Button>
          {open[k] && (
            <div id={panelId} className="divide-y border-t">
              {ps.map((p) => {
                const status = effectivePairingStatus(p)
                const states = p.is_bye === true && status === 'completed'
                  ? ['winner', 'neutral'] as const
                  : outcomeParticipantStates(p.outcome)
                return (
                  <div key={p.id} className="grid min-w-0 gap-2 px-3 py-2 text-sm sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center">
                    <MatchParticipants
                      source={p}
                      states={states}
                      secondEmptyLabel={p.is_bye === true ? '轮空 (bye)' : undefined}
                    />
                    <div className="flex min-w-0 flex-wrap items-center gap-1.5 sm:justify-end">
                      <StatusBadge status={status || 'pending'} />
                      {status === 'completed' && (
                        <PairingResult pairing={p} primaryOnly={legacyAggregate} />
                      )}
                      {p.scheduled_at && (
                        <span className="text-xs text-muted-foreground">{fmtTime(p.scheduled_at)}</span>
                      )}
                      {p.match_id && (
                        <Button asChild variant="ghost" size="xs" className="min-h-11 text-primary sm:min-h-8">
                          <Link to={`/match/${p.match_id}`} aria-label={duplicate ? '查看复式回放' : legacyAggregate ? '查看历史对局' : '查看计分场'}>
                            {duplicate ? '复式回放' : legacyAggregate ? '历史对局' : '查看'}
                          </Link>
                        </Button>
                      )}
                    </div>
                  </div>
                )
              })}
            </div>
          )}
        </Card>
      )})}
    </div>
  )
}
