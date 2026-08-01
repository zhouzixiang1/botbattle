import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import PageStub from '../components/PageStub'
import { apiGet, apiPost, errMsg } from '../api'

interface Notification {
  id: number
  type: string
  title: string
  body: string
  link: string
  is_read: number
  created_at: string
}

export default function Notifications() {
  const [items, setItems] = useState<Notification[]>([])
  const [unread, setUnread] = useState(0)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [filter, setFilter] = useState<'all' | 'unread'>('all')

  function load() {
    setLoading(true)
    apiGet<{ notifications: Notification[]; unread_count: number }>(
      `/api/notifications?unread_only=${filter === 'unread'}&limit=100`,
    )
      .then((d) => {
        setItems(d.notifications || [])
        setUnread(d.unread_count || 0)
      })
      .catch((e) => setError(errMsg(e)))
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    load()
  }, [filter])

  function markRead(id: number) {
    apiPost('/api/notifications/read', 'POST', { id }).then(() => {
      setItems((rs) => rs.map((n) => (n.id === id ? { ...n, is_read: 1 } : n)))
      setUnread((u) => Math.max(0, u - 1))
    })
  }

  function readAll() {
    apiPost('/api/notifications/read-all', 'POST', {}).then(() => {
      setItems((rs) => rs.map((n) => ({ ...n, is_read: 1 })))
      setUnread(0)
    })
  }

  return (
    <PageStub title="通知">
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <div className="flex gap-1">
          {(['all', 'unread'] as const).map((f) => (
            <button
              key={f}
              type="button"
              onClick={() => setFilter(f)}
              className={`rounded-lg px-3 py-1.5 text-sm ${
                filter === f
                  ? 'bg-brand-600 text-white'
                  : 'border border-slate-300 bg-white text-slate-600 hover:bg-slate-50'
              }`}
            >
              {f === 'all' ? '全部' : `未读${unread > 0 ? ` (${unread})` : ''}`}
            </button>
          ))}
        </div>
        {unread > 0 && (
          <button
            type="button"
            onClick={readAll}
            className="ml-auto rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-xs text-slate-600 hover:bg-slate-50"
          >
            全部标记已读
          </button>
        )}
      </div>
      {error && <p className="mb-3 text-sm text-error-500">{error}</p>}
      {loading ? (
        <p className="py-8 text-center text-sm text-slate-400">加载中…</p>
      ) : items.length === 0 ? (
        <p className="py-8 text-center text-sm text-slate-400">暂无通知</p>
      ) : (
        <div className="space-y-2">
          {items.map((n) => {
            const inner = (
              <div
                className={`card flex items-start gap-3 p-3 ${
                  !n.is_read ? 'border-brand-200 bg-brand-50/30' : ''
                }`}
              >
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    {!n.is_read && (
                      <span className="h-2 w-2 shrink-0 rounded-full bg-brand-500" />
                    )}
                    <span className="font-medium text-slate-800">{n.title}</span>
                    {n.type && (
                      <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[10px] text-slate-500">
                        {n.type}
                      </span>
                    )}
                  </div>
                  {n.body && <p className="mt-1 text-sm text-slate-600">{n.body}</p>}
                  <p className="mt-1 text-xs text-slate-400">
                    {n.created_at?.replace('T', ' ').slice(0, 16)}
                  </p>
                </div>
                {!n.is_read && (
                  <button
                    type="button"
                    onClick={(e) => {
                      e.preventDefault()
                      markRead(n.id)
                    }}
                    className="shrink-0 rounded-lg border border-slate-300 bg-white px-2 py-1 text-xs text-slate-600 hover:bg-slate-50"
                  >
                    已读
                  </button>
                )}
              </div>
            )
            return n.link ? (
              <Link key={n.id} to={n.link}>
                {inner}
              </Link>
            ) : (
              <div key={n.id}>{inner}</div>
            )
          })}
        </div>
      )}
    </PageStub>
  )
}
