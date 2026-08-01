import { useEffect, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import PageStub from '../components/PageStub'
import { apiGet, errMsg } from '../api'
import { gameLabel } from '../lib/games'

type SearchType = 'users' | 'bots' | 'matches'

interface UserRow {
  id: number
  username: string
  display_name: string
}
interface BotRow {
  id: number
  name: string
  display_name: string
  game_id: string
  owner_name?: string
  owner_display?: string
  rating?: number
}
interface MatchRow {
  id: string
  game_id: string
  winner: number | null
  bot_a_id: number
  bot_b_id: number
  bot_a_name: string
  bot_b_name: string
  bot_a_display?: string
  bot_b_display?: string
  created_at?: string
}

export default function Search() {
  const [params, setParams] = useSearchParams()
  const navigate = useNavigate()
  const q = params.get('q') || ''
  const type = (params.get('type') as SearchType) || 'users'
  const [input, setInput] = useState(q)
  const [users, setUsers] = useState<UserRow[]>([])
  const [bots, setBots] = useState<BotRow[]>([])
  const [matches, setMatches] = useState<MatchRow[]>([])
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    setInput(q)
  }, [q])

  useEffect(() => {
    if (!q.trim()) return
    setLoading(true)
    const t = type
    apiGet<Record<string, unknown[]>>(
      `/api/search?q=${encodeURIComponent(q)}&type=${t}&limit=30`,
    )
      .then((d) => {
        setUsers(t === 'users' ? (d.users as UserRow[]) : [])
        setBots(t === 'bots' ? (d.bots as BotRow[]) : [])
        setMatches(t === 'matches' ? (d.matches as MatchRow[]) : [])
      })
      .catch((e) => setError(errMsg(e)))
      .finally(() => setLoading(false))
  }, [q, type])

  function submit(e: React.FormEvent) {
    e.preventDefault()
    const next = new URLSearchParams(params)
    next.set('q', input.trim())
    next.set('type', type)
    setParams(next)
  }

  function switchType(t: SearchType) {
    const next = new URLSearchParams(params)
    next.set('type', t)
    if (q) next.set('q', q)
    navigate(`/search?${next.toString()}`)
  }

  return (
    <PageStub title="搜索">
      {/* 搜索框 */}
      <form onSubmit={submit} className="mb-4 flex gap-2">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="搜索用户 / Bot / 对局…"
          className="min-w-0 flex-1 rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-700 placeholder:text-slate-400"
          autoFocus
        />
        <button
          type="submit"
          className="rounded-lg bg-brand-600 px-4 py-2 text-sm font-medium text-white hover:bg-brand-500"
        >
          搜索
        </button>
      </form>

      {/* 类型 tab */}
      <div className="mb-4 flex gap-1 border-b border-slate-200">
        {(
          [
            ['users', '用户'],
            ['bots', 'Bot'],
            ['matches', '对局'],
          ] as const
        ).map(([k, label]) => (
          <button
            key={k}
            type="button"
            onClick={() => switchType(k)}
            className={`-mb-px border-b-2 px-4 py-2 text-sm font-medium ${
              type === k
                ? 'border-brand-500 text-brand-700'
                : 'border-transparent text-slate-500 hover:text-slate-700'
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {error && <p className="mb-3 text-sm text-error-500">{error}</p>}
      {!q.trim() && <p className="py-8 text-center text-sm text-slate-400">输入关键词开始搜索</p>}
      {loading && <p className="py-8 text-center text-sm text-slate-400">搜索中…</p>}

      {/* 用户结果 */}
      {type === 'users' && q.trim() && !loading && (
        <div className="space-y-2">
          {users.length === 0 ? (
            <p className="py-8 text-center text-sm text-slate-400">无匹配用户</p>
          ) : (
            users.map((u) => (
              <Link
                key={u.id}
                to={`/user/${encodeURIComponent(u.username)}`}
                className="card block p-3 hover:border-brand-300"
              >
                <span className="font-medium text-slate-800">
                  {u.display_name || u.username}
                </span>
                <span className="ml-2 text-xs text-slate-400">@{u.username}</span>
              </Link>
            ))
          )}
        </div>
      )}

      {/* Bot 结果 */}
      {type === 'bots' && q.trim() && !loading && (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {bots.length === 0 ? (
            <p className="col-span-full py-8 text-center text-sm text-slate-400">无匹配 Bot</p>
          ) : (
            bots.map((b) => (
              <Link key={b.id} to={`/bot/${b.id}`} className="card block p-4 hover:border-brand-300">
                <div className="flex items-center justify-between">
                  <span className="font-medium text-slate-800">
                    {b.display_name || b.name}
                  </span>
                  <span className="rounded-full bg-brand-50 px-2 py-0.5 text-[10px] font-medium text-brand-700">
                    {gameLabel(b.game_id)}
                  </span>
                </div>
                <p className="mt-1 text-xs text-slate-400">
                  @{b.name}
                  {b.owner_name ? ` · ${b.owner_display || b.owner_name}` : ''}
                  {b.rating != null && ` · ${Number(b.rating).toFixed(0)}`}
                </p>
              </Link>
            ))
          )}
        </div>
      )}

      {/* 对局结果 */}
      {type === 'matches' && q.trim() && !loading && (
        <div className="overflow-x-auto rounded-xl border border-slate-200">
          <table className="w-full min-w-[34rem] text-left text-sm">
            <thead className="bg-white text-xs uppercase tracking-wide text-slate-400">
              <tr>
                <th className="px-3 py-2.5">时间</th>
                <th className="px-3 py-2.5">对阵</th>
                <th className="px-3 py-2.5">游戏</th>
                <th className="px-3 py-2.5">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {matches.length === 0 ? (
                <tr>
                  <td colSpan={4} className="px-3 py-8 text-center text-slate-400">
                    无匹配对局
                  </td>
                </tr>
              ) : (
                matches.map((m) => (
                  <tr key={m.id} className="bg-white hover:bg-slate-100/60">
                    <td className="px-3 py-2.5 text-xs text-slate-400">
                      {m.created_at?.slice(0, 16).replace('T', ' ') || '—'}
                    </td>
                    <td className="px-3 py-2.5">
                      <Link to={`/bot/${m.bot_a_id}`} className="hover:text-brand-600">
                        {m.bot_a_display || m.bot_a_name}
                      </Link>{' '}
                      vs{' '}
                      <Link to={`/bot/${m.bot_b_id}`} className="hover:text-brand-600">
                        {m.bot_b_display || m.bot_b_name}
                      </Link>
                    </td>
                    <td className="px-3 py-2.5 text-slate-500">{gameLabel(m.game_id)}</td>
                    <td className="px-3 py-2.5">
                      <Link
                        to={`/match/${encodeURIComponent(m.id)}`}
                        className="text-brand-600 hover:text-brand-700"
                      >
                        回放 →
                      </Link>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      )}
    </PageStub>
  )
}
