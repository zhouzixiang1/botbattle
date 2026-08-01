import { useEffect, useState, useRef } from 'react'
import { Link } from 'react-router-dom'
import { apiGet, apiPost } from '../api'

interface Notification {
  id: number
  type: string
  title: string
  body: string
  link: string
  is_read: number
  created_at: string
}

export default function NotificationBell() {
  const [unread, setUnread] = useState(0)
  const [recent, setRecent] = useState<Notification[]>([])
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  // 轮询未读数（30s）
  useEffect(() => {
    const fetchUnread = () => {
      apiGet<{ count: number }>('/api/notifications/unread-count')
        .then((d) => setUnread(d.count || 0))
        .catch(() => {})
    }
    fetchUnread()
    const t = setInterval(fetchUnread, 30000)
    return () => clearInterval(t)
  }, [])

  // 打开时拉取最近通知
  useEffect(() => {
    if (!open) return
    apiGet<{ notifications: Notification[]; unread_count: number }>(
      '/api/notifications?limit=10',
    )
      .then((d) => {
        setRecent(d.notifications || [])
        setUnread(d.unread_count || 0)
      })
      .catch(() => {})
  }, [open])

  // 点击外部关闭
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  function readAll() {
    apiPost('/api/notifications/read-all', 'POST', {}).then(() => {
      setUnread(0)
      setRecent((rs) => rs.map((n) => ({ ...n, is_read: 1 })))
    })
  }

  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="relative rounded-lg px-2 py-1.5 text-slate-500 hover:bg-slate-100"
        aria-label="通知"
      >
        <span className="text-lg">🔔</span>
        {unread > 0 && (
          <span className="absolute -right-0.5 -top-0.5 flex h-4 min-w-4 items-center justify-center rounded-full bg-error-500 px-1 text-[10px] font-bold text-white">
            {unread > 99 ? '99+' : unread}
          </span>
        )}
      </button>
      {open && (
        <div className="absolute right-0 z-50 mt-2 w-80 overflow-hidden rounded-xl border border-slate-200 bg-white shadow-lg">
          <div className="flex items-center justify-between border-b border-slate-100 px-3 py-2">
            <span className="text-sm font-semibold text-slate-700">通知</span>
            {unread > 0 && (
              <button
                type="button"
                onClick={readAll}
                className="text-xs text-brand-600 hover:text-brand-700"
              >
                全部已读
              </button>
            )}
          </div>
          <div className="max-h-80 overflow-y-auto">
            {recent.length === 0 ? (
              <p className="py-6 text-center text-sm text-slate-400">暂无通知</p>
            ) : (
              recent.map((n) => {
                const inner = (
                  <div
                    className={`border-b border-slate-50 px-3 py-2 text-sm hover:bg-slate-50 ${
                      !n.is_read ? 'bg-brand-50/40' : ''
                    }`}
                  >
                    <div className="flex items-center gap-1.5">
                      {!n.is_read && (
                        <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-brand-500" />
                      )}
                      <span className="font-medium text-slate-700">{n.title}</span>
                    </div>
                    {n.body && (
                      <p className="mt-0.5 line-clamp-2 text-xs text-slate-500">{n.body}</p>
                    )}
                    <p className="mt-0.5 text-[10px] text-slate-400">
                      {n.created_at?.slice(5, 16).replace('T', ' ')}
                    </p>
                  </div>
                )
                return n.link ? (
                  <Link key={n.id} to={n.link} onClick={() => setOpen(false)}>
                    {inner}
                  </Link>
                ) : (
                  <div key={n.id}>{inner}</div>
                )
              })
            )}
          </div>
          <Link
            to="/notifications"
            onClick={() => setOpen(false)}
            className="block border-t border-slate-100 px-3 py-2 text-center text-xs text-brand-600 hover:bg-slate-50"
          >
            查看全部通知 →
          </Link>
        </div>
      )}
    </div>
  )
}
