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
}

export default function Home() {
  const [matches, setMatches] = useState<Match[]>([])
  const [error, setError] = useState('')

  useEffect(() => {
    apiGet<{ matches: Match[] }>('/api/matches?limit=30')
      .then((d) => setMatches(d.matches || []))
      .catch((e) => setError(errMsg(e)))
  }, [])

  return (
    <PageStub title="最新对局">
      <p className="mb-4 text-sm text-slate-400">
        发起挑战或参加比赛后，可在此查看进行中与已完成的对局。
      </p>
      {error && <p className="mb-3 text-sm text-error-500">{error}</p>}
      <div className="overflow-x-auto rounded-xl border border-slate-200 bg-white">
        <table className="min-w-full text-left text-sm">
          <thead className="border-b border-slate-200 text-slate-400">
            <tr>
              <th className="px-4 py-3">时间</th>
              <th className="px-4 py-3">对阵</th>
              <th className="px-4 py-3">状态</th>
              <th className="px-4 py-3">手数</th>
              <th className="px-4 py-3">操作</th>
            </tr>
          </thead>
          <tbody>
            {matches.map((m) => (
              <tr key={m.id} className="border-b border-slate-200">
                <td className="px-4 py-3 text-slate-400">{m.created_at || '—'}</td>
                <td className="px-4 py-3 text-slate-700">
                  {m.bot_a_display || m.bot_a_name || `#${m.bot_a_id}`} vs{' '}
                  {m.bot_b_display || m.bot_b_name || `#${m.bot_b_id}`}
                </td>
                <td className="px-4 py-3">{m.status}</td>
                <td className="px-4 py-3">
                  {m.hands_played ?? 0}/{m.total_hands ?? 70}
                </td>
                <td className="space-x-2 px-4 py-3">
                  {(m.status === 'pending' || m.status === 'running') && (
                    <Link className="text-brand-700 hover:underline" to={`/watch/${m.id}`}>
                      观赛
                    </Link>
                  )}
                  <Link className="text-slate-600 hover:underline" to={`/match/${m.id}`}>
                    详情
                  </Link>
                </td>
              </tr>
            ))}
            {!matches.length && !error && (
              <tr>
                <td colSpan={5} className="px-4 py-8 text-center text-slate-500">
                  暂无对局。去{' '}
                  <Link to="/challenge" className="text-brand-700">
                    发起挑战
                  </Link>
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </PageStub>
  )
}
