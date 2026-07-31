import { useCallback, useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import PageStub from '../components/PageStub'
import { useAuth } from '../components/useAuth'
import { apiGet, apiJson, errMsg } from '../api'

interface Contest {
  id: number
  title: string
  description?: string
  status: string
  organizer_id: number
  hands_per_match?: number
}

interface Entry {
  id: number
  user_id: number
  bot_id: number
  registered_at?: string
}

interface Pairing {
  id: number
  round_num?: number
  bot_a_id: number
  bot_b_id: number
  match_id?: string | null
  status?: string
}

interface Standing {
  bot_id: number
  points: number
  wins: number
  draws: number
  losses: number
  net_chips: number
}

export default function ContestDetail() {
  const { id } = useParams()
  const { user, isLoggedIn } = useAuth()
  const [contest, setContest] = useState<Contest | null>(null)
  const [entries, setEntries] = useState<Entry[]>([])
  const [pairings, setPairings] = useState<Pairing[]>([])
  const [standings, setStandings] = useState<Standing[]>([])
  const [bots, setBots] = useState<Array<{ id: number; name: string; display_name?: string }>>(
    [],
  )
  const [botId, setBotId] = useState('')
  const [error, setError] = useState('')

  const load = useCallback(() => {
    if (!id) return
    apiGet<{
      contest: Contest
      entries: Entry[]
      pairings: Pairing[]
      standings: Standing[]
    }>(`/api/contests/${id}`)
      .then((d) => {
        setContest(d.contest)
        setEntries(d.entries || [])
        setPairings(d.pairings || [])
        setStandings(d.standings || [])
      })
      .catch((e) => setError(errMsg(e)))
  }, [id])

  useEffect(() => {
    void load()
    if (isLoggedIn) {
      apiGet<{ bots: Array<{ id: number; name: string; display_name?: string }> }>(
        '/api/bots/mine',
      )
        .then((d) => setBots(d.bots || []))
        .catch(() => undefined)
    }
  }, [load, isLoggedIn])

  const isOrg =
    !!user &&
    !!contest &&
    (user.role === 'admin' || user.id === contest.organizer_id)

  const act = async (path: string, body?: unknown) => {
    setError('')
    try {
      await apiJson(path, 'POST', body)
      await load()
    } catch (e) {
      setError(errMsg(e))
    }
  }

  if (!contest) {
    return (
      <PageStub title="比赛详情">
        <p className="text-slate-400">{error || '加载中…'}</p>
      </PageStub>
    )
  }

  return (
    <PageStub title="比赛详情">
      <div className="rounded-xl border border-slate-200 bg-white p-4">
        <h2 className="text-lg font-medium text-slate-900">{contest.title}</h2>
        <p className="mt-1 text-sm text-slate-400">{contest.description || '无说明'}</p>
        <p className="mt-2 text-xs text-slate-500">
          状态 <span className="text-brand-700">{contest.status}</span> · 每场{' '}
          {contest.hands_per_match ?? 70} 手
        </p>
      </div>
      {error && <p className="mt-3 text-sm text-error-500">{error}</p>}

      <div className="mt-4 flex flex-wrap gap-2">
        {isOrg && contest.status === 'draft' && (
          <button
            type="button"
            className="rounded-lg bg-brand-600 px-3 py-2 text-sm text-white"
            onClick={() => void act(`/api/contests/${id}/open`)}
          >
            开放报名
          </button>
        )}
        {isOrg && (contest.status === 'open' || contest.status === 'draft') && (
          <button
            type="button"
            className="rounded-lg border border-slate-300 px-3 py-2 text-sm text-slate-700 hover:bg-slate-100"
            onClick={() => void act(`/api/contests/${id}/start`)}
          >
            开始循环赛
          </button>
        )}
        {isLoggedIn && contest.status === 'open' && (
          <div className="flex flex-wrap items-center gap-2">
            <select
              className="rounded-lg border border-slate-300 bg-white px-2 py-2 text-sm"
              value={botId}
              onChange={(e) => setBotId(e.target.value)}
            >
              <option value="">选择我的 Bot</option>
              {bots.map((b) => (
                <option key={b.id} value={b.id}>
                  {b.display_name || b.name}
                </option>
              ))}
            </select>
            <button
              type="button"
              disabled={!botId}
              className="rounded-lg border border-slate-300 px-3 py-2 text-sm hover:bg-white disabled:opacity-40"
              onClick={() =>
                void act(`/api/contests/${id}/register`, { bot_id: Number(botId) })
              }
            >
              报名派遣
            </button>
          </div>
        )}
      </div>

      <h3 className="mt-8 text-sm font-medium text-slate-600">报名</h3>
      <ul className="mt-2 space-y-1 text-sm text-slate-400">
        {entries.length === 0 && <li>暂无报名</li>}
        {entries.map((e) => (
          <li key={e.id}>
            user={e.user_id} bot={e.bot_id}
            {e.registered_at ? ` @ ${e.registered_at}` : ''}
          </li>
        ))}
      </ul>

      <h3 className="mt-8 text-sm font-medium text-slate-600">积分榜</h3>
      <table className="mt-2 min-w-full text-left text-sm">
        <thead className="text-slate-500">
          <tr>
            <th className="py-1 pr-4">Bot</th>
            <th className="py-1 pr-4">积分</th>
            <th className="py-1 pr-4">W/D/L</th>
            <th className="py-1">净筹码</th>
          </tr>
        </thead>
        <tbody>
          {standings.map((s) => (
            <tr key={s.bot_id} className="border-t border-slate-200">
              <td className="py-1 pr-4">#{s.bot_id}</td>
              <td className="py-1 pr-4 text-brand-700">{s.points}</td>
              <td className="py-1 pr-4">
                {s.wins}/{s.draws}/{s.losses}
              </td>
              <td className="py-1">{s.net_chips}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <h3 className="mt-8 text-sm font-medium text-slate-600">对阵</h3>
      <ul className="mt-2 space-y-1 text-sm text-slate-400">
        {pairings.length === 0 && <li>暂无对阵</li>}
        {pairings.map((p) => (
          <li key={p.id} className="flex flex-wrap items-center gap-2">
            <span>R{p.round_num ?? 1}</span>
            <span>
              #{p.bot_a_id} vs #{p.bot_b_id}
            </span>
            <span>{p.status}</span>
            {p.match_id && (
              <Link to={`/watch/${p.match_id}`} className="text-brand-600 hover:text-brand-700">
                观战
              </Link>
            )}
          </li>
        ))}
      </ul>
    </PageStub>
  )
}
