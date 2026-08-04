import { useEffect, useState, type FormEvent } from 'react'
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
import { apiGet, apiJson, errMsg } from '@/api'
import { GAMES, gameLabel } from '@/lib/games'
import { fmtTime } from '@/lib/format'
import Countdown from '@/components/Countdown'
import { toast } from 'sonner'
import { getGame, defaultMatchConfig } from '@/games'

interface Contest {
  id: number
  title: string
  status: string
  description?: string
  hands_per_match?: number
  created_at?: string
  template_id?: string
  game_id?: string
  match_config_json?: string
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

/** 解析比赛对局参数概要（经注册表 configFields，消除 per-game if 分支）。 */
function matchConfigSummary(c: Contest): string {
  const gid = c.game_id || 'holdem'
  const spec = getGame(gid)
  let cfg: Record<string, unknown> = {}
  try {
    cfg = c.match_config_json ? JSON.parse(c.match_config_json) : {}
  } catch {
    cfg = {}
  }
  if (spec.configFields.length === 0) return '单局'
  // 展示该游戏所有可调参数（如 holdem "70 手"、pencil "6 点"）
  return spec.configFields
    .map((f) => {
      const v = (cfg[f.key] as number) ?? f.default
      const unit = f.key === 'hands' ? '手' : f.key === 'n_dots' ? '点' : ''
      return `${v} ${unit}`.trim()
    })
    .join(' / ')
}

interface Template {
  id: string
  name: string
  game_id: string
}

export default function Contests() {
  const { user, isLoggedIn } = useAuth()
  const [list, setList] = useState<Contest[]>([])
  const [templates, setTemplates] = useState<Template[]>([])
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  // 动态对局参数（按所选游戏的 configFields 驱动，取代散落的 hands/nDots 状态）
  const [matchCfg, setMatchCfg] = useState<Record<string, number>>({})
  const [templateId, setTemplateId] = useState('holdem_swiss_ko')
  const [filterGame, setFilterGame] = useState('')
  const [formGameId, setFormGameId] = useState('holdem')
  const [requireRealName, setRequireRealName] = useState(false)
  // 时间编排（datetime-local 字符串；空=不设，手动触发对应阶段）
  const [regOpensAt, setRegOpensAt] = useState('')
  const [regClosesAt, setRegClosesAt] = useState('')
  const [startsAt, setStartsAt] = useState('')
  const [error, setError] = useState('')
  const canCreate = user?.role === 'organizer' || user?.role === 'admin'
  // 建赛表单：游戏由用户选（不再从模板反推），决定 match_config 字段 + 模板可选集
  const selGame = formGameId
  const selSpec = getGame(selGame)
  // 切换游戏时重置动态参数为该游戏默认
  useEffect(() => {
    setMatchCfg(defaultMatchConfig(formGameId))
  }, [formGameId])

  const load = () =>
    apiGet<{ contests: Contest[] }>('/api/contests' + (filterGame ? `?game_id=${filterGame}` : ''))
      .then((d) => setList(d.contests || []))
      .catch((e) => setError(errMsg(e)))

  useEffect(() => {
    void load()
    // 模板按建赛表单选中的游戏过滤（后端 ?game= 已支持）
    apiGet<{ templates: Template[] }>('/api/contests/templates?game=' + formGameId)
      .then((d) => {
        const tpls = d.templates || []
        setTemplates(tpls)
        // 切游戏后重置 templateId 为该游戏第一个模板（避免 value 指向已不存在的模板）
        if (tpls.length > 0 && !tpls.some((t) => t.id === templateId)) {
          setTemplateId(tpls[0].id)
        }
      })
      .catch(() => undefined)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filterGame, formGameId])

  /** datetime-local 值（如 2026-01-01T14:00）→ ISO 秒级字符串（后端 naive 本地时间约定） */
  const toIso = (v: string): string | undefined => {
    if (!v) return undefined
    // datetime-local 已是 YYYY-MM-DDTHH:MM，补秒
    return v.length === 16 ? v + ':00' : v
  }

  const onCreate = async (e: FormEvent) => {
    e.preventDefault()
    setError('')
    try {
      await apiJson('/api/contests', 'POST', {
        title,
        description,
        template_id: templateId,
        game_id: formGameId,
        match_config: { ...matchCfg },
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
    }
  }

  return (
    <PageStub
      title="比赛"
      subtitle="组织者发布比赛，选手派遣 Bot；默认模板偏 Swiss / 分组，适合校赛规模"
      actions={
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          游戏
          <Select value={filterGame || 'all'} onValueChange={(v) => setFilterGame(v === 'all' ? '' : v)}>
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
                <Select value={formGameId} onValueChange={setFormGameId}>
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
                <Select value={templateId} onValueChange={setTemplateId}>
                  <SelectTrigger className="mt-1.5 h-9 w-full">
                    <SelectValue placeholder="选择模板" />
                  </SelectTrigger>
                  <SelectContent>
                    {templates.map((t) => (
                      <SelectItem key={t.id} value={t.id}>
                        {t.name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              {selSpec.configFields.map((f) => (
                <div key={f.key} className="space-y-1.5">
                  <Label htmlFor={`contest-${f.key}`}>{f.label}</Label>
                  <Input
                    id={`contest-${f.key}`}
                    type="number"
                    min={f.min}
                    max={f.max}
                    className="w-24"
                    value={matchCfg[f.key] ?? f.default}
                    onChange={(e) => setMatchCfg({ ...matchCfg, [f.key]: Number(e.target.value) })}
                  />
                </div>
              ))}
              {selSpec.configFields.length === 0 && (
                <span className="self-center text-xs text-muted-foreground">
                  {selSpec.label}单局，无可调参数
                </span>
              )}
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
              <Button type="submit" className="gap-1.5">
                <Plus className="size-4" />
                创建比赛
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
      </Card>
    </PageStub>
  )
}
