import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import PageStub from '../components/PageStub'
import { apiGet, errMsg } from '../api'

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
}

export default function History() {
  const [matches, setMatches] = useState<Match[]>([])
  const [status, setStatus] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    const q = status
      ? `?status=${encodeURIComponent(status)}&limit=100`
      : '?limit=100'
    apiGet<{ matches: Match[] }>(`/api/matches${q}`)
      .then((d) => setMatches(d.matches || []))
      .catch((e) => setError(errMsg(e)))
  }, [status])

  return (
    <PageStub title="对局历史">
      <div className="mb-4">
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
      </div>
      {error && <p className="mb-3 text-sm text-error-500">{error}</p>}
      <ul className="divide-y divide-slate-700/80 overflow-hidden rounded-xl border border-slate-200 bg-white">
        {matches.length === 0 ? (
          <li className="px-4 py-8 text-center text-slate-500">暂无对局</li>
        ) : (
          matches.map((m) => (
            <li
              key={m.id}
              className="flex flex-wrap items-center justify-between gap-2 px-4 py-3 text-sm"
            >
              <div>
                <Link
                  to={`/match/${m.id}`}
                  className="font-mono text-brand-700 hover:underline"
                >
                  {m.id}
                </Link>
                <span className="ml-3 text-slate-400">
                  {m.bot_a_display || m.bot_a_name || m.bot_a_id} vs{' '}
                  {m.bot_b_display || m.bot_b_name || m.bot_b_id}
                </span>
                <span className="ml-3 text-slate-500">
                  {m.match_type} · {m.status} · {m.hands_played}/{m.total_hands}
                </span>
              </div>
              <div className="flex items-center gap-3">
                <span className="text-slate-500">{m.created_at}</span>
                {(m.status === 'pending' || m.status === 'running') && (
                  <Link to={`/watch/${m.id}`} className="text-brand-600 hover:text-brand-700">
                    观战
                  </Link>
                )}
              </div>
            </li>
          ))
        )}
      </ul>
    </PageStub>
  )
}
