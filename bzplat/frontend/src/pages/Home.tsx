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
  game_id?: string
  match_type?: string
  owner_id?: number | null
}

// 对局类型徽章（对标 botzone：区分用户挑战/后台/人类/赛事）
function matchTypeBadge(m: Match): { label: string; cls: string } | null {
  const t = m.match_type
  switch (t) {
    case 'ladder':
      return { label: '后台', cls: 'bg-sky-50 text-sky-700' }
    case 'human':
      return { label: '人类', cls: 'bg-violet-50 text-violet-700' }
    case 'contest':
      return { label: '赛事', cls: 'bg-amber-50 text-amber-700' }
    case 'table':
      return { label: '桌台', cls: 'bg-slate-100 text-slate-600' }
    case 'challenge':
      return { label: '挑战', cls: 'bg-emerald-50 text-emerald-700' }
    default:
      return null
  }
}

export default function Home() {
  const [matches, setMatches] = useState<Match[]>([])
  const [error, setError] = useState('')
  const [gameId, setGameId] = useState('')

  useEffect(() => {
    const q = gameId
      ? `?limit=30&game_id=${encodeURIComponent(gameId)}`
      : '?limit=30'
    apiGet<{ matches: Match[] }>(`/api/matches${q}`)
      .then((d) => setMatches(d.matches || []))
      .catch((e) => setError(errMsg(e)))
  }, [gameId])

  return (
    <PageStub title="最新对局">
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <p className="text-sm text-slate-400">
          发起挑战或参加比赛后，可在此查看进行中与已完成的对局。
        </p>
        <label className="ml-auto text-sm text-slate-500">
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
      <div className="overflow-x-auto rounded-xl border border-slate-200 bg-white">
        <table className="min-w-full text-left text-sm">
          <thead className="border-b border-slate-200 text-slate-400">
            <tr>
              <th className="px-4 py-3">时间</th>
              <th className="px-4 py-3">游戏</th>
              <th className="px-4 py-3">对阵</th>
              <th className="px-4 py-3">状态</th>
              <th className="px-4 py-3">进度</th>
              <th className="px-4 py-3">操作</th>
            </tr>
          </thead>
          <tbody>
            {matches.map((m) => (
              <tr key={m.id} className="border-b border-slate-200">
                <td className="px-4 py-3 text-slate-400">{m.created_at || '—'}</td>
                <td className="px-4 py-3">
                  {gameLabel(m.game_id)}
                  {(() => {
                    const b = matchTypeBadge(m)
                    return b ? (
                      <span className={`ml-1 inline-block rounded-full px-1.5 py-0.5 text-[10px] font-medium ${b.cls}`}>
                        {b.label}
                      </span>
                    ) : null
                  })()}
                </td>
                <td className="px-4 py-3 text-slate-700">
                  <Link to={`/bot/${m.bot_a_id}`} className="hover:text-brand-600">
                    {m.bot_a_display || m.bot_a_name || `#${m.bot_a_id}`}
                  </Link>{' '}
                  vs{' '}
                  <Link to={`/bot/${m.bot_b_id}`} className="hover:text-brand-600">
                    {m.bot_b_display || m.bot_b_name || `#${m.bot_b_id}`}
                  </Link>
                </td>
                <td className="px-4 py-3">{m.status}</td>
                <td className="px-4 py-3">
                  {m.game_id === 'holdem' || !m.game_id
                    ? `${m.hands_played ?? 0}/${m.total_hands ?? 70}`
                    : `${m.hands_played ?? 0} 步`}
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
                <td colSpan={6} className="px-4 py-8 text-center text-slate-500">
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
