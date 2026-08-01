import { useEffect, useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { Trophy, Plus } from 'lucide-react'
import PageStub from '@/components/PageStub'
import { useAuth } from '@/components/useAuth'
import { Card, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Badge } from '@/components/ui/badge'
import { EmptyState, ErrorMsg } from '@/components/ui/status'
import { apiGet, apiJson, errMsg } from '@/api'
import { GAMES, gameLabel } from '@/lib/games'

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
}

/** 解析比赛对局参数概要（按游戏展示）。 */
function matchConfigSummary(c: Contest): string {
  const gid = c.game_id || 'holdem'
  let cfg: Record<string, unknown> = {}
  try {
    cfg = c.match_config_json ? JSON.parse(c.match_config_json) : {}
  } catch {
    cfg = {}
  }
  if (gid === 'holdem') {
    const h = (cfg.hands as number) || c.hands_per_match || 70
    return `${h} 手`
  }
  if (gid === 'pencil') {
    return `${cfg.n_dots || 11} 点`
  }
  return '单局'
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
  const [hands, setHands] = useState(70)
  const [nDots, setNDots] = useState(11)
  const [templateId, setTemplateId] = useState('holdem_swiss_ko')
  const [filterGame, setFilterGame] = useState('')
  const [error, setError] = useState('')
  const canCreate = user?.role === 'organizer' || user?.role === 'admin'
  // 当前所选模板对应的游戏（决定 match_config 字段）
  const selGame = templates.find((t) => t.id === templateId)?.game_id || 'holdem'

  const load = () =>
    apiGet<{ contests: Contest[] }>('/api/contests')
      .then((d) => {
        const rows = d.contests || []
        setList(
          filterGame ? rows.filter((c) => (c.game_id || 'holdem') === filterGame) : rows,
        )
      })
      .catch((e) => setError(errMsg(e)))

  useEffect(() => {
    void load()
    apiGet<{ templates: Template[] }>('/api/contests/templates')
      .then((d) => setTemplates(d.templates || []))
      .catch(() => undefined)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filterGame])

  const onCreate = async (e: FormEvent) => {
    e.preventDefault()
    setError('')
    try {
      // 按所选模板的游戏组装 match_config（取代德扑专属 hands_per_match）
      const match_config =
        selGame === 'holdem'
          ? { hands }
          : selGame === 'pencil'
            ? { n_dots: nDots }
            : {} // gomoku 单局，无可调参数
      await apiJson('/api/contests', 'POST', {
        title,
        description,
        template_id: templateId,
        match_config,
      })
      setTitle('')
      setDescription('')
      await load()
    } catch (err) {
      setError(errMsg(err))
    }
  }

  const selectCls =
    'mt-1.5 h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm text-foreground shadow-xs focus:outline-none focus:ring-2 focus:ring-ring'
  const inlineSelectCls =
    'h-9 rounded-md border border-input bg-transparent px-3 text-sm text-foreground shadow-xs focus:outline-none focus:ring-2 focus:ring-ring'

  return (
    <PageStub
      title="比赛"
      subtitle="组织者发布比赛，选手派遣 Bot；默认模板偏 Swiss / 分组，适合校赛规模"
      actions={
        <label className="flex items-center gap-2 text-sm text-muted-foreground">
          游戏
          <select
            value={filterGame}
            onChange={(e) => setFilterGame(e.target.value)}
            className={inlineSelectCls}
          >
            <option value="">全部</option>
            {GAMES.map((g) => (
              <option key={g.id} value={g.id}>
                {g.label}
              </option>
            ))}
          </select>
        </label>
      }
    >
      {error && <ErrorMsg msg={error} className="mb-3" />}

      {canCreate && isLoggedIn && (
        <Card className="mb-6">
          <CardContent>
            <form onSubmit={(e) => void onCreate(e)} className="flex flex-wrap items-end gap-3">
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
                <Label htmlFor="contest-template">模板</Label>
                <select
                  id="contest-template"
                  className={selectCls}
                  value={templateId}
                  onChange={(e) => setTemplateId(e.target.value)}
                >
                  {templates.map((t) => (
                    <option key={t.id} value={t.id}>
                      {t.name}（{gameLabel(t.game_id)}）
                    </option>
                  ))}
                </select>
              </div>
              {selGame === 'holdem' && (
                <div className="space-y-1.5">
                  <Label htmlFor="contest-hands">手数</Label>
                  <Input
                    id="contest-hands"
                    type="number"
                    min={1}
                    max={200}
                    className="w-24"
                    value={hands}
                    onChange={(e) => setHands(Number(e.target.value))}
                  />
                </div>
              )}
              {selGame === 'pencil' && (
                <div className="space-y-1.5">
                  <Label htmlFor="contest-ndots">点阵边长</Label>
                  <Input
                    id="contest-ndots"
                    type="number"
                    min={3}
                    max={15}
                    className="w-24"
                    value={nDots}
                    onChange={(e) => setNDots(Number(e.target.value))}
                  />
                </div>
              )}
              {selGame === 'gomoku' && (
                <span className="self-center text-xs text-muted-foreground">五子棋单局，无可调参数</span>
              )}
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
              <li key={c.id} className="px-4 py-3">
                <div className="flex flex-wrap items-center gap-2">
                  <Link
                    to={`/contests/${c.id}`}
                    className="text-lg font-medium text-primary hover:underline"
                  >
                    {c.title}
                  </Link>
                  <Badge variant="secondary">{c.status}</Badge>
                </div>
                <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
                  <span>{c.template_id || '—'}</span>
                  <span>·</span>
                  <span>{gameLabel(c.game_id)}</span>
                  <span>·</span>
                  <span>{matchConfigSummary(c)}</span>
                  {c.created_at && (
                    <>
                      <span>·</span>
                      <span>{c.created_at}</span>
                    </>
                  )}
                </div>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </PageStub>
  )
}
