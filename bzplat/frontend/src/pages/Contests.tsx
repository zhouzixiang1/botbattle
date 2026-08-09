import { useEffect, useRef, useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { Trophy, Plus } from 'lucide-react'
import PageStub from '@/components/PageStub'
import { useAuth } from '@/components/useAuth'
import { Card, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { EmptyState, ErrorMsg, StatusBadge } from '@/components/ui/status'
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
  const [error, setError] = useState('')
  const [creating, setCreating] = useState(false)
  const creatingRef = useRef(false)
  const templateRequestSeqRef = useRef(0)
  const templateAbortRef = useRef<AbortController | null>(null)
  // 分页
  const [page, setPage] = useState(1)
  const [total, setTotal] = useState(0)
  const perPage = 20
  const canCreate = user?.role === 'organizer' || user?.role === 'admin'
  // 建赛表单：游戏由用户选（不再从模板反推）；规则参数已钉死，无需动态配置 UI。

  const load = () => {
    const params = new URLSearchParams()
    if (filterGame) params.set('game_id', filterGame)
    params.set('page', String(page))
    params.set('per_page', String(perPage))
    return apiGet<{ contests: Contest[]; total?: number; page?: number }>(
      '/api/contests?' + params.toString(),
    )
      .then((d) => {
        setList(d.contests || [])
        if (d.total !== undefined) setTotal(d.total)
      })
      .catch((e) => setError(errMsg(e)))
  }

  useEffect(() => {
    void load()
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
      setError(templateError || '请等待当前游戏的模板加载完成后再创建赛事')
      return
    }
    creatingRef.current = true
    setCreating(true)
    setError('')
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
      await load()
      toast.success('赛事创建成功')
    } catch (err) {
      setError(errMsg(err))
    } finally {
      creatingRef.current = false
      setCreating(false)
    }
  }

  return (
    <PageStub
      title="锦标赛"
      subtitle="组织者发布锦标赛，选手派遣 Bot；默认模板偏 Swiss / 分组，适合校赛规模"
      actions={
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          游戏
          <Select value={filterGame || 'all'} onValueChange={(v) => { setFilterGame(v === 'all' ? '' : v); setPage(1) }}>
            <SelectTrigger className="h-9 w-[8.5rem]">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">全部</SelectItem>
              {GAMES.map((g) => (
                <SelectItem key={g.id} value={g.id}>
                  {g.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      }
    >
      {error && <ErrorMsg msg={error} className="mb-3" />}

      {canCreate && isLoggedIn && (
        <Card className="mb-6">
          <CardContent>
            <form onSubmit={(e) => void onCreate(e)} className="flex flex-wrap items-end gap-3">
              {/* 先选游戏 → 再选该游戏的模板 */}
              <div className="space-y-1.5">
                <Label>游戏</Label>
                <Select value={formGameId} onValueChange={onFormGameChange}>
                  <SelectTrigger className="mt-1.5 h-9 w-[8.5rem]">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {GAMES.map((g) => (
                      <SelectItem key={g.id} value={g.id}>{g.label}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="contest-title">标题</Label>
                <Input
                  id="contest-title"
                  className="w-48"
                  value={title}
                  onChange={(e) => setTitle(e.target.value)}
                  required
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="contest-desc">说明</Label>
                <Input
                  id="contest-desc"
                  className="w-56"
                  value={description}
                  onChange={(e) => setDescription(e.target.value)}
                />
              </div>
              <div className="space-y-1.5">
                <Label>模板</Label>
                <Select
                  value={templateId || TEMPLATE_PENDING_VALUE}
                  onValueChange={(value) => {
                    if (value && value !== TEMPLATE_PENDING_VALUE) setTemplateId(value)
                  }}
                  disabled={templatesLoading || templates.length === 0}
                >
                  <SelectTrigger className="mt-1.5 h-9 w-full">
                    <SelectValue>{selectedTemplate?.name || templatePlaceholder}</SelectValue>
                  </SelectTrigger>
                  <SelectContent>
                    {templates.map((t) => (
                      <SelectItem key={t.id} value={t.id}>
                        {t.name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                {templateError && (
                  <p className="max-w-56 text-xs text-destructive">{templateError}</p>
                )}
              </div>
              <div className="flex items-center gap-2">
                <Switch checked={requireRealName} onCheckedChange={setRequireRealName} />
                <Label htmlFor="contest-realname" className="cursor-pointer text-sm">
                  要求实名报名（报名者须填写姓名/手机号/学校/学号）
                </Label>
              </div>
              {/* 时间编排（可选；留空=手动触发对应阶段） */}
              <div className="flex flex-wrap items-end gap-3 border-t border-border pt-3">
                <span className="self-center text-xs font-medium text-muted-foreground">
                  时间编排（可选，留空=手动）：
                </span>
                <label className="space-y-1 text-xs text-muted-foreground">
                  <span>开放报名时间</span>
                  <Input type="datetime-local" value={regOpensAt} onChange={(e) => setRegOpensAt(e.target.value)} className="h-9" />
                </label>
                <label className="space-y-1 text-xs text-muted-foreground">
                  <span>报名截止时间</span>
                  <Input type="datetime-local" value={regClosesAt} onChange={(e) => setRegClosesAt(e.target.value)} className="h-9" />
                </label>
                <label className="space-y-1 text-xs text-muted-foreground">
                  <span>比赛开始时间</span>
                  <Input type="datetime-local" value={startsAt} onChange={(e) => setStartsAt(e.target.value)} className="h-9" />
                </label>
              </div>
              <Button type="submit" disabled={creating || !templateReady} className="gap-1.5">
                <Plus className="size-4" />
                {creating ? '创建中…' : '创建比赛'}
              </Button>
            </form>
          </CardContent>
        </Card>
      )}

      <Card className="gap-0 py-0">
        {list.length === 0 ? (
          <EmptyState text="暂无比赛" icon={<Trophy className="size-7 opacity-40" />} />
        ) : (
          <ul className="divide-y divide-border">
            {list.map((c) => (
              <li key={c.id} className="min-w-0 px-4 py-3">
                <div className="flex min-w-0 items-center gap-2">
                  <Link
                    to={`/contests/${c.id}`}
                    className="min-w-0 shrink truncate text-lg font-medium text-primary hover:underline"
                    title={c.title}
                  >
                    {c.title}
                  </Link>
                  <StatusBadge status={c.status} />
                </div>
                <div className="mt-1 flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
                  <span className="max-w-full truncate" title={c.template_id || undefined}>
                    {templates.find((t) => t.id === c.template_id)?.name || c.template_id || '—'}
                  </span>
                  <span>·</span>
                  <span>{gameLabel(c.game_id)}</span>
                  <span>·</span>
                  <span>{matchConfigSummary(c)}</span>
                  {c.created_at && (
                    <>
                      <span>·</span>
                      <span>{fmtTime(c.created_at)}</span>
                    </>
                  )}
                </div>
                {(() => {
                  const hint = scheduleHint(c)
                  if (!hint) return null
                  return (
                    <div className="mt-1 flex items-center gap-1 text-xs text-primary">
                      <span className="font-medium">{hint.label}</span>
                      {hint.time && <Countdown endsAt={hint.time} />}
                    </div>
                  )
                })()}
              </li>
            ))}
          </ul>
        )}
        <Pagination page={page} perPage={perPage} total={total} onPageChange={setPage} />
      </Card>
    </PageStub>
  )
}
