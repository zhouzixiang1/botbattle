import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { ArrowLeft, Bug, Inbox, Mail, RefreshCw, Send } from 'lucide-react'
import { toast } from 'sonner'

import { apiGet, apiJson, errMsg, userToken, type ApiRequestInit, type CurrentUser } from '@/api'
import type { ThreadDetail, ThreadSummary } from '@/components/communications/types'
import { THREAD_KIND_LABELS } from '@/components/communications/types'
import { ThreadView } from '@/components/communications/thread-view'
import { PageFrame, PageHeader } from '@/components/layout'
import { useAuth } from '@/components/useAuth'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { EmptyState, ErrorMsg, Loading } from '@/components/ui/status'
import { fmtTime } from '@/lib/format'
import { cn } from '@/lib/utils'

type Box = 'inbox' | 'sent'

export default function Messages() {
  const { user } = useAuth()
  return <MessagesForIdentity key={user?.id ?? 'guest'} user={user} />
}

function MessagesForIdentity({ user }: { user: CurrentUser | null }) {
  const navigate = useNavigate()
  const { conversationId = '' } = useParams()
  const [box, setBox] = useState<Box>('inbox')
  const [threads, setThreads] = useState<ThreadSummary[]>([])
  const [selected, setSelected] = useState('')
  const [thread, setThread] = useState<ThreadDetail | null>(null)
  const [reply, setReply] = useState('')
  const [loading, setLoading] = useState(true)
  const [detailLoading, setDetailLoading] = useState(false)
  const [sending, setSending] = useState(false)
  const [error, setError] = useState('')
  const authToken = useRef(userToken.get()).current
  const listRequestRef = useRef<{ seq: number; controller: AbortController | null }>({ seq: 0, controller: null })
  const detailRequestRef = useRef<{ seq: number; controller: AbortController | null }>({ seq: 0, controller: null })
  const sendRequestRef = useRef<{ seq: number; controller: AbortController | null }>({ seq: 0, controller: null })

  const requestOptions = useCallback((signal?: AbortSignal): Omit<ApiRequestInit, 'method' | 'body'> => ({
    signal,
    suppressAuth: true,
    credentials: authToken ? 'omit' : 'include',
    headers: authToken ? { Authorization: `Bearer ${authToken}` } : undefined,
    cache: 'no-store',
    referrerPolicy: 'no-referrer',
  }), [authToken])

  const loadThreads = useCallback(async () => {
    if (!user) return
    listRequestRef.current.controller?.abort()
    const controller = new AbortController()
    const seq = listRequestRef.current.seq + 1
    listRequestRef.current = { seq, controller }
    const isCurrent = () => (
      listRequestRef.current.seq === seq &&
      listRequestRef.current.controller === controller &&
      !controller.signal.aborted
    )
    setLoading(true)
    setError('')
    try {
      const result = await apiGet<{ threads: ThreadSummary[] }>(
        `/api/communications/${box}?per_page=100`,
        requestOptions(controller.signal),
      )
      if (!isCurrent()) return
      setThreads(result.threads || [])
      setSelected((current) => conversationId || (current && result.threads.some((item) => item.public_id === current) ? current : ''))
      if (!result.threads.length && !conversationId) setThread(null)
    } catch (cause) {
      if (isCurrent()) setError(errMsg(cause, '消息列表加载失败'))
    } finally {
      if (isCurrent()) setLoading(false)
    }
  }, [box, conversationId, requestOptions, user])

  useEffect(() => { void loadThreads() }, [loadThreads])

  const openThread = async (publicId: string) => {
    detailRequestRef.current.controller?.abort()
    const controller = new AbortController()
    const seq = detailRequestRef.current.seq + 1
    detailRequestRef.current = { seq, controller }
    const isCurrent = () => (
      detailRequestRef.current.seq === seq &&
      detailRequestRef.current.controller === controller &&
      !controller.signal.aborted
    )
    setSelected(publicId)
    setDetailLoading(true)
    setError('')
    try {
      const result = await apiGet<ThreadDetail>(
        `/api/communications/threads/${encodeURIComponent(publicId)}`,
        requestOptions(controller.signal),
      )
      if (!isCurrent()) return
      if (result.conversation.public_id !== publicId) throw new Error('会话详情标识不一致')
      if (box === 'inbox') {
        await apiJson(
          `/api/communications/threads/${encodeURIComponent(publicId)}/read`,
          'POST',
          {},
          requestOptions(controller.signal),
        )
        if (!isCurrent()) return
        setThreads((items) => items.map((item) => item.public_id === publicId ? { ...item, unread_count: 0 } : item))
      }
      if (!isCurrent()) return
      setThread(result)
    } catch (cause) {
      if (!isCurrent()) return
      setError(errMsg(cause, '消息读取失败'))
      setThread(null)
    } finally {
      if (isCurrent()) setDetailLoading(false)
    }
  }

  useEffect(() => {
    if (user && conversationId) {
      void openThread(conversationId)
    } else {
      detailRequestRef.current.controller?.abort()
      detailRequestRef.current = { seq: detailRequestRef.current.seq + 1, controller: null }
      setDetailLoading(false)
    }
  }, [conversationId, user])

  useEffect(() => () => {
    listRequestRef.current.controller?.abort()
    detailRequestRef.current.controller?.abort()
    sendRequestRef.current.controller?.abort()
  }, [])

  const sendReply = async () => {
    if (!thread || selected !== thread.conversation.public_id || !reply.trim()) return
    sendRequestRef.current.controller?.abort()
    const controller = new AbortController()
    const seq = sendRequestRef.current.seq + 1
    sendRequestRef.current = { seq, controller }
    const publicId = thread.conversation.public_id
    const body = reply.trim()
    const isCurrent = () => (
      sendRequestRef.current.seq === seq &&
      sendRequestRef.current.controller === controller &&
      !controller.signal.aborted
    )
    setSending(true)
    setError('')
    try {
      await apiJson(
        `/api/communications/threads/${encodeURIComponent(publicId)}/reply`,
        'POST',
        { body },
        requestOptions(controller.signal),
      )
      if (!isCurrent()) return
      setReply('')
      await openThread(publicId)
      if (!isCurrent()) return
      await loadThreads()
      if (!isCurrent()) return
      toast.success('回复已发送')
    } catch (cause) {
      if (isCurrent()) setError(errMsg(cause, '回复发送失败'))
    } finally {
      if (isCurrent()) setSending(false)
    }
  }

  if (!user) {
    return (
      <PageFrame width="narrow" layout="messages-guest">
        <PageHeader title="站内信" description="登录后查看平台消息并在原线程回复。" />
        <EmptyState text="请先登录" className="rounded-xl border py-12" />
      </PageFrame>
    )
  }

  return (
    <PageFrame width="full" layout="messages-mailbox" className="gap-3">
      <PageHeader
        title="站内信"
        description="平台通知、群发公告与支持回复集中归档；用户不能向其他用户发私信。"
        actions={(
          <Button asChild size="sm"><Link to="/feedback"><Bug className="size-4" />反馈问题</Link></Button>
        )}
      />
      {error && <ErrorMsg msg={error} />}
      <section className="grid min-h-[calc(100dvh-12rem)] min-w-0 overflow-hidden rounded-xl border bg-card lg:grid-cols-[11rem_minmax(15rem,22rem)_minmax(0,1fr)]">
        <nav aria-label="邮箱文件夹" className={cn('border-b p-2 lg:border-r lg:border-b-0', selected && 'hidden lg:block')}>
          <div className="grid grid-cols-2 gap-1 lg:grid-cols-1">
            <Button variant={box === 'inbox' ? 'secondary' : 'ghost'} className="justify-start" onClick={() => { detailRequestRef.current.controller?.abort(); detailRequestRef.current = { seq: detailRequestRef.current.seq + 1, controller: null }; setDetailLoading(false); setBox('inbox'); setSelected(''); setThread(null); navigate('/messages') }}>
              <Inbox className="size-4" />收件箱
            </Button>
            <Button variant={box === 'sent' ? 'secondary' : 'ghost'} className="justify-start" onClick={() => { detailRequestRef.current.controller?.abort(); detailRequestRef.current = { seq: detailRequestRef.current.seq + 1, controller: null }; setDetailLoading(false); setBox('sent'); setSelected(''); setThread(null); navigate('/messages') }}>
              <Send className="size-4" />已发送
            </Button>
          </div>
          <div className="mt-2 border-t pt-2">
            <Button asChild variant="ghost" className="w-full justify-start"><Link to="/notifications"><Mail className="size-4" />通知中心</Link></Button>
            <Button asChild variant="ghost" className="w-full justify-start"><Link to="/feedback"><Bug className="size-4" />问题反馈</Link></Button>
          </div>
        </nav>

        <div className={cn('min-w-0 border-b lg:border-r lg:border-b-0', selected && 'hidden lg:block')}>
          <div className="flex h-12 items-center gap-2 border-b px-3">
            <h2 className="text-sm font-semibold">{box === 'inbox' ? '收件箱' : '已发送'}</h2>
            <Badge variant="secondary" className="tabular-nums">{threads.length}</Badge>
            <Button variant="ghost" size="icon" className="ml-auto" aria-label="刷新消息" onClick={() => void loadThreads()} disabled={loading}>
              <RefreshCw className={cn('size-4', loading && 'animate-spin')} />
            </Button>
          </div>
          {loading ? <Loading text="正在加载消息…" /> : threads.length === 0 ? (
            <EmptyState text={box === 'inbox' ? '暂无来信' : '暂无已发送回复'} className="py-14" />
          ) : (
            <ul className="divide-y">
              {threads.map((item) => (
                <li key={item.public_id}>
                  <button
                    type="button"
                    onClick={() => navigate(`/messages/${encodeURIComponent(item.public_id)}`)}
                    className="min-h-20 w-full min-w-0 cursor-pointer px-3 py-2 text-left transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
                  >
                    <span className="flex min-w-0 items-center gap-2">
                      <span className={cn('truncate text-sm', item.unread_count > 0 && 'font-semibold')}>{item.subject || '无主题消息'}</span>
                      {item.unread_count > 0 && <Badge className="ml-auto shrink-0 tabular-nums">{item.unread_count}</Badge>}
                    </span>
                    <span className="mt-1 line-clamp-2 break-words text-xs leading-relaxed text-muted-foreground">{item.latest_body}</span>
                    <span className="mt-1 flex items-center gap-2 text-[0.6875rem] text-muted-foreground">
                      <span>{THREAD_KIND_LABELS[item.kind] || '消息'}</span>
                      <time className="ml-auto font-mono tabular-nums">{fmtTime(item.latest_at)}</time>
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className={cn('min-h-0 min-w-0', !selected && 'hidden lg:flex')}>
          {selected && (
            <div className="flex h-12 items-center border-b px-2 lg:hidden">
              <Button variant="ghost" size="sm" onClick={() => { setSelected(''); setThread(null); navigate('/messages') }}><ArrowLeft className="size-4" />返回列表</Button>
            </div>
          )}
          {detailLoading ? <Loading text="正在读取消息…" /> : (
            <ThreadView thread={thread} reply={reply} onReplyChange={setReply} onSend={() => void sendReply()} sending={sending} />
          )}
        </div>
      </section>
    </PageFrame>
  )
}
