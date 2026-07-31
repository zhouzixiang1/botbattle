import { useEffect, useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import PageStub from '../components/PageStub'
import { useAuth } from '../components/useAuth'
import { apiGet, apiJson, errMsg } from '../api'
import { GAMES, gameLabel } from '../lib/games'

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

  return (
    <PageStub title="比赛">
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <p className="text-sm text-slate-400">
          组织者发布比赛，选手派遣 Bot。默认模板偏 Swiss / 分组，适合校赛规模。
        </p>
        <label className="ml-auto text-sm text-slate-500">
          游戏
          <select
            value={filterGame}
            onChange={(e) => setFilterGame(e.target.value)}
            className="ml-2 rounded-lg border border-slate-300 bg-white px-2 py-1.5 text-slate-700"
          >
            <option value="">全部</option>
            {GAMES.map((g) => (
              <option key={g.id} value={g.id}>
                {g.label}
              </option>
            ))}
          </select>
        </label>
      </div>
      {error && <p className="mb-3 text-sm text-error-500">{error}</p>}

      {canCreate && isLoggedIn && (
        <form
          onSubmit={(e) => void onCreate(e)}
          className="mb-6 flex flex-wrap items-end gap-3 rounded-xl border border-slate-200 bg-white p-4"
        >
          <label className="text-sm text-slate-600">
            标题
            <input
              className="mt-1 block rounded-lg border border-slate-300 bg-white px-3 py-2 text-slate-800"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              required
            />
          </label>
          <label className="text-sm text-slate-600">
            说明
            <input
              className="mt-1 block rounded-lg border border-slate-300 bg-white px-3 py-2 text-slate-800"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
          </label>
          <label className="text-sm text-slate-600">
            模板
            <select
              className="mt-1 block rounded-lg border border-slate-300 bg-white px-3 py-2 text-slate-800"
              value={templateId}
              onChange={(e) => setTemplateId(e.target.value)}
            >
              {templates.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.name}（{gameLabel(t.game_id)}）
                </option>
              ))}
            </select>
          </label>
          {selGame === 'holdem' && (
            <label className="text-sm text-slate-600">
              手数
              <input
                type="number"
                min={1}
                max={200}
                className="mt-1 block w-24 rounded-lg border border-slate-300 bg-white px-3 py-2 text-slate-800"
                value={hands}
                onChange={(e) => setHands(Number(e.target.value))}
              />
            </label>
          )}
          {selGame === 'pencil' && (
            <label className="text-sm text-slate-600">
              点阵边长
              <input
                type="number"
                min={3}
                max={15}
                className="mt-1 block w-24 rounded-lg border border-slate-300 bg-white px-3 py-2 text-slate-800"
                value={nDots}
                onChange={(e) => setNDots(Number(e.target.value))}
              />
            </label>
          )}
          {selGame === 'gomoku' && (
            <span className="self-center text-xs text-slate-400">五子棋单局，无可调参数</span>
          )}
          <button
            type="submit"
            className="rounded-lg bg-brand-600 px-4 py-2 text-sm text-white hover:bg-brand-500"
          >
            创建比赛
          </button>
        </form>
      )}

      <ul className="divide-y divide-slate-700/80 overflow-hidden rounded-xl border border-slate-200 bg-white">
        {list.map((c) => (
          <li key={c.id} className="px-4 py-3">
            <Link
              to={`/contests/${c.id}`}
              className="text-lg text-brand-700 hover:underline"
            >
              {c.title}
            </Link>
            <div className="text-xs text-slate-500">
              {c.status} · {c.template_id || '—'} · {gameLabel(c.game_id)} ·{' '}
              {matchConfigSummary(c)} · {c.created_at}
            </div>
          </li>
        ))}
        {!list.length && (
          <li className="px-4 py-8 text-center text-slate-500">暂无比赛</li>
        )}
      </ul>
    </PageStub>
  )
}
