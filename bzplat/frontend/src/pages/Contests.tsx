import { useEffect, useRef, useState, type FormEvent } from 'react'
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
import { apiFetch, apiGet, apiJson, errMsg } from '@/api'
import { GAMES, findGame, gameLabel } from '@/lib/games'
import { fmtTime } from '@/lib/format'
import Countdown from '@/components/Countdown'
import Pagination from '@/components/Pagination'
import { toast } from 'sonner'

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

interface Template {
  id: string
  name: string
  game_id: string
}

// Radix Select 必须全生命周期保持受控且不能用空字符串；该值不可能通过后端
// template id 校验（id 必须以字母开头），仅用于 loading/empty 占位。
const TEMPLATE_PENDING_VALUE = '__template_pending__'

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
  const templateRequestSeqRef = useRef(0)
  const templateAbortRef = useRef<AbortController | null>(null)
  // 分页
  const [page, setPage] = useState(1)
  const [total, setTotal] = useState(0)
  const perPage = 20
  const canCreate = user?.role === 'organizer' || user?.role === 'admin'
  // 建赛表单：游戏由用户选（不再从模板反推）；规则参数已钉死，无需动态配置 UI。

  const load = (requestedPage = page) => {
    setListLoading(true)
    setListError('')
    const params = new URLSearchParams()
    if (filterGame) params.set('game_id', filterGame)
    params.set('page', String(requestedPage))
    params.set('per_page', String(perPage))
    return apiGet<{ contests: Contest[]; total?: number; page?: number }>(
      '/api/contests?' + params.toString(),
    )
      .then((d) => {
        setList(d.contests || [])
        if (d.total !== undefined) setTotal(d.total)
      })
      .catch((e) => setListError(errMsg(e)))
      .finally(() => setListLoading(false))
  }

  useEffect(() => {
    void load(page)
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
          setTemplateId(tpls[0]?.id || '')
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
    setFormGameId(nextGame)
  }

  const selectedTemplate = templates.find((t) => t.id === templateId)
  const templatePlaceholder = templatesLoading ? '模板加载中…'
    : templateError ? '模板加载失败'
    : '选择模板'
  const templateReady = !templatesLoading &&
    templatesForGame === formGameId &&
    selectedTemplate?.game_id === formGameId

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
      })
      setTitle('')
      setDescription('')
      setRegOpensAt('')
      setRegClosesAt('')
      setStartsAt('')
      setPage(1)
      await load(1)
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
        <Select value={filterGame || 'all'} onValueChange={(value) => { setFilterGame(value === 'all' ? '' : value); setPage(1) }}>
          <SelectTrigger className="w-[8.5rem] max-w-full"><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="all">全部游戏</SelectItem>
            {GAMES.map((game) => <SelectItem key={game.id} value={game.id}>{game.label}</SelectItem>)}
          </SelectContent>
        </Select>
        <span className="text-xs tabular-nums text-muted-foreground sm:ml-auto">
          共 {total} 场 · 第 {page}/{Math.max(1, Math.ceil(total / perPage))} 页
        </span>
        {canCreate && isLoggedIn && (
          <Button
            type="button"
            size="sm"
            variant={showCreate ? 'secondary' : 'default'}
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
            <div className="grid min-w-0 gap-3 md:grid-cols-2 xl:grid-cols-[10rem_16rem_minmax(14rem,1fr)_minmax(14rem,1fr)]">
              <div className="min-w-0 space-y-1.5">
                <Label>游戏</Label>
                <Select value={formGameId} onValueChange={onFormGameChange}>
                  <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
                  <SelectContent>{GAMES.map((game) => <SelectItem key={game.id} value={game.id}>{game.label}</SelectItem>)}</SelectContent>
                </Select>
              </div>
              <div className="min-w-0 space-y-1.5">
                <Label>模板</Label>
                <Select
                  value={templateId || TEMPLATE_PENDING_VALUE}
                  onValueChange={(value) => { if (value && value !== TEMPLATE_PENDING_VALUE) setTemplateId(value) }}
                  disabled={templatesLoading || templates.length === 0}
                >
                  <SelectTrigger className="w-full"><SelectValue>{selectedTemplate?.name || templatePlaceholder}</SelectValue></SelectTrigger>
                  <SelectContent>{templates.map((template) => <SelectItem key={template.id} value={template.id}>{template.name}</SelectItem>)}</SelectContent>
                </Select>
                {templateError && <ErrorMsg msg={templateError} className="text-xs" />}
              </div>
              <div className="space-y-1.5"><Label htmlFor="contest-title">标题</Label><Input id="contest-title" value={title} onChange={(event) => setTitle(event.target.value)} required /></div>
              <div className="space-y-1.5"><Label htmlFor="contest-desc">说明</Label><Input id="contest-desc" value={description} onChange={(event) => setDescription(event.target.value)} /></div>
            </div>
            <div className="grid min-w-0 items-end gap-3 lg:grid-cols-[minmax(15rem,0.8fr)_minmax(0,2fr)_auto]">
              <div className="flex min-w-0 min-h-[var(--control-height)] items-start gap-2 rounded-lg border px-3 py-2">
                <Switch id="contest-realname" checked={requireRealName} onCheckedChange={setRequireRealName} />
                <div className="min-w-0"><Label htmlFor="contest-realname" className="cursor-pointer">要求实名报名</Label><p className="mt-0.5 text-xs text-muted-foreground">报名时核验完整实名资料。</p></div>
              </div>
              <fieldset className="min-w-0">
                <legend className="mb-1.5 text-xs font-medium text-muted-foreground">时间编排</legend>
                <div className="grid min-w-0 gap-2 sm:grid-cols-3">
                  <div className="space-y-1"><Label htmlFor="contest-opens" className="text-xs">开放报名</Label><Input id="contest-opens" type="datetime-local" value={regOpensAt} onChange={(event) => setRegOpensAt(event.target.value)} /></div>
                  <div className="space-y-1"><Label htmlFor="contest-closes" className="text-xs">报名截止</Label><Input id="contest-closes" type="datetime-local" value={regClosesAt} onChange={(event) => setRegClosesAt(event.target.value)} /></div>
                  <div className="space-y-1"><Label htmlFor="contest-starts" className="text-xs">比赛开始</Label><Input id="contest-starts" type="datetime-local" value={startsAt} onChange={(event) => setStartsAt(event.target.value)} /></div>
                </div>
              </fieldset>
              <Button type="submit" disabled={creating || !templateReady} aria-busy={creating} className="w-full lg:w-auto">
                <Plus className="size-4" />{creating ? '创建中…' : '创建赛事'}
              </Button>
            </div>
          </form>
        </DataRegion>
      )}

      <DataRegion
        title="赛事列表"
        description={`按创建时间排列真实赛事；每页 ${perPage} 场。`}
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
                      <Button asChild variant="ghost" size="sm"><Link to={`/contests/${contest.id}`}>查看赛事</Link></Button>
                    </li>
                  )
                })}
              </ul>
            )}
      </DataRegion>
      <Pagination page={page} perPage={perPage} total={total} onPageChange={setPage} />
    </PageFrame>
  )
}
