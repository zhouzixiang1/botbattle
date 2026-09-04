import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { ListFilter, Plus, Trophy, X } from 'lucide-react'
import { useAuth } from '@/components/useAuth'
import { DataRegion, PageFrame, PageHeader, StickyToolbar } from '@/components/layout'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { EntityName, Identifier, OverflowText } from '@/components/ui/overflow-text'
import { EmptyState, ErrorMsg, Loading, StatusBadge } from '@/components/ui/status'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Switch } from '@/components/ui/switch'
import { apiFetch, apiJson, errMsg } from '@/api'
import { GAMES, findGame, gameLabel } from '@/lib/games'
import { fmtTime } from '@/lib/format'
import Countdown from '@/components/Countdown'
import Pagination from '@/components/Pagination'
import { toast } from 'sonner'
import {
  StageSeriesSettingsEditor,
  defaultStageSeriesSettings,
  stageSeriesSettingsValid,
  type StageSeriesConfig,
  type StageSeriesSettings,
} from '@/components/contest/stage-series-settings'
import { TemplateGuidancePanel } from '@/components/contest/template-guidance-panel'
import type {
  ContestTemplateGuidance,
  ContestTemplatePurpose,
  ContestTemplateTimeClass,
} from '@/components/contest/template-guidance'
import {
  parseTimeControlRegistry,
  timeControlDescription,
  timeControlLabel,
  type TimeControlOption,
} from '@/lib/time-controls'
import {
  parseStageFormatConfigs,
  type StageFormatConfig,
} from '@/lib/contest-format'

interface Contest {
  id: number
  title: string
  status: string
  description?: string
  created_at?: string
  template_id?: string
  template_name?: string
  game_id?: string
  require_real_name?: number
  registration_opens_at?: string | null
  registration_closes_at?: string | null
  starts_at?: string | null
  official_results_ready?: boolean | number
}

/** 状态相关的时间提示文案 + 倒计时目标 */
function scheduleHint(c: Contest): { label: string; time?: string | null } | null {
  const now = Date.now()
  const future = (t?: string | null) => t && new Date(t).getTime() > now
  if (c.status === 'draft' && future(c.registration_opens_at))
    return { label: '距开放报名', time: c.registration_opens_at }
  if (c.status === 'open' && future(c.registration_closes_at))
    return { label: '距报名截止', time: c.registration_closes_at }
  if (c.status === 'published' && future(c.starts_at))
    return { label: '距开赛', time: c.starts_at }
  if (c.status === 'rest') return { label: '休息中', time: undefined }
  return null
}

/** 比赛对局参数概要（游戏规则已钉死固定值：holdem 70 手、棋类单局）。 */
function matchConfigSummary(c: Contest): string {
  return findGame(c.game_id)?.matchFormatLabel ?? '规则不可用'
}

interface Template extends ContestTemplateGuidance {
  id: string
  name: string
  game_id: string
  summary?: string
  recommended?: boolean
  recommended_min?: number | null
  recommended_max?: number | null
  purpose?: ContestTemplatePurpose | null
  time_class?: ContestTemplateTimeClass | null
  stages?: Array<{ key?: string; duplicate?: boolean; type?: string; tiebreak?: string | null }>
  games_per_pair_config?: {
    default: number
    min: number
    max: number
  }
  stage_series_configs?: StageSeriesConfig[]
  time_controls?: unknown[]
  default_time_control_id?: string
  stage_format_configs?: unknown
  requires_source_contest?: boolean
  allows_navigation_source_contest?: boolean
}

type StageFormatSettings = Record<string, { group_count: number }>

interface ContestListQuery {
  gameId: string
  page: number
}

// Radix Select 必须全生命周期保持受控且不能用空字符串；该值不可能通过后端
// template id 校验（id 必须以字母开头），仅用于 loading/empty 占位。
const TEMPLATE_PENDING_VALUE = '__template_pending__'
const NO_SOURCE_CONTEST_VALUE = '__no_source_contest__'
const EMPTY_STAGE_SERIES_CONFIGS: StageSeriesConfig[] = []
const EMPTY_STAGE_FORMAT_CONFIGS: StageFormatConfig[] = []
const EMPTY_TIME_CONTROLS: TimeControlOption[] = []
const SOURCE_CANDIDATE_LIMIT = 50
const PENCIL_ONLINE_CONTROL = 'pencil_per_decision_1s_v1'
const PENCIL_OFFLINE_CONTROL = 'pencil_per_side_total_900s_v1'

interface SourceContestCandidate {
  id: number
  title: string
}

interface SourceContestCandidates {
  candidates: SourceContestCandidate[]
  hasMore: boolean
}

function parseSourceContestCandidates(value: unknown): SourceContestCandidates {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new Error('关联赛事列表格式无效')
  }
  const row = value as Record<string, unknown>
  if (
    Object.keys(row).some((key) => key !== 'candidates' && key !== 'has_more')
    || !Array.isArray(row.candidates)
    || row.candidates.length > SOURCE_CANDIDATE_LIMIT
    || typeof row.has_more !== 'boolean'
  ) {
    throw new Error('关联赛事候选格式无效')
  }
  const candidates: SourceContestCandidate[] = []
  const seen = new Set<number>()
  for (const candidate of row.candidates) {
    if (typeof candidate !== 'object' || candidate === null || Array.isArray(candidate)) {
      throw new Error('关联赛事候选行格式无效')
    }
    const item = candidate as Record<string, unknown>
    if (
      Object.keys(item).some((key) => key !== 'id' && key !== 'title')
      || typeof item.id !== 'number'
      || !Number.isSafeInteger(item.id)
      || item.id < 1
      || seen.has(item.id)
      || typeof item.title !== 'string'
      || !item.title
      || item.title !== item.title.trim()
      || Array.from(item.title).some((char) => char.charCodeAt(0) < 32 || char.charCodeAt(0) === 127)
    ) {
      throw new Error('关联赛事候选行格式无效')
    }
    seen.add(item.id)
    candidates.push({ id: item.id, title: item.title })
  }
  return { candidates, hasMore: row.has_more }
}

