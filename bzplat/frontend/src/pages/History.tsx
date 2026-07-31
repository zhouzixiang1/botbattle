import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import PageStub from '../components/PageStub'
import { apiGet, errMsg } from '../api'
import { GAMES, gameLabel } from '../lib/games'

interface Match {
  id: string
  status: string
  bot_a_id: number
  bot_b_id: number
  bot_a_name?: string
  bot_b_name?: string
  bot_a_display?: string
  bot_b_display?: string
  earnings_a?: number
  earnings_b?: number
  created_at?: string
  hands_played?: number
  total_hands?: number
  match_type?: string
  game_id?: string
}

export default function History() {
  const [matches, setMatches] = useState<Match[]>([])
  const [status, setStatus] = useState('')
  const [gameId, setGameId] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    const params = new URLSearchParams({ limit: '100' })
    if (status) params.set('status', status)
    if (gameId) params.set('game_id', gameId)
    apiGet<{ matches: Match[] }>(`/api/matches?${params}`)
      .then((d) => setMatches(d.matches || []))
      .catch((e) => setError(errMsg(e)))
  }, [status, gameId])

  return (
    <PageStub title="对局历史">
      <div className="mb-4 flex flex-wrap gap-4">
        <label className="text-sm text-slate-400">
          状态
          <select
            value={status}
            onChange={(e) => setStatus(e.target.value)}
            className="ml-2 rounded-lg border border-slate-300 bg-white px-2 py-1.5 text-slate-700"
          >
            <option value="">全部</option>
            <option value="pending">pending</option>
            <option value="running">running</option>
            <option value="completed">completed</option>
            <option value="aborted">aborted</option>
          </select>
        </label>
        <label className="text-sm text-slate-400">
          游戏
          <select
            value={gameId}
            onChange={(e) => setGameId(e.target.value)}
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
      <ul className="divide-y divide-slate-700/80 overflow-hidden rounded-xl border border-slate-200 bg-white">
        {matches.length === 0 ? (
          <li className="px-4 py-8 text-center text-slate-500">暂无对局</li>
        ) : (
          matches.map((m) => (
            <li key={m.id} className="flex flex-wrap items-center gap-3 px-4 py-3">
              <div className="min-w-0 flex-1">
                <div className="font-medium text-slate-800">
                  {m.bot_a_display || m.bot_a_name} vs {m.bot_b_display || m.bot_b_name}
                </div>
                <div className="mt-1 text-xs text-slate-400">
                  {gameLabel(m.game_id)} · {m.status} · {m.created_at || ''}
                </div>
              </div>
              <Link className="text-sm text-brand-600 hover:underline" to={`/match/${m.id}`}>
                详情
              </Link>
              {(m.status === 'running' || m.status === 'pending') && (
                <Link className="text-sm text-brand-600 hover:underline" to={`/watch/${m.id}`}>
                  观赛
                </Link>
              )}
            </li>
          ))
        )}
      </ul>
    </PageStub>
  )
}
