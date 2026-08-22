import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { Trophy, Users, Swords, ListOrdered, Play, DoorOpen, RefreshCw, Timer, ChevronDown, ChevronRight, Plus, Download, AlertTriangle, ArrowLeft, CalendarClock } from 'lucide-react'
import { DataRegion, PageFrame, PageHeader, StickyToolbar } from '@/components/layout'
import { MatchParticipants } from '@/components/MatchParticipants'
import { PairingResult } from '@/components/contest/pairing-result'
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
import { toast } from 'sonner'

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
}
interface Stage {
  key?: string
  type?: string
  scoring?: string
  rounds?: number
  group_count?: number
  advance_count?: number
  advance_per_group?: number
  rest_after_minutes?: number
  allow_bot_swap_in_rest?: boolean
  /** 复式赛制（duplicate）：每对阵 2 leg 同副牌交换座位合并判胜（仅 holdem） */
  duplicate?: boolean
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
  stage_idx?: number
  stage_key?: string
  group_id?: string
  match_winner?: number | null
  scheduled_at?: string | null
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
}
interface StageStandingSummary {
  stage_idx: number
  stage_key: string
  status: string
  source: 'persisted' | 'live' | 'scheduled' | 'pending'
  completed_pairings: number
  total_pairings: number
  advancement_final: boolean
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
    return JSON.parse(c.stages_json)
  } catch {
    return []
  }
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
  if (typeof tiebreaks.seed === 'number') values.push(`种子 ${tiebreaks.seed}`)
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
  const [estimate, setEstimate] = useState<{ estimated_matches?: number; eta_seconds?: number } | null>(null)
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

  // 复式赛制（duplicate）：任一阶段 duplicate=True 或模板名含 dup 时展示标记。
  // 仅 holdem 支持（后端 build_match_plan 判定），这里仅前端提示，不阻断。
  const isDuplicate = useMemo(
    () =>
      stages.some((s) => s.duplicate) ||
      !!(contest?.template_id || '').toLowerCase().includes('dup'),
    [stages, contest?.template_id],
  )

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
        estimate?: { estimated_matches?: number; eta_seconds?: number }
        entries_page?: number
        entries_per_page?: number
        entries_total?: number
        my_entry?: Entry | null
        is_organizer?: boolean
      }>(`/api/contests/${targetId}?entries_page=${targetEntriesPage}&entries_per_page=${entriesPerPage}`)
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
      if (p.status === 'completed') cur.completed += 1
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
            <Button asChild variant="outline" size="sm">
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
  const currentScoringLabel = SCORING_LABEL[
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
    '完赛后固化的权威结果；赛事积分不改变平台 Rating。实际战绩不把瑞士轮轮空记作胜场，轮空次数单独列出；同分行显示实际使用的破同分链。',
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
            <Button asChild variant="outline" size="sm">
              <Link to="/contests"><ArrowLeft aria-hidden="true" className="size-4" />返回赛事</Link>
            </Button>
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  type="button"
                  variant="outline"
                  size="icon-sm"
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
            {isDuplicate && <Badge variant="secondary">复式赛制</Badge>}
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
              <dd className="font-medium text-foreground">{templateLabel}{isDuplicate ? ' · 复式' : ''}</dd>
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
              <dt className="text-muted-foreground">对阵</dt>
              <dd className="font-mono font-medium tabular-nums text-foreground">
                {estimate?.estimated_matches != null ? `预计 ${estimate.estimated_matches}` : pairings.length}
              </dd>
            </div>
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
          <Button variant="outline" disabled={busyAction} onClick={() => void act(`/api/contests/${id}/publish`, undefined, '排期已发布')} className="gap-1.5">
            <ListOrdered className="size-4" />截止报名·出排期
          </Button>
        )}
        {isOrg && contest.status === 'published' && (
          <Button variant="outline" disabled={busyAction} onClick={() => void act(`/api/contests/${id}/start`, undefined, '比赛已开始')} className="gap-1.5">
            <Play className="size-4" />立即开赛
          </Button>
        )}
        {isOrg && contest.status === 'rest' && (
          <Button disabled={busyAction} onClick={() => void act(`/api/contests/${id}/resume`, undefined, '已进入下一阶段')}>结束休息 / 下一阶段</Button>
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
            <TabsTrigger value="matchups" className="gap-1.5">
              <Swords className="size-4" />对阵
            </TabsTrigger>
          )}
          <TabsTrigger value="entries" className="gap-1.5">
            <Users className="size-4" />选手
            {entriesTotal > 0 && (
              <span className="text-xs text-muted-foreground">{entriesTotal}</span>
            )}
          </TabsTrigger>
          {showStandings && (
            <TabsTrigger value="standings" className="gap-1.5">
              <ListOrdered className="size-4" />阶段积分
              {standings.length > 0 && (
                <span className="text-xs text-muted-foreground">{standings.length}</span>
              )}
            </TabsTrigger>
          )}
          {showOfficial && (
            <TabsTrigger value="official" className="gap-1.5">
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
                    const prog = stageProgress.get(i)
                    const typeLabel = STAGE_TYPE_LABEL[s.type || ''] || `阶段${i + 1}`
                    const roundTag = prog && prog.maxRound > 0 && prog.completed < prog.total
                      ? `第${prog.maxRound}轮`
                      : null
                    return (
                      <TabsTrigger key={s.key || i} value={String(i)} className="gap-1.5">
                        <span>{typeLabel}</span>
                        {roundTag && <span className="text-xs text-muted-foreground">· {roundTag}</span>}
                        {contest.current_stage_idx === i && contest.status !== 'finished' && (
                          <Badge variant="outline" className="ml-1 text-[9px] text-primary">当前</Badge>
                        )}
                        {prog && prog.total > 0 && (
                          <span className="text-[10px] text-muted-foreground">
                            {prog.completed}/{prog.total}
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
                  {[
                    STAGE_TYPE_LABEL[stages[stageTab].type || ''] || (stages[stageTab].type ? '自定义赛制' : null),
                    SCORING_LABEL[stages[stageTab].scoring || ''] || (stages[stageTab].scoring ? '自定义计分' : null),
                    stages[stageTab].duplicate ? '复式赛制（同副牌）' : null,
                    stages[stageTab].group_count ? `分组 ${stages[stageTab].group_count}` : null,
                    stages[stageTab].rounds !== undefined ? `轮数 ${stages[stageTab].rounds}` : null,
                    stages[stageTab].advance_count ? `晋级 ${stages[stageTab].advance_count}` : null,
                    stages[stageTab].advance_per_group ? `每组晋级 ${stages[stageTab].advance_per_group}` : null,
                    stages[stageTab].rest_after_minutes ? `休息 ${stages[stageTab].rest_after_minutes} 分` : null,
                    stages[stageTab].allow_bot_swap_in_rest ? '休息可换 Bot' : null,
                  ].filter(Boolean).join(' · ')}
                </OverflowText>
              )}
            </div>
          )}

          {/* 宽屏把对阵与阶段榜并排，窄屏自然上下排列。 */}
          <div className="grid min-w-0 items-stretch gap-3 xl:grid-cols-[minmax(0,1fr)_22rem]">
          <DataRegion
            title={`对阵${stages.length ? ` · ${STAGE_TYPE_LABEL[stages[stageTab]?.type || ''] || `阶段${stageTab + 1}`}` : ''}`}
            description={stagePairings.length > 0 ? `当前阶段共 ${stagePairings.length} 场对阵。` : '排期生成后将在这里显示。'}
            actions={stagePairings.length > 0 ? (
              <div role="group" aria-label="对阵视图" className="flex items-center gap-1 rounded-lg bg-muted p-0.5">
                <Button
                  type="button"
                  size="xs"
                  variant={pairingView === 'tree' ? 'secondary' : 'ghost'}
                  aria-pressed={pairingView === 'tree'}
                  onClick={() => setPairingView('tree')}
                >
                  {isElimStage ? '对阵树' : '分组视图'}
                </Button>
                <Button
                  type="button"
                  size="xs"
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
            {stagePairings.length === 0 ? (
              <EmptyState text="暂无对阵" icon={<Swords className="size-6 opacity-40" />} className="py-8" />
            ) : pairingView === 'table' ? (
              <ScheduleTable pairings={stagePairings} />
            ) : isElimStage ? (
              <BracketTree pairings={stagePairings} />
            ) : (
              <PairingFoldedList pairings={stagePairings} />
            )}
          </DataRegion>
          <StageStandingPanel summary={selectedStageStanding} />
          </div>

        </TabsContent>

        {/* Tab「选手」：报名列表（非实名组织者/admin 可批量指派；实名赛仅 admin 可审计代报名） */}
        <TabsContent value="entries" className="mt-2">
          <DataRegion
            title={`报名选手（${entriesTotal}）`}
            description={rosterDescription}
            actions={
              <>
                {canAssignEntries && !isShowcase && (contest.status === 'draft' || contest.status === 'open') && (
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
                          {e.seed ? <Badge variant="outline">种子 {e.seed}</Badge> : null}
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
            description={`赛事积分与平台 Rating 相互独立；本阶段计分：${currentScoringLabel}。胜/平/负仅统计真实对局，瑞士轮轮空会按胜场分加分，轮空次数单独列出。正式结果以完赛后的名次为准。`}
          >
              <DataTable className="rounded-none border-0 border-b" scrollLabel="阶段积分表">
                <Table className="min-w-[38rem]">
                  <TableHeader>
                    <TableRow>
                      <TableHead className="w-14">名次</TableHead>
                      <TableHead className="min-w-[6rem]">Bot</TableHead>
                      <TableHead>积分</TableHead>
                      <TableHead className="min-w-[13rem]">实际战绩 / 轮空</TableHead>
                      <TableHead className="hidden md:table-cell">累计分差</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {standings.length === 0 ? (
                      <TableRow><TableCell colSpan={5}><EmptyState text="暂无积分数据" icon={<Trophy className="size-7 opacity-40" />} /></TableCell></TableRow>
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
                            {scoreBreakdown(s)}
                          </TableCell>
                          <TableCell className="hidden font-mono text-xs text-muted-foreground md:table-cell">{s.delta_total}</TableCell>
                        </TableRow>
                      ))
                    )}
                  </TableBody>
                </Table>
              </DataTable>
              <div className="px-3 py-2">
                <Pagination
                  page={safeStandingsPage}
                  perPage={standingsPerPage}
                  total={standingsTotal}
                  onPageChange={setStandingsPage}
                />
              </div>
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
                      <TableHead className="min-w-[13rem]">计分构成</TableHead>
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
                          {scoreRow ? scoreBreakdown(scoreRow) : '历史计分构成不可用'}
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

function StageStandingPanel({ summary }: { summary?: StageStandingSummary }) {
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
            {summary.completed_pairings}/{summary.total_pairings} 场
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
function PairingFoldedList({ pairings }: { pairings: Pairing[] }) {
  // 按 group_id 优先分组（分组赛），否则按 round_num（swiss/循环）
  const groups = useMemo(() => {
    const hasGroup = pairings.some((p) => p.group_id)
    const keyFn = hasGroup
      ? (p: Pairing) => p.group_id || '—'
      : (p: Pairing) => `第 ${(p.round_num ?? 1)} 轮`
    const map = new Map<string, Pairing[]>()
    for (const p of pairings) {
      const k = keyFn(p)
      if (!map.has(k)) map.set(k, [])
      map.get(k)!.push(p)
    }
    return Array.from(map.entries())
  }, [pairings])

  // 大规模（>6 组或任一组 >12 场）默认全部收起
  const big = groups.length > 6 || pairings.length > 60
  const [open, setOpen] = useState<Record<string, boolean>>(() =>
    Object.fromEntries(groups.map(([k]) => [k, !big])),
  )
  const groupKey = groups.map(([key]) => key).join('\u0000')

  // React 会在同一详情组件内切换阶段；新阶段不应继承上一阶段的折叠状态。
  useEffect(() => {
    const keys = groupKey ? groupKey.split('\u0000') : []
    setOpen(Object.fromEntries(keys.map((key) => [key, !big])))
  }, [groupKey, big])

  return (
    <div className="space-y-2">
      {groups.map(([k, ps], groupIndex) => {
        const panelId = `pairing-group-${groupIndex}`
        return (
        <Card key={k} density="compact" className="gap-0 overflow-hidden shadow-none">
          <Button
            type="button"
            variant="ghost"
            onClick={() => setOpen((o) => ({ ...o, [k]: !o[k] }))}
            aria-expanded={Boolean(open[k])}
            aria-controls={panelId}
            className="h-auto min-h-[var(--control-height)] w-full justify-start whitespace-normal rounded-none px-3 py-2 text-left"
          >
            {open[k] ? <ChevronDown aria-hidden="true" className="size-4" /> : <ChevronRight aria-hidden="true" className="size-4" />}
            <OverflowText tooltip={k} tooltipFocusable={false} className="min-w-0 flex-1">{k}</OverflowText>
            <Badge variant="secondary" className="shrink-0">{ps.length} 场</Badge>
            <span className="ml-auto hidden shrink-0 text-xs text-muted-foreground sm:inline">
              {ps.filter((p) => p.status === 'completed').length} 已完成
            </span>
          </Button>
          {open[k] && (
            <div id={panelId} className="divide-y border-t">
              {ps.map((p) => {
                const w = p.match_winner
                return (
                  <div key={p.id} className="grid min-w-0 gap-2 px-3 py-2 text-sm sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center">
                    <MatchParticipants
                      source={p}
                      states={[
                        w === 0 ? 'winner' : w === 1 ? 'loser' : 'neutral',
                        w === 1 ? 'winner' : w === 0 && p.is_bye !== true ? 'loser' : 'neutral',
                      ]}
                      secondEmptyLabel={p.is_bye === true ? '轮空 (bye)' : undefined}
                    />
                    <div className="flex min-w-0 flex-wrap items-center gap-1.5 sm:justify-end">
                      <StatusBadge status={p.status || 'pending'} />
                      {p.status === 'completed' && <PairingResult pairing={p} />}
                      {p.scheduled_at && (
                        <span className="text-xs text-muted-foreground">{fmtTime(p.scheduled_at)}</span>
                      )}
                      {p.match_id && (
                        <Button asChild variant="ghost" size="xs" className="text-primary">
                          <Link to={`/match/${p.match_id}`}>查看</Link>
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
