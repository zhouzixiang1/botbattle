import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { CheckCheck, Bell } from 'lucide-react'
import PageStub from '@/components/PageStub'
import { Card } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { EmptyState, ErrorMsg, Loading } from '@/components/ui/status'
import { cn } from '@/lib/utils'
import { apiGet, apiPost, errMsg } from '@/api'
import { fmtTime } from '@/lib/format'

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
    apiPost('/api/notifications/read', 'POST', { id })
      .then(() => {
        setItems((rs) => rs.map((n) => (n.id === id ? { ...n, is_read: 1 } : n)))
        setUnread((u) => Math.max(0, u - 1))
      })
      .catch((e) => setError(errMsg(e)))
  }

  function readAll() {
    apiPost('/api/notifications/read-all', 'POST', {})
      .then(() => {
        setItems((rs) => rs.map((n) => ({ ...n, is_read: 1 })))
        setUnread(0)
      })
      .catch((e) => setError(errMsg(e)))
  }

  return (
    <PageStub title="通知" subtitle="你的对局结果、赛事与系统消息">
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <div className="inline-flex rounded-lg border border-border p-1">
          {(['all', 'unread'] as const).map((f) => (
            <button
              key={f}
              type="button"
              onClick={() => setFilter(f)}
              className={cn(
                'rounded-md px-3 py-1 text-sm transition-colors',
                filter === f
                  ? 'bg-primary text-primary-foreground'
                  : 'text-muted-foreground hover:text-foreground',
              )}
            >
              {f === 'all' ? '全部' : `未读${unread > 0 ? ` (${unread})` : ''}`}
            </button>
          ))}
        </div>
        {unread > 0 && (
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={readAll}
            className="ml-auto gap-1.5"
          >
            <CheckCheck className="size-3.5" />
            全部标记已读
          </Button>
        )}
      </div>
      {error && <ErrorMsg msg={error} className="mb-3" />}
      {loading ? (
        <Loading />
      ) : items.length === 0 ? (
        <EmptyState text="暂无通知" icon={<Bell className="size-7 opacity-40" />} />
      ) : (
        <div className="space-y-2">
          {items.map((n) => {
            const inner = (
              <Card
                className={cn(
                  'gap-0 flex-row items-start p-3',
                  !n.is_read && 'border-primary/40 bg-primary/5',
                )}
              >
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    {!n.is_read && (
                      <span className="size-2 shrink-0 rounded-full bg-primary" />
                    )}
                    <span className="font-medium text-foreground">{n.title}</span>
                    {n.type && <Badge variant="secondary">{n.type}</Badge>}
                  </div>
                  {n.body && <p className="mt-1 text-sm text-muted-foreground">{n.body}</p>}
                  <p className="mt-1 text-xs text-muted-foreground">
                    {fmtTime(n.created_at)}
                  </p>
                </div>
                {!n.is_read && (
                  <Button
                    type="button"
                    variant="outline"
                    size="xs"
                    onClick={(e) => {
                      e.preventDefault()
                      markRead(n.id)
                    }}
                    className="shrink-0"
                  >
                    已读
                  </Button>
                )}
              </Card>
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
