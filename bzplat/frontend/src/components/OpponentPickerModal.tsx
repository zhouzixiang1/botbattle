import { useEffect, useState } from 'react'
import { apiGet, errMsg } from '../api'
import { gameLabel, type GameId } from '../lib/games'

export interface PickBot {
  id: number
  name: string
  display_name?: string
  owner_id?: number
  owner_name?: string
  owner_display?: string
  game_id?: string
  format?: string
  os?: string
  arch?: string
  is_active?: number
}

interface User {
  id: number
  username: string
  display_name?: string
}

type Tab = 'all' | 'mine' | 'users'

/**
 * 对手选择大弹窗（参考 botzone）：搜索框 + 列表合一。
 * - "全部/我的"：按 bot 搜索（名称），点选即定为对手。
 * - "按用户"：先搜用户，再展示该用户该游戏的 bot。
 */
export default function OpponentPickerModal({
  gameId,
  myUserId,
  onClose,
  onPick,
}: {
  gameId: GameId
  myUserId?: number
  onClose: () => void
  onPick: (bot: PickBot) => void
}) {
  const [tab, setTab] = useState<Tab>('all')
  const [q, setQ] = useState('')
  const [bots, setBots] = useState<PickBot[]>([])
  const [users, setUsers] = useState<User[]>([])
  const [selUser, setSelUser] = useState<User | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  // bot 列表（全部 / 我的 / 选定用户的）
  useEffect(() => {
    if (tab === 'users' && !selUser) return
    setLoading(true)
    const params = new URLSearchParams({ game_id: gameId })
    if (tab === 'mine') params.set('owner_id', String(myUserId ?? ''))
    if (tab === 'users' && selUser) params.set('owner_id', String(selUser.id))
    apiGet<{ bots: PickBot[] }>(`/api/bots/public?${params.toString()}`)
      .then((d) => {
        let rows = (d.bots || []).filter((b) => b.is_active !== 0)
        if (tab === 'mine') rows = rows.filter((b) => b.owner_id !== myUserId ? false : true)
        // 客户端按 q 过滤名称
        if (q.trim()) rows = rows.filter((b) => (b.name + (b.display_name || '')).toLowerCase().includes(q.toLowerCase()))
        setBots(rows)
      })
      .catch((e) => setError(errMsg(e, '加载失败')))
      .finally(() => setLoading(false))
  }, [gameId, tab, selUser, q, myUserId])

  // 用户搜索
  useEffect(() => {
    if (tab !== 'users') return
    const t = setTimeout(() => {
      if (!q.trim()) {
        setUsers([])
        return
      }
      apiGet<{ users: User[] }>(`/api/users?q=${encodeURIComponent(q.trim())}`)
        .then((d) => setUsers(d.users || []))
        .catch(() => setUsers([]))
    }, 250)
    return () => clearTimeout(t)
  }, [q, tab])

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4" onClick={onClose}>
      <div
        className="flex max-h-[85vh] w-full max-w-2xl flex-col overflow-hidden rounded-2xl bg-white shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        {/* 头部 */}
        <div className="flex items-center justify-between border-b border-slate-200 px-5 py-3">
          <h3 className="text-base font-semibold text-slate-800">
            选择对手 <span className="text-sm font-normal text-slate-400">({gameLabel(gameId)})</span>
          </h3>
          <button type="button" onClick={onClose} className="text-slate-400 hover:text-slate-600">✕</button>
        </div>

        {/* 搜索框 */}
        <div className="border-b border-slate-100 px-5 py-3">
          <input
            autoFocus
            value={q}
            onChange={(e) => {
              setQ(e.target.value)
              if (tab === 'users') setSelUser(null)
            }}
            placeholder={tab === 'users' ? '搜索用户名…' : '搜索 Bot 名称…'}
            className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm focus:border-brand-400 focus:outline-none"
          />
          <div className="mt-2 flex gap-2 text-xs">
            {([['all', '全部 Bot'], ['mine', '我的 Bot（自博弈）'], ['users', '按用户搜索']] as [Tab, string][]).map(
              ([t, label]) => (
                <button
                  key={t}
                  type="button"
                  onClick={() => {
                    setTab(t)
                    setSelUser(null)
                  }}
                  className={`rounded-full px-3 py-1 ${tab === t ? 'bg-brand-600 text-white' : 'bg-slate-100 text-slate-600 hover:bg-slate-200'}`}
                >
                  {label}
                </button>
              ),
            )}
          </div>
        </div>

        {/* 内容区 */}
        <div className="flex-1 overflow-auto px-5 py-3">
          {error && <p className="py-4 text-center text-sm text-error-500">{error}</p>}

          {/* 按用户：先显示用户列表，选定后显示其 bot */}
          {tab === 'users' && !selUser && (
            <div>
              {users.length === 0 ? (
                <p className="py-8 text-center text-sm text-slate-400">
                  {q.trim() ? '无匹配用户' : '输入用户名前缀搜索…'}
                </p>
              ) : (
                <ul className="divide-y divide-slate-100">
                  {users.map((u) => (
                    <li key={u.id}>
                      <button
                        type="button"
                        onClick={() => setSelUser(u)}
                        className="flex w-full items-center justify-between px-2 py-2.5 text-left hover:bg-slate-50"
                      >
                        <span className="text-sm text-slate-700">{u.display_name || u.username}</span>
                        <span className="text-xs text-slate-400">@{u.username}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}

          {tab === 'users' && selUser && (
            <div className="mb-2 flex items-center gap-2 text-sm text-slate-600">
              <button type="button" onClick={() => setSelUser(null)} className="text-brand-600 hover:underline">
                ← 返回用户搜索
              </button>
              <span>已选：{selUser.display_name || selUser.username}</span>
            </div>
          )}

          {/* bot 列表（all / mine / 选定用户） */}
          {!(tab === 'users' && !selUser) && (
            <>
              {loading ? (
                <p className="py-8 text-center text-sm text-slate-400">加载中…</p>
              ) : bots.length === 0 ? (
                <p className="py-8 text-center text-sm text-slate-400">无可选 Bot</p>
              ) : (
                <ul className="divide-y divide-slate-100">
                  {bots.map((b) => (
                    <li key={b.id}>
                      <button
                        type="button"
                        onClick={() => onPick(b)}
                        className="flex w-full items-center justify-between px-2 py-2.5 text-left hover:bg-brand-50"
                      >
                        <span className="text-sm font-medium text-slate-800">
                          {b.display_name || b.name}
                          {tab === 'mine' && <span className="ml-2 text-xs text-slate-400">自博弈</span>}
                        </span>
                        <span className="text-xs text-slate-400">
                          {b.owner_display || b.owner_name || `#${b.owner_id}`} · {b.format}/{b.os}-{b.arch}
                        </span>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  )
}
