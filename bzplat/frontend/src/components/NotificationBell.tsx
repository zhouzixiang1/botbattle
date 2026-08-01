import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Bell, Check } from 'lucide-react'
import { apiGet, apiPost } from '@/api'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'

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

  function readAll() {
    apiPost('/api/notifications/read-all', 'POST', {}).then(() => {
      setUnread(0)
      setRecent((rs) => rs.map((n) => ({ ...n, is_read: 1 })))
    })
  }

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          className="relative inline-flex size-9 items-center justify-center rounded-lg text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground"
          aria-label="通知"
        >
          <Bell className="size-[1.15rem]" />
          {unread > 0 && (
            <span className="absolute -right-0.5 -top-0.5 flex h-4 min-w-4 items-center justify-center rounded-full bg-destructive px-1 text-[10px] font-bold text-destructive-foreground">
              {unread > 99 ? '99+' : unread}
            </span>
          )}
        </button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-80 p-0">
        <div className="flex items-center justify-between border-b border-border px-3 py-2">
          <span className="text-sm font-semibold text-foreground">通知</span>
          {unread > 0 && (
            <Button type="button" variant="ghost" size="sm" onClick={readAll} className="h-7 gap-1 text-xs text-primary">
              <Check className="size-3" />全部已读
            </Button>
          )}
        </div>
        <div className="max-h-80 overflow-y-auto">
          {recent.length === 0 ? (
            <p className="py-6 text-center text-sm text-muted-foreground">暂无通知</p>
          ) : (
            recent.map((n) => {
              const inner = (
                <div
                  className={cn(
                    'border-b border-border/50 px-3 py-2 text-sm transition-colors hover:bg-accent',
                    !n.is_read && 'bg-primary/5'
                  )}
                >
                  <div className="flex items-center gap-1.5">
                    {!n.is_read && <span className="size-1.5 shrink-0 rounded-full bg-primary" />}
                    <span className="font-medium text-foreground">{n.title}</span>
                  </div>
                  {n.body && <p className="mt-0.5 line-clamp-2 text-xs text-muted-foreground">{n.body}</p>}
                  <p className="mt-0.5 text-[10px] text-muted-foreground">
                    {n.created_at?.slice(5, 16).replace('T', ' ')}
                  </p>
                </div>
              )
              return n.link ? (
                <Link key={n.id} to={n.link} onClick={() => setOpen(false)}>{inner}</Link>
              ) : (
                <div key={n.id}>{inner}</div>
              )
            })
          )}
        </div>
        <Link
          to="/notifications"
          onClick={() => setOpen(false)}
          className="block border-t border-border px-3 py-2 text-center text-xs font-medium text-primary transition-colors hover:bg-accent"
        >
          查看全部通知
        </Link>
      </PopoverContent>
    </Popover>
  )
}
