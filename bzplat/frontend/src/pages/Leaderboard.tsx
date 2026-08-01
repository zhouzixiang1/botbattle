import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import PageStub from '../components/PageStub'
import { apiGet, errMsg } from '../api'
import { GAMES, gameLabel, type GameId } from '../lib/games'

interface Row {
  bot_id: number
  bot_name?: string
  bot_display?: string
  owner_name?: string
  rating: number
  rd?: number
  wins?: number
  losses?: number
  draws?: number
  matches_played?: number
  net_chips?: number
  format?: string
  os?: string
  arch?: string
  game_id?: string
}

export default function Leaderboard() {
  const [rows, setRows] = useState<Row[]>([])
  const [error, setError] = useState('')
  const [gameId, setGameId] = useState<GameId | ''>('holdem')

  useEffect(() => {
    const q = gameId ? `?game_id=${encodeURIComponent(gameId)}` : ''
    apiGet<{ leaderboard: Row[] }>(`/api/leaderboard${q}`)
      .then((d) => setRows(d.leaderboard || []))
      .catch((e) => setError(errMsg(e)))
  }, [gameId])

  return (
    <PageStub title="排行榜">
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <p className="text-sm text-slate-400">Glicko-2 评分（按游戏过滤）。</p>
        <label className="ml-auto text-sm text-slate-500">
          游戏
          <select
            value={gameId}
            onChange={(e) => setGameId(e.target.value as GameId | '')}
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
      <div className="overflow-x-auto rounded-xl border border-slate-200">
        <table className="w-full min-w-[40rem] text-left text-sm">
          <thead className="bg-white text-xs uppercase tracking-wide text-slate-400">
            <tr>
              <th className="px-3 py-2.5">#</th>
              <th className="px-3 py-2.5">Bot</th>
              <th className="px-3 py-2.5">游戏</th>
              <th className="px-3 py-2.5">所有者</th>
              <th className="px-3 py-2.5">Rating</th>
              <th className="px-3 py-2.5">战绩</th>
              <th className="px-3 py-2.5">平台</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-700/60">
            {rows.length === 0 ? (
              <tr>
                <td colSpan={7} className="px-3 py-8 text-center text-slate-500">
                  暂无数据
                </td>
              </tr>
            ) : (
              rows.map((r, i) => (
                <tr key={r.bot_id} className="bg-white hover:bg-slate-100/60">
                  <td className="px-3 py-2.5 text-slate-500">{i + 1}</td>
                  <td className="px-3 py-2.5 font-medium text-slate-800">
                    <Link to={`/bot/${r.bot_id}`} className="hover:text-brand-600">
                      {r.bot_display || r.bot_name || `#${r.bot_id}`}
                    </Link>
                  </td>
                  <td className="px-3 py-2.5 text-slate-500">{gameLabel(r.game_id)}</td>
                  <td className="px-3 py-2.5">
                    {r.owner_name ? (
                      <Link
                        to={`/user/${encodeURIComponent(r.owner_name)}`}
                        className="text-brand-600 hover:text-brand-700"
                      >
                        {r.owner_name}
                      </Link>
                    ) : (
                      '—'
                    )}
                  </td>
                  <td className="px-3 py-2.5 font-mono text-brand-700">
                    {Number(r.rating).toFixed(1)}
                  </td>
                  <td className="px-3 py-2.5 text-slate-400">
                    {r.wins ?? 0}W / {r.losses ?? 0}L / {r.draws ?? 0}D
                  </td>
                  <td className="px-3 py-2.5 text-xs text-slate-400">
                    {r.format}/{r.os}-{r.arch}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </PageStub>
  )
}
