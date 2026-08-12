import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Bell, Bug, Check, CheckCheck, Mail, MailOpen } from 'lucide-react'

import { apiGet, apiPost, errMsg } from '@/api'
import { DataRegion, PageFrame, PageHeader, StickyToolbar, SummaryStrip } from '@/components/layout'
import Pagination from '@/components/Pagination'
import { useAuth } from '@/components/useAuth'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { EntityName, OverflowText } from '@/components/ui/overflow-text'
import { EmptyState, ErrorMsg, Loading } from '@/components/ui/status'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { fmtTime } from '@/lib/format'
import { SummaryMetric } from '@/pages/public-page-ui'

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

const PER_PAGE = 20

export default function Notifications() {
  const { user } = useAuth()
  const [items, setItems] = useState<Notification[]>([])
  const [unread, setUnread] = useState(0)
  const [loadError, setLoadError] = useState('')
  const [actionError, setActionError] = useState('')
  const [loading, setLoading] = useState(true)
  const [filter, setFilter] = useState<'all' | 'unread'>('all')
  const [page, setPage] = useState(1)
  const [total, setTotal] = useState(0)
  const [markingAll, setMarkingAll] = useState(false)
  const [markingId, setMarkingId] = useState<number | null>(null)

  const load = useCallback(() => {
    if (!user) {
      setLoading(false)
      return
    }
    setLoading(true)
    setLoadError('')
    apiGet<{ notifications: Notification[]; unread_count: number; total?: number }>(
      `/api/notifications?unread_only=${filter === 'unread'}&page=${page}&per_page=${PER_PAGE}`,
    )
      .then((data) => {
        setItems(data.notifications || [])
        setUnread(data.unread_count || 0)
        setTotal(data.total ?? 0)
      })
      .catch((cause) => setLoadError(errMsg(cause)))
      .finally(() => setLoading(false))
  }, [filter, page, user])

  useEffect(() => {
    load()
  }, [load])

  const changeFilter = (value: 'all' | 'unread') => {
    setFilter(value)
    setPage(1)
  }

  const markRead = (id: number) => {
    setMarkingId(id)
    apiPost('/api/notifications/read', 'POST', { id })
      .then(() => {
        setActionError('')
        setItems((current) => current.map((item) => item.id === id ? { ...item, is_read: 1 } : item))
        setUnread((count) => Math.max(0, count - 1))
      })
      .catch((cause) => setActionError(errMsg(cause)))
      .finally(() => setMarkingId(null))
  }

  const readAll = () => {
    setMarkingAll(true)
    apiPost('/api/notifications/read-all', 'POST', {})
      .then(() => {
        setActionError('')
        setItems((current) => current.map((item) => ({ ...item, is_read: 1 })))
        setUnread(0)
      })
      .catch((cause) => setActionError(errMsg(cause)))
      .finally(() => setMarkingAll(false))
  }

  if (!user) {
    return (
      <PageFrame layout="account-notifications-guest">
        <PageHeader title="通知" description="登录后查看对局结果、赛事与系统消息。" />
        <DataRegion title="通知中心" className="mx-auto w-full max-w-5xl"><EmptyState text="请先登录" icon={<Bell className="size-5 opacity-50" />} className="py-8" /></DataRegion>
      </PageFrame>
    )
  }

  return (
    <PageFrame width="default" layout="account-notifications">
      <PageHeader
        title="通知"
        description="对局结果、赛事进度、评论与系统消息集中在这里。"
        actions={<><Button asChild variant="outline" size="sm"><Link to="/messages"><Mail className="size-4" />站内信</Link></Button><Button asChild variant="outline" size="sm"><Link to="/feedback"><Bug className="size-4" />反馈问题</Link></Button>{unread > 0 && <Button type="button" variant="outline" size="sm" onClick={readAll} disabled={markingAll} aria-busy={markingAll}><CheckCheck className="size-4" />{markingAll ? '处理中…' : '全部标记已读'}</Button>}</>}
      />

      <SummaryStrip columns={3}>
        <SummaryMetric label="未读" value={unread} detail="全部通知类型" icon={<Bell className="size-4" />} />
        <SummaryMetric label="当前结果" value={total} detail={filter === 'unread' ? '仅未读通知' : '全部通知'} icon={<MailOpen className="size-4" />} />
        <SummaryMetric label="本页" value={items.length} detail={`第 ${page} 页 · 每页 ${PER_PAGE} 条`} />
      </SummaryStrip>

      <StickyToolbar label="通知筛选">
        <Tabs value={filter} onValueChange={(value) => changeFilter(value as 'all' | 'unread')} className="min-w-0">
          <TabsList>
            <TabsTrigger value="all">全部</TabsTrigger>
            <TabsTrigger value="unread">未读{unread > 0 ? ` ${unread}` : ''}</TabsTrigger>
          </TabsList>
        </Tabs>
        <span className="ml-auto shrink-0 text-xs text-muted-foreground">第 {page} 页</span>
      </StickyToolbar>

      {actionError && <ErrorMsg msg={actionError} />}

      <DataRegion title="消息列表" description={filter === 'unread' ? '仅显示仍需处理的通知' : '按时间倒序显示'}>
        {loadError ? (
          <ErrorMsg msg={loadError} className="px-4 py-5" />
        ) : loading ? (
          <Loading text="正在加载通知…" />
        ) : items.length === 0 ? (
          <EmptyState text={filter === 'unread' ? '没有未读通知' : '暂无通知'} icon={<Bell className="size-5 opacity-50" />} className="py-8" />
        ) : (
          <ul className="divide-y divide-border">
            {items.map((item) => {
              const content = (
                <span className="min-w-0 flex-1">
                  <span className="flex min-w-0 flex-wrap items-center gap-2">
                    {!item.is_read && (
                      <span className="inline-flex min-w-0 shrink-0 items-center gap-1 text-xs font-medium text-primary">
                        <span aria-hidden="true" className="size-2 rounded-full bg-primary" />未读
                      </span>
                    )}
                    <EntityName lines={2} tooltip={false} tooltipFocusable={false} className="min-w-0 text-sm">{item.title}</EntityName>
                    <Badge variant="secondary">{NOTIFICATION_LABELS[item.type] || '系统'}</Badge>
                  </span>
                  {item.body && <OverflowText lines={2} tooltip={false} className="mt-1 text-sm text-muted-foreground">{item.body}</OverflowText>}
                  <time className="mt-1 block font-mono text-xs tabular-nums text-muted-foreground">{fmtTime(item.created_at)}</time>
                </span>
              )

              return (
                <li key={item.id} className={`flex min-w-0 items-start gap-3 px-3 py-2.5 ${item.is_read ? '' : 'bg-primary/5'}`}>
                  {item.link ? (
                    <Link to={item.link} className="flex min-w-0 flex-1 hover:text-primary">{content}</Link>
                  ) : content}
                  {!item.is_read && (
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      disabled={markingId === item.id}
                      aria-busy={markingId === item.id}
                      onClick={() => markRead(item.id)}
                      className="shrink-0"
                    >
                      <Check className="size-3.5" />{markingId === item.id ? '处理中' : '标为已读'}
                    </Button>
                  )}
                </li>
              )
            })}
          </ul>
        )}
      </DataRegion>

      <Pagination page={page} perPage={PER_PAGE} total={total} onPageChange={setPage} />
    </PageFrame>
  )
}