export default function Contests() {
  const { user, isLoggedIn } = useAuth()
  const [list, setList] = useState<Contest[]>([])
  const [templates, setTemplates] = useState<Template[]>([])
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [templateId, setTemplateId] = useState('')
  const [templatesForGame, setTemplatesForGame] = useState('')
  const [templatesLoading, setTemplatesLoading] = useState(true)
  const [templateError, setTemplateError] = useState('')
  const [filterGame, setFilterGame] = useState('')
  const [formGameId, setFormGameId] = useState('holdem')
  const [requireRealName, setRequireRealName] = useState(false)
  const [gamesPerPair, setGamesPerPair] = useState(1)
  const [stageSeriesSettings, setStageSeriesSettings] = useState<StageSeriesSettings>({})
  const [timeControlId, setTimeControlId] = useState('')
  const [stageFormatSettings, setStageFormatSettings] = useState<StageFormatSettings>({})
  const [sourceContestId, setSourceContestId] = useState('')
  const [sourceContestQuery, setSourceContestQuery] = useState('')
  const [sourceContests, setSourceContests] = useState<SourceContestCandidate[]>([])
  const [sourceContestsHasMore, setSourceContestsHasMore] = useState(false)
  const [sourceContestsLoading, setSourceContestsLoading] = useState(false)
  const [sourceContestsError, setSourceContestsError] = useState('')
  // 时间编排（datetime-local 字符串；空=不设，手动触发对应阶段）
  const [regOpensAt, setRegOpensAt] = useState('')
  const [regClosesAt, setRegClosesAt] = useState('')
  const [startsAt, setStartsAt] = useState('')
  const [listError, setListError] = useState('')
  const [formError, setFormError] = useState('')
  const [listLoading, setListLoading] = useState(true)
  const [creating, setCreating] = useState(false)
  const [showCreate, setShowCreate] = useState(false)
  const creatingRef = useRef(false)
  const currentListQueryRef = useRef<ContestListQuery>({ gameId: '', page: 1 })
  const listRequestSeqRef = useRef(0)
  const listAbortRef = useRef<AbortController | null>(null)
  const templateRequestSeqRef = useRef(0)
  const templateAbortRef = useRef<AbortController | null>(null)
  const sourceRequestSeqRef = useRef(0)
  const sourceAbortRef = useRef<AbortController | null>(null)
  // 分页
  const [page, setPage] = useState(1)
  const [total, setTotal] = useState(0)
  const perPage = 20
  const canCreate = user?.role === 'organizer' || user?.role === 'admin'
  // 建赛表单：游戏由用户选（不再从模板反推）；规则参数已钉死，无需动态配置 UI。

  const load = ({ gameId, page: requestedPage }: ContestListQuery) => {
    const requestSeq = ++listRequestSeqRef.current
    const controller = new AbortController()
    listAbortRef.current?.abort()
    listAbortRef.current = controller
    setListLoading(true)
    setListError('')
    const params = new URLSearchParams()
    if (gameId) params.set('game_id', gameId)
    params.set('page', String(requestedPage))
    params.set('per_page', String(perPage))
    return apiFetch<{ contests: Contest[]; total?: number; page?: number }>(
      '/api/contests?' + params.toString(),
      { signal: controller.signal },
    )
      .then((d) => {
        if (controller.signal.aborted || requestSeq !== listRequestSeqRef.current) return
        setList(d.contests || [])
        if (d.total !== undefined) setTotal(d.total)
      })
      .catch((e) => {
        if (controller.signal.aborted || requestSeq !== listRequestSeqRef.current) return
        setListError(errMsg(e))
      })
      .finally(() => {
        if (controller.signal.aborted || requestSeq !== listRequestSeqRef.current) return
        if (listAbortRef.current === controller) listAbortRef.current = null
        setListLoading(false)
      })
  }

  useEffect(() => {
    // React StrictMode 会同步执行 effect→cleanup→effect。延后一拍让探测性
    // 首轮只清 timer，不制造一条没有业务意义的 ERR_ABORTED；真实筛选/
    // 翻页仍由 load() 内的 generation + AbortController 取消上一代请求。
    const startTimer = window.setTimeout(() => {
      void load({ gameId: filterGame, page })
    }, 0)
    return () => {
      window.clearTimeout(startTimer)
      ++listRequestSeqRef.current
      listAbortRef.current?.abort()
      listAbortRef.current = null
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filterGame, page])

  useEffect(() => {
    // 模板按建赛表单选中的游戏过滤（后端 ?game= 已支持）。请求代次 + Abort
    // 双重防护快速切换时的乱序响应，旧游戏模板不得回填到当前表单。
    const requestedGame = formGameId
    const requestSeq = ++templateRequestSeqRef.current
    const controller = new AbortController()
    templateAbortRef.current?.abort()
    templateAbortRef.current = controller
    setTemplates([])
    setTemplateId('')
    setTemplatesForGame('')
    setTemplateError('')
    setTemplatesLoading(true)

    // 延后一拍启动：React StrictMode 会同步执行 effect→cleanup→effect。这样首轮
    // 探测只清 timer，不制造一条无业务意义的 ERR_ABORTED；真实切换仍会 abort
    // 已经发出的上一代请求。
    const startTimer = window.setTimeout(() => {
      void apiFetch<{ templates: Template[] }>(
        '/api/contests/templates?game=' + encodeURIComponent(requestedGame),
        { method: 'GET', signal: controller.signal },
      )
        .then((d) => {
          if (controller.signal.aborted || requestSeq !== templateRequestSeqRef.current) return
          // 即使服务端过滤契约回退，也绝不让异游戏模板进入可提交状态。
          const tpls = (d.templates || []).filter((t) => t.game_id === requestedGame)
          setTemplates(tpls)
          setTemplateId(tpls.find((template) => template.recommended)?.id || tpls[0]?.id || '')
          setTemplatesForGame(requestedGame)
        })
        .catch((err: unknown) => {
          if (controller.signal.aborted || requestSeq !== templateRequestSeqRef.current) return
          setTemplateError(errMsg(err, '模板加载失败'))
        })
        .finally(() => {
          if (controller.signal.aborted || requestSeq !== templateRequestSeqRef.current) return
          setTemplatesLoading(false)
        })
    }, 0)

    return () => {
      window.clearTimeout(startTimer)
      controller.abort()
      if (templateAbortRef.current === controller) templateAbortRef.current = null
    }
  }, [formGameId])

  const onFormGameChange = (nextGame: string) => {
    if (nextGame === formGameId) return
    // 在 effect 清理前同步作废旧请求和旧选择，关闭“切游戏后立即提交”的窗口。
    ++templateRequestSeqRef.current
    templateAbortRef.current?.abort()
    setTemplates([])
    setTemplateId('')
    setTemplatesForGame('')
    setTemplateError('')
    setTemplatesLoading(true)
    setGamesPerPair(1)
    setStageSeriesSettings({})
    setTimeControlId('')
    setStageFormatSettings({})
    setSourceContestId('')
    setSourceContestQuery('')
    setSourceContests([])
    setSourceContestsHasMore(false)
    setSourceContestsError('')
    setFormGameId(nextGame)
  }

  const selectedTemplate = templates.find((t) => t.id === templateId)
  const stageSeriesConfigs = selectedTemplate?.stage_series_configs || EMPTY_STAGE_SERIES_CONFIGS
  const gamesPerPairConfig = stageSeriesConfigs.length > 0 ? undefined : selectedTemplate?.games_per_pair_config
  const selectedTemplateIsDuplicate = selectedTemplate?.stages?.some((stage) => stage.duplicate) === true
  const templateTimeRegistry = useMemo(() => (
    selectedTemplate
      ? parseTimeControlRegistry({
        game_id: selectedTemplate.game_id,
        time_controls: selectedTemplate.time_controls,
        default_time_control_id: selectedTemplate.default_time_control_id,
      })
      : null
  ), [selectedTemplate])
  const timeControls = templateTimeRegistry?.time_controls || EMPTY_TIME_CONTROLS
  const selectedTimeControl = timeControls.find((control) => control.id === timeControlId)
  const parsedStageFormatConfigs = useMemo(
    () => parseStageFormatConfigs(selectedTemplate?.stage_format_configs),
    [selectedTemplate],
  )
  const stageFormatConfigs = parsedStageFormatConfigs || EMPTY_STAGE_FORMAT_CONFIGS
  const stageFormatContractReady = parsedStageFormatConfigs !== null && stageFormatConfigs.every((config) => (
    selectedTemplate?.stages?.some((stage) => (
      stage.key === config.stage_key
      && (stage.type === 'group_round_robin' || stage.type === 'group_double_round_robin')
    )) === true
  ))
  const sourceRequirementContractReady = selectedTemplate?.requires_source_contest === undefined
    || typeof selectedTemplate.requires_source_contest === 'boolean'
  const sourceNavigationContractReady = selectedTemplate?.allows_navigation_source_contest === undefined
    || typeof selectedTemplate.allows_navigation_source_contest === 'boolean'
  const requiresSourceContest = selectedTemplate?.requires_source_contest === true
  const allowsNavigationSourceContest = selectedTemplate?.allows_navigation_source_contest === true
  const supportsSourceContest = requiresSourceContest || allowsNavigationSourceContest
  const templateCapabilitiesReady = stageFormatContractReady
    && sourceRequirementContractReady
    && sourceNavigationContractReady
    && !(requiresSourceContest && allowsNavigationSourceContest)
    && (!requiresSourceContest || selectedTemplate?.game_id === 'gomoku')
    && (!allowsNavigationSourceContest || selectedTemplate?.game_id === 'pencil')
  const templatePlaceholder = templatesLoading ? '模板加载中…'
    : templateError ? '模板加载失败'
    : '选择模板'
  const templateReady = !templatesLoading &&
    templatesForGame === formGameId &&
    selectedTemplate?.game_id === formGameId
  const gamesPerPairReady = !gamesPerPairConfig || (
    Number.isInteger(gamesPerPair) &&
    gamesPerPair >= gamesPerPairConfig.min &&
    gamesPerPair <= gamesPerPairConfig.max
  )
  const stageSeriesReady = stageSeriesConfigs.length === 0 ||
    stageSeriesSettingsValid(stageSeriesConfigs, stageSeriesSettings)
  const timeControlReady = templateTimeRegistry !== null && selectedTimeControl !== undefined
  const stageFormatReady = templateCapabilitiesReady && stageFormatConfigs.every((config) => {
    const value = stageFormatSettings[config.stage_key]?.group_count
    return Number.isInteger(value) && value >= config.min && (config.max == null || value <= config.max)
  })
  const selectedSourceContestExists = sourceContests.some(
    (contest) => String(contest.id) === sourceContestId,
  )
  const sourceContestReady = requiresSourceContest
    ? sourceContestId !== '' && selectedSourceContestExists
    : !allowsNavigationSourceContest || sourceContestId === '' || selectedSourceContestExists

  useEffect(() => {
    setGamesPerPair(gamesPerPairConfig?.default ?? 1)
    setStageSeriesSettings(defaultStageSeriesSettings(stageSeriesConfigs))
    setStageFormatSettings(Object.fromEntries(stageFormatConfigs.map((config) => [
      config.stage_key,
      { group_count: config.min },
    ])))
    setTimeControlId((current) => (
      timeControls.some((control) => control.id === current)
        ? current
        : templateTimeRegistry?.default_time_control_id || ''
    ))
    setSourceContestId('')
    setSourceContestQuery('')
  }, [templateId, gamesPerPairConfig?.default, stageSeriesConfigs, stageFormatConfigs, timeControls, templateTimeRegistry?.default_time_control_id])

  useEffect(() => {
    const requestSeq = ++sourceRequestSeqRef.current
    sourceAbortRef.current?.abort()
    sourceAbortRef.current = null
    if (!supportsSourceContest) {
      setSourceContests([])
      setSourceContestsHasMore(false)
      setSourceContestsError('')
      setSourceContestsLoading(false)
      return
    }
    const controller = new AbortController()
    sourceAbortRef.current = controller
    setSourceContests([])
    setSourceContestsHasMore(false)
    setSourceContestsError('')
    setSourceContestsLoading(true)
    const params = new URLSearchParams({
      game_id: formGameId,
      limit: String(SOURCE_CANDIDATE_LIMIT),
    })
    const normalizedQuery = sourceContestQuery.trim()
    if (normalizedQuery) params.set('query', normalizedQuery)
    // Debounce typing while preserving a single bounded request per settled
    // query. Abort + generation prevent stale results or errors from crossing
    // game/capability/query changes even when a transport ignores cancellation.
    const startTimer = window.setTimeout(() => {
      void apiFetch<unknown>(
        `/api/contests/source-candidates?${params.toString()}`,
        { signal: controller.signal },
      )
        .then((payload) => {
          if (controller.signal.aborted || requestSeq !== sourceRequestSeqRef.current) return
          const parsed = parseSourceContestCandidates(payload)
          setSourceContests(parsed.candidates)
          setSourceContestsHasMore(parsed.hasMore)
        })
        .catch((error: unknown) => {
          if (controller.signal.aborted || requestSeq !== sourceRequestSeqRef.current) return
          setSourceContestsError(errMsg(error, '关联赛事列表加载失败'))
        })
        .finally(() => {
          if (controller.signal.aborted || requestSeq !== sourceRequestSeqRef.current) return
          if (sourceAbortRef.current === controller) sourceAbortRef.current = null
          setSourceContestsLoading(false)
        })
    }, 150)
    return () => {
      window.clearTimeout(startTimer)
      controller.abort()
      if (sourceAbortRef.current === controller) sourceAbortRef.current = null
    }
  }, [formGameId, sourceContestQuery, supportsSourceContest])

  /** datetime-local 值（如 2026-01-01T14:00）→ ISO 秒级字符串（后端 naive 本地时间约定） */
  const toIso = (v: string): string | undefined => {
    if (!v) return undefined
    // datetime-local 已是 YYYY-MM-DDTHH:MM，补秒
    return v.length === 16 ? v + ':00' : v
  }

  const onCreate = async (e: FormEvent) => {
    e.preventDefault()
    if (creatingRef.current) return
    if (!templateReady) {
      setFormError(templateError || '请等待当前游戏的模板加载完成后再创建赛事')
      return
    }
    if (!gamesPerPairReady) {
      setFormError(selectedTemplateIsDuplicate
        ? '请选择模板允许的每对选手复式交锋组数'
        : '请选择模板允许的每对选手计分场数')
      return
    }
    if (!stageSeriesReady) {
      setFormError('请选择模板允许的逐阶段公平性设置')
      return
    }
    if (!timeControlReady) {
      setFormError('当前赛制的时限配置不可用，请重新加载后再创建赛事')
      return
    }
    if (!templateCapabilitiesReady) {
      setFormError('当前赛制的分组或来源能力格式无效，请重新加载')
      return
    }
    if (!stageFormatReady) {
      setFormError('请填写赛制允许的分组数量')
      return
    }
    if (!sourceContestReady) {
      setFormError('请选择一场已完成且正式榜完整的五子棋模拟赛')
      return
    }
    creatingRef.current = true
    setCreating(true)
    setFormError('')
    try {
      await apiJson('/api/contests', 'POST', {
        title,
        description,
        template_id: templateId,
        game_id: formGameId,
        require_real_name: requireRealName,
        registration_opens_at: toIso(regOpensAt),
        registration_closes_at: toIso(regClosesAt),
        starts_at: toIso(startsAt),
        time_control_id: timeControlId,
        ...(stageFormatConfigs.length > 0
          ? { stage_format_settings: stageFormatSettings }
          : {}),
        ...(supportsSourceContest && sourceContestId
          ? { source_contest_id: Number(sourceContestId) }
          : {}),
        ...(stageSeriesConfigs.length > 0
          ? { stage_series_settings: stageSeriesSettings }
          : gamesPerPairConfig
            ? { games_per_pair: gamesPerPair }
            : {}),
      })
      setTitle('')
      setDescription('')
      setRegOpensAt('')
      setRegClosesAt('')
      setStartsAt('')
      setGamesPerPair(gamesPerPairConfig?.default ?? 1)
      setStageSeriesSettings(defaultStageSeriesSettings(stageSeriesConfigs))
      setTimeControlId(templateTimeRegistry?.default_time_control_id || '')
      setStageFormatSettings(Object.fromEntries(stageFormatConfigs.map((config) => [
        config.stage_key,
        { group_count: config.min },
      ])))
      setSourceContestId('')
      setSourceContestQuery('')
      setSourceContestsHasMore(false)
      await load(currentListQueryRef.current)
      setShowCreate(false)
      toast.success('赛事创建成功')
    } catch (err) {
      setFormError(errMsg(err))
    } finally {
      creatingRef.current = false
      setCreating(false)
    }
  }

  return (
    <PageFrame layout="public-contests">
      <PageHeader
        title="锦标赛"
        description="浏览报名、排期、对阵与正式结果。"
      />

      <StickyToolbar label="赛事筛选与创建">
        <ListFilter className="size-4 shrink-0 text-muted-foreground" />
        <span className="shrink-0 text-xs font-medium text-muted-foreground">游戏</span>
        <Select
          value={filterGame || 'all'}
          onValueChange={(value) => {
            const gameId = value === 'all' ? '' : value
            currentListQueryRef.current = { gameId, page: 1 }
            setFilterGame(gameId)
            setPage(1)
          }}
        >
          <SelectTrigger className="min-h-11 w-[8.5rem] max-w-full sm:min-h-[var(--control-height)]"><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="all">全部游戏</SelectItem>
            {GAMES.map((game) => <SelectItem key={game.id} value={game.id}>{game.label}</SelectItem>)}
          </SelectContent>
        </Select>
        <span className="text-xs tabular-nums text-muted-foreground sm:ml-auto">
          共 {total} 项赛事 · 第 {page}/{Math.max(1, Math.ceil(total / perPage))} 页
        </span>
        {canCreate && isLoggedIn && (
          <Button
            type="button"
            size="sm"
            variant={showCreate ? 'secondary' : 'default'}
            className="min-h-11 sm:min-h-[var(--control-height)]"
            aria-expanded={showCreate}
            aria-controls="contest-create-panel"
            onClick={() => { setShowCreate((value) => !value); setFormError('') }}
          >
            {showCreate ? <X aria-hidden="true" className="size-4" /> : <Plus aria-hidden="true" className="size-4" />}
            {showCreate ? '收起创建区' : '创建赛事'}
          </Button>
        )}
      </StickyToolbar>

      {canCreate && isLoggedIn && showCreate && (
        <DataRegion
          id="contest-create-panel"
          title="创建赛事"
          description="新赛事统一使用赛事沙箱，每个 Bot 2 核 / 2 GiB；只计赛事成绩，不计平台排行榜。"
        >
          <form onSubmit={(event) => void onCreate(event)} className="min-w-0 space-y-3 p-3">
            {formError && <ErrorMsg msg={formError} />}
            {selectedTemplate && !templateCapabilitiesReady && (
              <ErrorMsg msg="当前赛制的分组或来源能力格式无效；已停止创建。" />
            )}
            <div className="grid min-w-0 gap-3 md:grid-cols-2 xl:grid-cols-[9rem_minmax(15rem,1.3fr)_minmax(13rem,1fr)_minmax(14rem,1fr)_minmax(14rem,1fr)]">
              <div className="min-w-0 space-y-1.5">
                <Label>游戏</Label>
                <Select value={formGameId} onValueChange={onFormGameChange}>
                  <SelectTrigger className="min-h-11 w-full sm:min-h-[var(--control-height)]"><SelectValue /></SelectTrigger>
                  <SelectContent>{GAMES.map((game) => <SelectItem key={game.id} value={game.id}>{game.label}</SelectItem>)}</SelectContent>
                </Select>
              </div>
              <div className="min-w-0 space-y-1.5">
                <Label>赛制</Label>
                <Select
                  value={templateId || TEMPLATE_PENDING_VALUE}
                  onValueChange={(value) => { if (value && value !== TEMPLATE_PENDING_VALUE) setTemplateId(value) }}
                  disabled={templatesLoading || templates.length === 0}
                >
                  <SelectTrigger className="min-h-11 w-full sm:min-h-[var(--control-height)]" aria-label="赛制" aria-describedby="contest-template-summary">
                    <SelectValue>
                      {selectedTemplate
                        ? `${selectedTemplate.name}${selectedTemplate.recommended ? ' · 推荐' : ''}`
                        : templatePlaceholder}
                    </SelectValue>
                  </SelectTrigger>
                  <SelectContent>
                    {templates.map((template) => (
                      <SelectItem key={template.id} value={template.id}>
                        {template.name}{template.recommended ? ' · 推荐' : ''}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <p id="contest-template-summary" className="text-sm leading-relaxed text-muted-foreground">
                  {selectedTemplate?.summary || '模板决定对阵覆盖、计分场数与预计耗时。'}
                </p>
                <TemplateGuidancePanel
                  template={selectedTemplate}
                  templates={templates}
                />
                {templateError && <ErrorMsg msg={templateError} className="text-xs" />}
              </div>
              <div className="min-w-0 space-y-1.5">
                <Label>对局时限</Label>
                <Select
                  value={timeControlId || TEMPLATE_PENDING_VALUE}
                  onValueChange={(value) => { if (value !== TEMPLATE_PENDING_VALUE) setTimeControlId(value) }}
                  disabled={!templateTimeRegistry || timeControls.length <= 1}
                >
                  <SelectTrigger
                    className="min-h-11 w-full sm:min-h-[var(--control-height)]"
                    aria-label="对局时限"
                    aria-describedby="contest-time-control-help"
                  >
                    <SelectValue>
                      {selectedTimeControl ? timeControlLabel(selectedTimeControl) : '时限加载中…'}
                    </SelectValue>
                  </SelectTrigger>
                  <SelectContent>
                    {timeControls.map((control) => (
                      <SelectItem key={control.id} value={control.id}>
                        {timeControlLabel(control)}{control.is_default ? ' · 默认' : ''}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <p id="contest-time-control-help" className="text-sm leading-relaxed text-muted-foreground">
                  {selectedTimeControl
                    ? timeControlDescription(selectedTimeControl)
                    : '只接受当前游戏公开的固定时限；发布后不可修改。'}
                </p>
              </div>
              <div className="space-y-1.5"><Label htmlFor="contest-title">标题</Label><Input id="contest-title" className="min-h-11 sm:min-h-[var(--control-height)]" value={title} onChange={(event) => setTitle(event.target.value)} required /></div>
              <div className="space-y-1.5"><Label htmlFor="contest-desc">说明</Label><Input id="contest-desc" className="min-h-11 sm:min-h-[var(--control-height)]" value={description} onChange={(event) => setDescription(event.target.value)} /></div>
            </div>
            {formGameId === 'pencil' && (
              <fieldset className="rounded-lg border border-primary/20 bg-primary/[0.035] p-3">
                <legend className="px-1 text-xs font-semibold text-foreground">点格棋用途预设</legend>
                <p className="mb-2 text-xs leading-relaxed text-muted-foreground">
                  预设只组合赛制与时限；预赛和决赛仍是两场独立赛事，不复制名单或自动晋级。
                </p>
                <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4" role="group" aria-label="点格棋赛制与时限快捷预设">
                  {([
                    ['pencil_drr', PENCIL_ONLINE_CONTROL, '线上预赛 · 全员双循环'],
                    ['pencil_group_drr', PENCIL_ONLINE_CONTROL, '线上预赛 · 分组双循环'],
                    ['pencil_drr', PENCIL_OFFLINE_CONTROL, '线下决赛 · 全员双循环'],
                    ['pencil_group_drr', PENCIL_OFFLINE_CONTROL, '线下决赛 · 分组双循环'],
                  ] as const).map(([nextTemplate, nextControl, label]) => (
                    <Button
                      key={`${nextTemplate}-${nextControl}`}
                      type="button"
                      variant={templateId === nextTemplate && timeControlId === nextControl ? 'default' : 'outline'}
                      className="min-h-11 min-w-0 whitespace-normal text-left leading-snug"
                      onClick={() => {
                        setTemplateId(nextTemplate)
                        setTimeControlId(nextControl)
                      }}
                      disabled={!templates.some((template) => template.id === nextTemplate)}
                    >
                      {label}
                    </Button>
                  ))}
                </div>
              </fieldset>
            )}
            {gamesPerPairConfig && (
              <fieldset className="grid min-w-0 gap-3 rounded-lg border border-border bg-muted/20 p-3 sm:grid-cols-[minmax(12rem,15rem)_minmax(0,1fr)] sm:items-end">
                <legend className="sr-only">
                  {selectedTemplateIsDuplicate ? '每对选手复式交锋组数' : '每对选手计分场数'}
                </legend>
                <div className="min-w-0 space-y-1.5">
                  <span id="contest-games-per-pair-label" className="block text-sm font-medium leading-none">
                    {selectedTemplateIsDuplicate ? '每对选手复式交锋组数' : '每对选手计分场数'}
                  </span>
                  <Select value={String(gamesPerPair)} onValueChange={(value) => setGamesPerPair(Number(value))}>
                    <SelectTrigger
                      className="min-h-11 w-full"
                      aria-labelledby="contest-games-per-pair-label"
                      aria-describedby="contest-games-per-pair-help"
                    >
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {Array.from(
                        { length: gamesPerPairConfig.max - gamesPerPairConfig.min + 1 },
                        (_, index) => gamesPerPairConfig.min + index,
                      ).map((count) => (
                        <SelectItem key={count} value={String(count)}>
                          {count} {selectedTemplateIsDuplicate ? '组' : '场'}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                <p id="contest-games-per-pair-help" className="text-sm leading-relaxed text-muted-foreground">
                  {selectedTemplateIsDuplicate
                    ? `${gamesPerPair} 组复式交锋 · ${gamesPerPair * 2} 场计分。每组由两场同牌换座的 70 手计分场组成，两场分别计胜、平、负。`
                    : `每对选手进行 ${gamesPerPair} 场计分，每场独立判定胜、平、负；场数越多，发牌与座位波动越小，总赛程也越长。`}
                </p>
              </fieldset>
            )}
            {stageSeriesConfigs.length > 0 && (
              <div className="overflow-hidden rounded-lg border border-border bg-muted/20">
                <StageSeriesSettingsEditor
                  configs={stageSeriesConfigs}
                  value={stageSeriesSettings}
                  onChange={setStageSeriesSettings}
                />
              </div>
            )}
            {(stageFormatConfigs.length > 0 || supportsSourceContest) && (
              <div className="grid min-w-0 gap-3 rounded-lg border border-border bg-muted/20 p-3 md:grid-cols-2">
                {stageFormatConfigs.map((config) => {
                  const value = stageFormatSettings[config.stage_key]?.group_count ?? config.min
                  return (
                    <div key={`${config.stage_key}-${config.field}`} className="min-w-0 space-y-1.5">
                      <Label htmlFor={`contest-${config.stage_key}-group-count`}>分组数量</Label>
                      <Input
                        id={`contest-${config.stage_key}-group-count`}
                        type="number"
                        inputMode="numeric"
                        min={config.min}
                        max={config.max ?? undefined}
                        step={1}
                        value={value}
                        onChange={(event) => {
                          const groupCount = Number(event.target.value)
                          setStageFormatSettings((current) => ({
                            ...current,
                            [config.stage_key]: { group_count: groupCount },
                          }))
                        }}
                        aria-describedby={`contest-${config.stage_key}-group-count-help`}
                        className="min-h-11 sm:min-h-[var(--control-height)]"
                      />
                      <p id={`contest-${config.stage_key}-group-count-help`} className="text-xs leading-relaxed text-muted-foreground">
                        发布时至少 {config.min} 组且每组至少 2 人；不满足时拒绝发布，不会静默缩减。抽签只执行一次，组间人数差不超过 1。
                      </p>
                    </div>
                  )
                })}
                {supportsSourceContest && (
                  <div className="min-w-0 space-y-1.5">
                    <Label htmlFor="contest-source-query">
                      {requiresSourceContest ? '搜索保护种子来源' : '搜索关联赛事'}
                    </Label>
                    <Input
                      id="contest-source-query"
                      value={sourceContestQuery}
                      onChange={(event) => {
                        setSourceContestQuery(event.target.value)
                        setSourceContestId('')
                      }}
                      maxLength={100}
                      placeholder="输入标题或精确赛事 ID；留空显示最近候选"
                      autoComplete="off"
                      className="min-h-11 sm:min-h-[var(--control-height)]"
                      aria-describedby="contest-source-search-help"
                    />
                    <p id="contest-source-search-help" className="text-xs leading-relaxed text-muted-foreground">
                      每次最多返回 {SOURCE_CANDIDATE_LIMIT} 项；结果过多时请缩小关键词。
                    </p>
                    <Label>{requiresSourceContest ? '保护种子来源模拟赛' : '关联赛事（可选）'}</Label>
                    <Select
                      value={sourceContestId || (allowsNavigationSourceContest ? NO_SOURCE_CONTEST_VALUE : TEMPLATE_PENDING_VALUE)}
                      onValueChange={(value) => {
                        if (value === NO_SOURCE_CONTEST_VALUE) setSourceContestId('')
                        else if (value !== TEMPLATE_PENDING_VALUE) setSourceContestId(value)
                      }}
                      disabled={sourceContestsLoading || sourceContests.length === 0}
                    >
                      <SelectTrigger
                        className="min-h-11 w-full sm:min-h-[var(--control-height)]"
                        aria-label={requiresSourceContest ? '保护种子来源模拟赛' : '关联赛事（可选）'}
                        aria-describedby="contest-source-help"
                      >
                        <SelectValue>
                          {sourceContestId
                            ? sourceContests.find((contest) => String(contest.id) === sourceContestId)?.title || '来源赛事不可用'
                            : sourceContestsLoading
                              ? requiresSourceContest ? '正在读取已完成模拟赛…' : '正在读取同游戏赛事…'
                              : sourceContests.length > 0
                                ? requiresSourceContest ? '选择模拟赛' : '不关联其他赛事'
                                : requiresSourceContest ? '暂无可用模拟赛' : '不关联其他赛事'}
                        </SelectValue>
                      </SelectTrigger>
                      <SelectContent>
                        {allowsNavigationSourceContest && (
                          <SelectItem value={NO_SOURCE_CONTEST_VALUE}>不关联其他赛事</SelectItem>
                        )}
                        {sourceContests.map((contest) => (
                          <SelectItem key={contest.id} value={String(contest.id)}>
                            {contest.title} · #{contest.id}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                    <p id="contest-source-help" className="text-xs leading-relaxed text-muted-foreground">
                      {requiresSourceContest
                        ? '仅列出已结束且正式榜完整的五子棋赛事。发布时按来源榜顺序从已报名选手中递补 4 或 5 名保护种子。'
                        : '仅建立两场独立点格棋赛事之间的导航；不要求来源已结束，不复制名单、成绩或晋级关系。'}
                    </p>
                    {sourceContestsHasMore && !sourceContestsLoading && !sourceContestsError && (
                      <p role="status" className="text-xs leading-relaxed text-amber-700 dark:text-amber-300">
                        候选超过 {SOURCE_CANDIDATE_LIMIT} 项，请输入更具体的标题或精确赛事 ID。
                      </p>
                    )}
                    {sourceContestsError && <ErrorMsg msg={sourceContestsError} className="text-xs" />}
                  </div>
                )}
              </div>
            )}
            <div className="grid min-w-0 items-end gap-3 lg:grid-cols-[minmax(15rem,0.8fr)_minmax(0,2fr)_auto]">
              <div className="flex min-w-0 min-h-[var(--control-height)] items-start gap-2 rounded-lg border px-3 py-2">
                <Switch id="contest-realname" checked={requireRealName} onCheckedChange={setRequireRealName} />
                <div className="min-w-0"><Label htmlFor="contest-realname" className="cursor-pointer">要求实名报名</Label><p className="mt-0.5 text-xs text-muted-foreground">报名时核验完整实名资料。</p></div>
              </div>
              <fieldset className="min-w-0">
                <legend className="mb-1.5 text-xs font-medium text-muted-foreground">时间编排</legend>
                <div className="grid min-w-0 gap-2 sm:grid-cols-3">
                  <div className="space-y-1"><Label htmlFor="contest-opens" className="text-xs">开放报名</Label><Input id="contest-opens" className="min-h-11 sm:min-h-[var(--control-height)]" type="datetime-local" value={regOpensAt} onChange={(event) => setRegOpensAt(event.target.value)} /></div>
                  <div className="space-y-1"><Label htmlFor="contest-closes" className="text-xs">报名截止</Label><Input id="contest-closes" className="min-h-11 sm:min-h-[var(--control-height)]" type="datetime-local" value={regClosesAt} onChange={(event) => setRegClosesAt(event.target.value)} /></div>
                  <div className="space-y-1"><Label htmlFor="contest-starts" className="text-xs">比赛开始</Label><Input id="contest-starts" className="min-h-11 sm:min-h-[var(--control-height)]" type="datetime-local" value={startsAt} onChange={(event) => setStartsAt(event.target.value)} /></div>
                </div>
              </fieldset>
              <Button
                type="submit"
                disabled={creating || !templateReady || !gamesPerPairReady || !stageSeriesReady || !timeControlReady || !templateCapabilitiesReady || !stageFormatReady || !sourceContestReady}
                aria-busy={creating}
                className="min-h-11 w-full lg:w-auto"
              >
                <Plus className="size-4" />{creating ? '创建中…' : '创建赛事'}
              </Button>
            </div>
          </form>
        </DataRegion>
      )}

      <DataRegion
        title="赛事列表"
        description={`按创建时间排列真实赛事；每页 ${perPage} 项。`}
      >
            {listError ? (
              <ErrorMsg msg={listError} className="px-4 py-6" />
            ) : listLoading ? (
              <Loading text="正在加载赛事…" />
            ) : list.length === 0 ? (
              <EmptyState text="当前条件下暂无赛事" icon={<Trophy className="size-5 opacity-50" />} className="py-8" />
            ) : (
              <ul className="divide-y divide-border">
                {list.map((contest, index) => {
                  const hint = scheduleHint(contest)
                  const templateName = contest.template_name || templates.find((template) => template.id === contest.template_id)?.name
                  const liveAvailable = ['published', 'running', 'rest'].includes(contest.status)
                  return (
                    <li key={contest.id} className="grid min-w-0 gap-2 px-3 py-2.5 sm:grid-cols-[2rem_minmax(0,1fr)_auto] sm:items-center">
                      <span className="hidden font-mono text-xs tabular-nums text-muted-foreground sm:block">{(page - 1) * perPage + index + 1}</span>
                      <div className="min-w-0">
                        <div className="flex min-w-0 flex-wrap items-center gap-2">
                          <Link to={`/contests/${contest.id}`} className="min-w-0 flex-1 hover:text-primary">
                            <EntityName lines={2} tooltip={false} tooltipFocusable={false} className="text-sm hover:text-primary">{contest.title}</EntityName>
                          </Link>
                          <StatusBadge status={contest.status} />
                        </div>
                        {contest.description && <OverflowText lines={2} tooltip={false} className="mt-1 text-xs text-muted-foreground">{contest.description}</OverflowText>}
                        <div className="mt-1 flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
                          {templateName ? <OverflowText className="max-w-48" tooltip={templateName}>{templateName}</OverflowText> : <Identifier>{contest.template_id || '—'}</Identifier>}
                          <span>{gameLabel(contest.game_id)}</span>
                          <span>{matchConfigSummary(contest)}</span>
                          <time className="font-mono tabular-nums">{fmtTime(contest.created_at)}</time>
                          {hint && <span className="inline-flex min-w-0 items-center gap-1 font-medium text-primary">{hint.label}{hint.time && <Countdown endsAt={hint.time} />}</span>}
                        </div>
                      </div>
                      <Button asChild variant={liveAvailable ? 'outline' : 'ghost'} size="sm" className="min-h-11 sm:min-h-[var(--control-height)]">
                        <Link to={liveAvailable ? `/contests/${contest.id}/live` : `/contests/${contest.id}`}>
                          {liveAvailable ? '进入直播' : '查看赛事'}
                        </Link>
                      </Button>
                    </li>
                  )
                })}
              </ul>
            )}
      </DataRegion>
      <Pagination
        page={page}
        perPage={perPage}
        total={total}
        onPageChange={(nextPage) => {
          currentListQueryRef.current = { gameId: filterGame, page: nextPage }
          setPage(nextPage)
        }}
      />
    </PageFrame>
  )
}
