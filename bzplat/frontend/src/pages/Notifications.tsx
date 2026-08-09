import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { CheckCheck, Bell } from 'lucide-react'
import PageStub from '@/components/PageStub'
import { Card } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { EmptyState, ErrorMsg, Loading } from '@/components/ui/status'
import Pagination from '@/components/Pagination'
import { useAuth } from '@/components/useAuth'
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

const NOTIFICATION_LABELS: Record<string, string> = {
  match_done: '对局',
  followed: '关注',
  contest: '赛事',
  comment: '评论',
  system: '系统',
}

export default function Notifications() {
  const { user } = useAuth()
  const [items, setItems] = useState<Notification[]>([])
  const [unread, setUnread] = useState(0)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [filter, setFilter] = useState<'all' | 'unread'>('all')
  // 分页
  const [page, setPage] = useState(1)
  const [total, setTotal] = useState(0)
  const perPage = 20

  const load = useCallback(() => {
    if (!user) {
      setLoading(false)
      return
    }
    setLoading(true)
    apiGet<{ notifications: Notification[]; unread_count: number; total?: number }>(
      `/api/notifications?unread_only=${filter === 'unread'}&page=${page}&per_page=${perPage}`,
    )
      .then((d) => {
        setItems(d.notifications || [])
        setUnread(d.unread_count || 0)
        if (d.total !== undefined) setTotal(d.total)
      })
      .catch((e) => setError(errMsg(e)))
      .finally(() => setLoading(false))
  }, [filter, page, user])

  useEffect(() => {
    load()
  }, [load])

  if (!user) {
    return (
      <PageStub title="通知">
        <EmptyState text="请先登录" />
      </PageStub>
    )
  }

  // 切换筛选 → 回到第 1 页
  const onFilterChange = (f: 'all' | 'unread') => {
    setFilter(f)
    setPage(1)
  }

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
              onClick={() => onFilterChange(f)}
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
                    {n.type && <Badge variant="secondary">{NOTIFICATION_LABELS[n.type] || '系统'}</Badge>}
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
      <Pagination page={page} perPage={perPage} total={total} onPageChange={setPage} />
    </PageStub>
  )
}
