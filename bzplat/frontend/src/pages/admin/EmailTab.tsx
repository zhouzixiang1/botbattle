import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import {
  AlertTriangle,
  ArrowLeft,
  Bug,
  CalendarClock,
  CheckCircle2,
  Inbox,
  MailCheck,
  MailPlus,
  Megaphone,
  Paperclip,
  RefreshCw,
  RotateCcw,
  Send,
  ShieldAlert,
  Users,
  XCircle,
  type LucideIcon,
} from 'lucide-react'
import { toast } from 'sonner'

import { apiGet, apiJson, errMsg } from '@/api'
import Pagination from '@/components/Pagination'
import { ThreadView } from '@/components/communications/thread-view'
import type {
  BroadcastDetail,
  BroadcastSummary,
  BugDetail,
  BugSummary,
  FailedDelivery,
  ThreadDetail,
  ThreadSummary,
} from '@/components/communications/types'
import {
  AUDIENCE_KIND_LABELS,
  BROADCAST_STATE_LABELS,
  BUG_CATEGORY_LABELS,
  BUG_IMPACT_LABELS,
  BUG_STATUS_LABELS,
  THREAD_KIND_LABELS,
} from '@/components/communications/types'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Identifier } from '@/components/ui/overflow-text'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { EmptyState, ErrorMsg, Loading } from '@/components/ui/status'
import { Switch } from '@/components/ui/switch'
import { Textarea } from '@/components/ui/textarea'
import { useConfirm } from '@/hooks/use-confirm'
import { fmtTime } from '@/lib/format'
import { cn } from '@/lib/utils'

type Folder = 'inbox' | 'sent' | 'broadcasts' | 'feedback' | 'failed'
type Selection =
  | { kind: 'thread'; publicId: string }
  | { kind: 'broadcast'; publicId: string }
  | { kind: 'bug'; publicId: string }
  | { kind: 'delivery'; publicId: string }

interface FolderItem {
  key: Folder
  label: string
  icon: LucideIcon
}

const FOLDERS: FolderItem[] = [
  { key: 'inbox', label: '收件箱', icon: Inbox },
  { key: 'sent', label: '已发送', icon: Send },
  { key: 'broadcasts', label: '群发记录', icon: Megaphone },
  { key: 'feedback', label: '问题反馈', icon: Bug },
  { key: 'failed', label: '失败投递', icon: ShieldAlert },
]

const BUG_TRANSITIONS: Record<string, string[]> = {
  new: ['acknowledged', 'needs_info', 'in_progress', 'resolved', 'duplicate', 'wont_fix'],
  acknowledged: ['needs_info', 'in_progress', 'resolved', 'duplicate', 'wont_fix'],
  needs_info: ['in_progress', 'resolved', 'duplicate', 'wont_fix'],
  in_progress: ['needs_info', 'resolved', 'duplicate', 'wont_fix'],
  resolved: [],
  duplicate: [],
  wont_fix: [],
}

const AUDIENCE_OPTIONS = [
  ['active_users', '全部启用用户'],
  ['role', '按角色'],
  ['game_bot_owners', '按游戏 Bot 所有者'],
  ['contest_entrants', '锦标赛参赛者'],
  ['selected_users', '指定用户'],
] as const

const COUNT_LABELS: Record<string, string> = {
  pending: '待处理',
  processing: '处理中',
  delivered: '已投影',
  failed: '失败',
  cancelled: '已取消',
  'in_app:queued': '站内信待发',
  'in_app:sending': '站内信发送中',
  'in_app:sent': '站内信已发',
  'in_app:failed': '站内信失败',
  'email:queued': '邮件待发',
  'email:sending': '邮件发送中',
  'email:sent': '邮件已发',
  'email:failed': '邮件失败',
  'email:cancelled': '邮件已取消',
}

type AudienceKind = typeof AUDIENCE_OPTIONS[number][0]

interface BroadcastPreview {
  public_id: string
  state: string
  audience_count: number
  audience_snapshot_hash: string
  approval_token: string
  preview_expires_at: string
  channels: string[]
  subject: string
  body_text: string
}

function BroadcastStatus({ state }: { state: string }) {
  const destructive = state === 'cancelled'
  const active = state === 'running'
  return (
    <Badge variant={destructive ? 'destructive' : active ? 'default' : 'secondary'}>
      {BROADCAST_STATE_LABELS[state] || state}
    </Badge>
  )
}

function ListButton({ selected, onClick, children }: { selected: boolean; onClick: () => void; children: ReactNode }) {
  return (
    <button
      type="button"
      aria-current={selected ? 'true' : undefined}
      onClick={onClick}
      className={cn(
        'w-full min-w-0 cursor-pointer border-b px-3 py-2.5 text-left transition-colors last:border-b-0 hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring',
        selected && 'bg-primary/5',
      )}
    >
      {children}
    </button>
  )
}

function CountGrid({ values }: { values: Record<string, number> }) {
  const items = Object.entries(values)
  if (!items.length) return <span className="text-xs text-muted-foreground">暂无数据</span>
  return (
    <dl className="grid min-w-0 grid-cols-2 gap-2 sm:grid-cols-3">
      {items.map(([key, value]) => (
        <div key={key} className="min-w-0 rounded-lg border bg-muted/20 px-2.5 py-2">
          <dt className="truncate text-xs text-muted-foreground">{COUNT_LABELS[key] || key}</dt>
          <dd className="mt-0.5 font-mono text-sm font-semibold tabular-nums">{value}</dd>
        </div>
      ))}
    </dl>
  )
}

function objectValue(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
}

function DiagnosticSummary({ bug }: { bug: BugDetail }) {
  const bundle = objectValue(bug.diagnostic?.bundle)
  const client = objectValue(bundle.client)
  const viewport = objectValue(client.viewport)
  const failure = objectValue(bundle.failed_api)
  const context = objectValue(bundle.public_context)
  const queue = objectValue(context.queue)
  const rows = [
    ['路由', String(bundle.route || bug.current_route || '未记录')],
    ['角色', String(bundle.role || '未知')],
    ['浏览器 / 系统', `${String(client.browser_family || '未知')} / ${String(client.os_family || '未知')}`],
    ['视口', viewport.width ? `${String(viewport.width)} × ${String(viewport.height)}` : '未记录'],
    ['主题', String(client.theme || '未知')],
    ['构建', String(bundle.build || '未知')],
    ['失败接口', failure.template ? `${String(failure.template)} · ${String(failure.status || '—')}` : '无'],
    ['追踪号', String(failure.trace_id || '无')],
    ['提交时队列', `等待 ${String(queue.pending ?? '—')} / 运行 ${String(queue.running ?? '—')}`],
  ]
  return (
    <dl className="grid min-w-0 gap-x-4 gap-y-2 text-xs sm:grid-cols-2 xl:grid-cols-3">
      {rows.map(([label, value]) => (
        <div key={label} className="min-w-0">
          <dt className="text-muted-foreground">{label}</dt>
          <dd className="mt-0.5 break-words font-mono text-foreground">{value}</dd>
        </div>
      ))}
    </dl>
  )
}

function BroadcastCompose({ onCreated, onClose }: { onCreated: (publicId: string) => void; onClose: () => void }) {
  const [confirm, confirmDialog] = useConfirm()
  const [audienceKind, setAudienceKind] = useState<AudienceKind>('active_users')
  const [role, setRole] = useState('user')
  const [gameId, setGameId] = useState('holdem')
  const [contestId, setContestId] = useState('')
  const [usernames, setUsernames] = useState('')
  const [subject, setSubject] = useState('')
  const [body, setBody] = useState('')
  const [email, setEmail] = useState(false)
  const [scheduledAt, setScheduledAt] = useState('')
  const [preview, setPreview] = useState<BroadcastPreview | null>(null)
  const [previewFingerprint, setPreviewFingerprint] = useState('')
  const [previewBusy, setPreviewBusy] = useState(false)
  const [approving, setApproving] = useState(false)
  const [error, setError] = useState('')
  const previewRequestRef = useRef<{ seq: number; controller: AbortController | null }>({ seq: 0, controller: null })
  const previewRef = useRef<BroadcastPreview | null>(null)
  const previewFingerprintRef = useRef('')
  const currentFingerprintRef = useRef('')
  const selectedUsernames = useMemo(
    () => usernames.split(/[\s,，;]+/).map((item) => item.trim()).filter(Boolean),
    [usernames],
  )
  const audience = useMemo(() => {
    if (audienceKind === 'role') return { kind: audienceKind, role }
    if (audienceKind === 'game_bot_owners') return { kind: audienceKind, game_id: gameId }
    if (audienceKind === 'contest_entrants') return { kind: audienceKind, contest_id: Number(contestId) }
    if (audienceKind === 'selected_users') {
      return { kind: audienceKind, usernames: selectedUsernames }
    }
    return { kind: audienceKind }
  }, [audienceKind, contestId, gameId, role, selectedUsernames])

  const previewPayload = useMemo(() => ({
    audience,
    subject: subject.trim(),
    body: body.trim(),
    channels: email ? ['in_app', 'email'] : ['in_app'],
  }), [audience, body, email, subject])
  const currentFingerprint = useMemo(
    () => JSON.stringify(previewPayload),
    [previewPayload],
  )
  currentFingerprintRef.current = currentFingerprint
  previewRef.current = preview
  previewFingerprintRef.current = previewFingerprint

  const invalidate = () => {
    previewRequestRef.current.controller?.abort()
    previewRequestRef.current = {
      seq: previewRequestRef.current.seq + 1,
      controller: null,
    }
    previewRef.current = null
    previewFingerprintRef.current = ''
    setPreview(null)
    setPreviewFingerprint('')
    setPreviewBusy(false)
  }

  useEffect(() => () => previewRequestRef.current.controller?.abort(), [])

  const previewBroadcast = async () => {
    if (!subject.trim() || !body.trim()) { setError('请填写主题和通知正文'); return }
    if (audienceKind === 'contest_entrants' && (!Number.isInteger(Number(contestId)) || Number(contestId) < 1)) {
      setError('请填写有效的锦标赛内部 ID'); return
    }
    if (audienceKind === 'selected_users' && !selectedUsernames.length) {
      setError('请至少填写一个公开用户名'); return
    }
    previewRequestRef.current.controller?.abort()
    const controller = new AbortController()
    const seq = previewRequestRef.current.seq + 1
    const requestFingerprint = currentFingerprint
    const requestPayload = previewPayload
    previewRequestRef.current = { seq, controller }
    previewRef.current = null
    previewFingerprintRef.current = ''
    setPreview(null)
    setPreviewFingerprint('')
    setPreviewBusy(true); setError('')
    const isCurrent = () => (
      previewRequestRef.current.seq === seq &&
      previewRequestRef.current.controller === controller &&
      currentFingerprintRef.current === requestFingerprint &&
      !controller.signal.aborted
    )
    try {
      const result = await apiJson<{ broadcast: BroadcastPreview }>(
        '/api/admin/communications/broadcasts/preview',
        'POST',
        requestPayload,
        { signal: controller.signal },
      )
      if (!isCurrent()) return
      previewRef.current = result.broadcast
      previewFingerprintRef.current = requestFingerprint
      setPreview(result.broadcast)
      setPreviewFingerprint(requestFingerprint)
    } catch (cause) {
      if (isCurrent()) setError(errMsg(cause, '预览生成失败'))
    } finally {
      if (isCurrent()) setPreviewBusy(false)
    }
  }

  const approve = async () => {
    if (!preview || previewFingerprint !== currentFingerprint) {
      invalidate(); setError('表单已变化，请重新生成预览'); return
    }
    const boundPreview = preview
    const boundFingerprint = previewFingerprint
    if (!await confirm({
      title: '确认群发通知',
      desc: `将向已固定的 ${boundPreview.audience_count} 名用户投递；批准后不会重新计算受众。`,
      confirmText: scheduledAt ? '确认定时发送' : '确认发送',
    })) return
    if (
      previewRef.current?.public_id !== boundPreview.public_id ||
      previewFingerprintRef.current !== boundFingerprint ||
      currentFingerprintRef.current !== boundFingerprint
    ) {
      invalidate(); setError('表单或预览已变化，请重新生成预览'); return
    }
    setApproving(true); setError('')
    try {
      const result = await apiJson<{ broadcast: { public_id: string } }>('/api/admin/communications/broadcasts/create', 'POST', {
        preview_public_id: boundPreview.public_id,
        approval_token: boundPreview.approval_token,
        confirm: true,
        scheduled_at: scheduledAt ? new Date(scheduledAt).toISOString() : null,
      })
      toast.success(scheduledAt ? '群发通知已安排' : '群发通知已进入投递队列')
      onCreated(result.broadcast.public_id)
    } catch (cause) { setError(errMsg(cause, '群发批准失败')) }
    finally { setApproving(false) }
  }

  const closeCompose = () => {
    previewRequestRef.current.controller?.abort()
    onClose()
  }

  return (
    <div className="min-w-0 p-3 sm:p-4">
      <div className="mb-3 flex min-w-0 flex-wrap items-start gap-2 border-b pb-3">
        <div className="min-w-0">
          <h3 className="text-sm font-semibold">新建群发通知</h3>
          <p className="mt-0.5 text-xs text-muted-foreground">先预览固定受众快照，再二次确认发送；站内信始终保留。</p>
        </div>
        <Button type="button" variant="ghost" size="sm" className="ml-auto" disabled={approving} onClick={closeCompose}><XCircle className="size-4" />关闭</Button>
      </div>
      <ErrorMsg msg={error} className="mb-3" />
      <div className="grid min-w-0 gap-4 xl:grid-cols-[minmax(18rem,24rem)_minmax(0,1fr)]">
        <div className="space-y-3">
          <div className="space-y-1.5">
            <Label>受众</Label>
            <Select value={audienceKind} onValueChange={(value) => { setAudienceKind(value as AudienceKind); invalidate() }}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>{AUDIENCE_OPTIONS.map(([value, label]) => <SelectItem key={value} value={value}>{label}</SelectItem>)}</SelectContent>
            </Select>
          </div>
          {audienceKind === 'role' && <div className="space-y-1.5"><Label>角色</Label><Select value={role} onValueChange={(value) => { setRole(value); invalidate() }}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectItem value="user">普通用户</SelectItem><SelectItem value="organizer">组织者</SelectItem><SelectItem value="admin">管理员</SelectItem></SelectContent></Select></div>}
          {audienceKind === 'game_bot_owners' && <div className="space-y-1.5"><Label>游戏</Label><Select value={gameId} onValueChange={(value) => { setGameId(value); invalidate() }}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectItem value="holdem">德州扑克</SelectItem><SelectItem value="gomoku">五子棋</SelectItem><SelectItem value="pencil">点格棋</SelectItem></SelectContent></Select></div>}
          {audienceKind === 'contest_entrants' && <div className="space-y-1.5"><Label htmlFor="broadcast-contest-id">锦标赛内部 ID</Label><Input id="broadcast-contest-id" type="number" min={1} value={contestId} onChange={(event) => { setContestId(event.target.value); invalidate() }} placeholder="仅用于精确匹配该赛事" /></div>}
          {audienceKind === 'selected_users' && <div className="space-y-1.5"><Label htmlFor="broadcast-users">公开用户名</Label><Textarea id="broadcast-users" value={usernames} onChange={(event) => { setUsernames(event.target.value); invalidate() }} rows={5} placeholder="每行一个，也可用逗号分隔" /></div>}
          <div className="rounded-lg border bg-muted/20 p-3">
            <div className="flex min-h-10 items-center gap-2"><Switch checked disabled aria-label="站内信必选" /><span className="text-sm">站内信（必选）</span></div>
            <div className="mt-1 flex min-h-10 items-center gap-2"><Switch checked={email} onCheckedChange={(value) => { setEmail(value); invalidate() }} /><span className="text-sm">同时发送邮件</span></div>
          </div>
          <div className="space-y-1.5"><Label htmlFor="broadcast-schedule">定时发送（可选）</Label><Input id="broadcast-schedule" type="datetime-local" value={scheduledAt} onChange={(event) => setScheduledAt(event.target.value)} /></div>
        </div>

        <div className="space-y-3">
          <div className="space-y-1.5"><Label htmlFor="broadcast-subject">主题</Label><Input id="broadcast-subject" maxLength={160} value={subject} onChange={(event) => { setSubject(event.target.value); invalidate() }} placeholder="用户将在收件箱中看到这个主题" /></div>
          <div className="space-y-1.5"><Label htmlFor="broadcast-body">通知正文</Label><Textarea id="broadcast-body" maxLength={20_000} rows={10} value={body} onChange={(event) => { setBody(event.target.value); invalidate() }} placeholder="说明发生了什么、用户需要做什么。" /><div className="text-right text-xs tabular-nums text-muted-foreground">{body.length}/20000</div></div>
          {!preview ? (
            <Button type="button" disabled={previewBusy || approving} aria-busy={previewBusy} onClick={() => void previewBroadcast()}><MailCheck className="size-4" />{previewBusy ? '正在计算受众…' : '预览受众与内容'}</Button>
          ) : (
            <section className="rounded-xl border border-primary/25 bg-primary/5 p-3" aria-label="群发预览">
              <div className="flex flex-wrap items-center gap-2"><CheckCircle2 className="size-4 text-primary" /><h4 className="text-sm font-semibold">快照已固定</h4><Badge className="ml-auto tabular-nums">{preview.audience_count} 人</Badge></div>
              <dl className="mt-3 grid gap-2 text-xs sm:grid-cols-2">
                <div><dt className="text-muted-foreground">渠道</dt><dd className="mt-0.5">{preview.channels.map((item) => item === 'in_app' ? '站内信' : '邮件').join(' + ')}</dd></div>
                <div><dt className="text-muted-foreground">预览有效期</dt><dd className="mt-0.5 font-mono">{fmtTime(preview.preview_expires_at)}</dd></div>
                <div className="min-w-0 sm:col-span-2"><dt className="text-muted-foreground">快照校验</dt><dd className="mt-0.5"><Identifier>{preview.audience_snapshot_hash}</Identifier></dd></div>
              </dl>
              <div className="mt-3 flex flex-wrap justify-end gap-2"><Button type="button" variant="outline" size="sm" disabled={approving} onClick={invalidate}>返回修改</Button><Button type="button" size="sm" disabled={previewBusy || approving} onClick={() => void approve()}><Megaphone className="size-4" />{approving ? '正在批准…' : scheduledAt ? '批准定时发送' : '批准并发送'}</Button></div>
            </section>
          )}
        </div>
      </div>
      {confirmDialog}
    </div>
  )
}

export default function CommunicationsTab() {
  const [confirm, confirmDialog] = useConfirm()
  const [folder, setFolder] = useState<Folder>('inbox')
  const [composing, setComposing] = useState(false)
  const [threads, setThreads] = useState<ThreadSummary[]>([])
  const [broadcasts, setBroadcasts] = useState<BroadcastSummary[]>([])
  const [bugs, setBugs] = useState<BugSummary[]>([])
  const [failures, setFailures] = useState<FailedDelivery[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [bugFilter, setBugFilter] = useState('all')
  const [selection, setSelection] = useState<Selection | null>(null)
  const [thread, setThread] = useState<ThreadDetail | null>(null)
  const [broadcast, setBroadcast] = useState<BroadcastDetail | null>(null)
  const [bug, setBug] = useState<BugDetail | null>(null)
  const [failure, setFailure] = useState<FailedDelivery | null>(null)
  const [reply, setReply] = useState('')
  const [replyEmail, setReplyEmail] = useState(false)
  const [statusDraft, setStatusDraft] = useState('')
  const [statusNote, setStatusNote] = useState('')
  const [duplicateOf, setDuplicateOf] = useState('')
  const [listLoading, setListLoading] = useState(true)
  const [detailLoading, setDetailLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const selectionRef = useRef<Selection | null>(null)
  const detailRequestRef = useRef<{ seq: number; controller: AbortController | null }>({ seq: 0, controller: null })
  const attachmentControllersRef = useRef<Set<AbortController>>(new Set())
  const perPage = 30

  const cancelAttachmentRequests = () => {
    for (const controller of attachmentControllersRef.current) controller.abort()
    attachmentControllersRef.current.clear()
  }

  const setActiveSelection = (next: Selection | null) => {
    const current = selectionRef.current
    if (current?.kind !== next?.kind || current?.publicId !== next?.publicId) {
      cancelAttachmentRequests()
    }
    selectionRef.current = next
    setSelection(next)
  }

  const cancelDetailRequest = () => {
    detailRequestRef.current.controller?.abort()
    detailRequestRef.current = { seq: detailRequestRef.current.seq + 1, controller: null }
  }

  const beginDetailRequest = (next: Selection) => {
    detailRequestRef.current.controller?.abort()
    const controller = new AbortController()
    const seq = detailRequestRef.current.seq + 1
    detailRequestRef.current = { seq, controller }
    setActiveSelection(next)
    setDetailLoading(true)
    setError('')
    return { controller, seq }
  }

  const requestIsCurrent = (seq: number, controller: AbortController) => (
    detailRequestRef.current.seq === seq &&
    detailRequestRef.current.controller === controller &&
    !controller.signal.aborted
  )

  const selectionMatches = (kind: Selection['kind'], publicId: string) => (
    selectionRef.current?.kind === kind && selectionRef.current.publicId === publicId
  )

  const resetDetail = () => {
    cancelDetailRequest(); setActiveSelection(null); setDetailLoading(false); setThread(null); setBroadcast(null); setBug(null); setFailure(null); setReply(''); setReplyEmail(false)
  }

  useEffect(() => () => {
    detailRequestRef.current.controller?.abort()
    cancelAttachmentRequests()
  }, [])

  const loadList = useCallback(async () => {
    setListLoading(true); setError('')
    try {
      if (folder === 'inbox' || folder === 'sent') {
        const result = await apiGet<{ threads: ThreadSummary[]; total: number }>(`/api/admin/communications/${folder}?page=${page}&per_page=${perPage}`)
        setThreads(result.threads || []); setTotal(result.total || 0)
      } else if (folder === 'broadcasts') {
        const result = await apiGet<{ broadcasts: BroadcastSummary[]; total: number }>(`/api/admin/communications/broadcasts?page=${page}&per_page=${perPage}`)
        setBroadcasts(result.broadcasts || []); setTotal(result.total || 0)
      } else if (folder === 'feedback') {
        const suffix = bugFilter === 'all' ? '' : `&status=${encodeURIComponent(bugFilter)}`
        const result = await apiGet<{ bug_reports: BugSummary[]; total: number }>(`/api/admin/bug-reports?page=${page}&per_page=${perPage}${suffix}`)
        setBugs(result.bug_reports || []); setTotal(result.total || 0)
      } else {
        const result = await apiGet<{ deliveries: FailedDelivery[]; total: number }>(`/api/admin/communications/failed?page=${page}&per_page=${perPage}`)
        setFailures(result.deliveries || []); setTotal(result.total || 0)
      }
    } catch (cause) { setError(errMsg(cause, '列表加载失败')) }
    finally { setListLoading(false) }
  }, [bugFilter, folder, page])

  useEffect(() => { void loadList() }, [loadList])

  const chooseFolder = (next: Folder) => {
    setFolder(next); setPage(1); setComposing(false); resetDetail()
  }

  const openThread = async (publicId: string) => {
    const { controller, seq } = beginDetailRequest({ kind: 'thread', publicId })
    try {
      const result = await apiGet<ThreadDetail>(`/api/admin/communications/threads/${encodeURIComponent(publicId)}`, { signal: controller.signal })
      if (!requestIsCurrent(seq, controller)) return
      if (result.conversation.public_id !== publicId) throw new Error('会话详情标识不一致')
      setThread(result)
      setBroadcast(null); setBug(null); setFailure(null)
    } catch (cause) {
      if (!requestIsCurrent(seq, controller)) return
      setThread(null); setError(errMsg(cause, '会话读取失败'))
    } finally {
      if (requestIsCurrent(seq, controller)) setDetailLoading(false)
    }
  }

  const openBroadcast = async (publicId: string) => {
    const { controller, seq } = beginDetailRequest({ kind: 'broadcast', publicId })
    try {
      const result = await apiGet<{ broadcast: BroadcastDetail }>(`/api/admin/communications/broadcasts/${encodeURIComponent(publicId)}`, { signal: controller.signal })
      if (!requestIsCurrent(seq, controller)) return
      if (result.broadcast.public_id !== publicId) throw new Error('群发详情标识不一致')
      setBroadcast(result.broadcast); setThread(null); setBug(null); setFailure(null)
    } catch (cause) {
      if (!requestIsCurrent(seq, controller)) return
      setBroadcast(null); setError(errMsg(cause, '群发详情读取失败'))
    } finally {
      if (requestIsCurrent(seq, controller)) setDetailLoading(false)
    }
  }

  const openBug = async (publicId: string) => {
    const { controller, seq } = beginDetailRequest({ kind: 'bug', publicId })
    try {
      const result = await apiGet<{ bug_report: BugDetail }>(`/api/admin/bug-reports/${encodeURIComponent(publicId)}`, { signal: controller.signal })
      if (!requestIsCurrent(seq, controller)) return
      const detail = result.bug_report
      if (detail.public_id !== publicId) throw new Error('反馈详情标识不一致')
      const conversation = await apiGet<ThreadDetail>(`/api/admin/communications/threads/${encodeURIComponent(detail.conversation_public_id)}`, { signal: controller.signal })
      if (!requestIsCurrent(seq, controller)) return
      if (conversation.conversation.public_id !== detail.conversation_public_id) throw new Error('反馈会话标识不一致')
      setBug(detail); setThread(conversation); setBroadcast(null); setFailure(null)
      setStatusDraft(BUG_TRANSITIONS[detail.status]?.[0] || '')
      setStatusNote(''); setDuplicateOf('')
    } catch (cause) {
      if (!requestIsCurrent(seq, controller)) return
      setBug(null); setThread(null); setError(errMsg(cause, '反馈详情读取失败'))
    } finally {
      if (requestIsCurrent(seq, controller)) setDetailLoading(false)
    }
  }

  const openFailure = (item: FailedDelivery) => {
    cancelDetailRequest(); setDetailLoading(false); setActiveSelection({ kind: 'delivery', publicId: item.public_id }); setFailure(item); setThread(null); setBroadcast(null); setBug(null)
  }

  const sendReply = async () => {
    if (!thread || !reply.trim()) return
    const contextMatches = selectionRef.current?.kind === 'thread'
      ? selectionMatches('thread', thread.conversation.public_id)
      : Boolean(bug && selectionMatches('bug', bug.public_id) && thread.conversation.public_id === bug.conversation_public_id)
    if (!contextMatches) { setError('当前选择已变化，请重新打开会话后再操作'); return }
    const selectedKind = selectionRef.current?.kind
    const selectedPublicId = selectionRef.current?.publicId || ''
    setBusy(true); setError('')
    try {
      await apiJson(`/api/admin/communications/threads/${encodeURIComponent(thread.conversation.public_id)}/reply`, 'POST', { body: reply.trim(), email: replyEmail })
      setReply(''); setReplyEmail(false)
      if (selectionMatches(selectedKind as Selection['kind'], selectedPublicId)) {
        if (bug) await openBug(bug.public_id)
        else await openThread(thread.conversation.public_id)
      }
      await loadList(); toast.success('回复已发送')
    } catch (cause) { setError(errMsg(cause, '回复发送失败')) }
    finally { setBusy(false) }
  }

  const updateBugStatus = async () => {
    if (!bug || !statusDraft) return
    if (!selectionMatches('bug', bug.public_id)) { setError('当前选择已变化，请重新打开反馈后再操作'); return }
    const publicId = bug.public_id
    setBusy(true); setError('')
    try {
      await apiJson(`/api/admin/bug-reports/${encodeURIComponent(bug.public_id)}/status`, 'PATCH', {
        status: statusDraft,
        note: statusNote.trim(),
        duplicate_of: statusDraft === 'duplicate' ? duplicateOf.trim() || null : null,
      })
      if (selectionMatches('bug', publicId)) await openBug(publicId)
      await loadList(); toast.success('反馈状态已更新')
    } catch (cause) { setError(errMsg(cause, '状态更新失败')) }
    finally { setBusy(false) }
  }

  const retryBroadcast = async () => {
    if (!broadcast) return
    if (!selectionMatches('broadcast', broadcast.public_id)) { setError('当前选择已变化，请重新打开群发后再操作'); return }
    const publicId = broadcast.public_id
    const recipients = broadcast.failed_recipients.slice(0, 100).map((item) => item.public_id)
    const remaining = 100 - recipients.length
    const deliveries = broadcast.failed_deliveries.slice(0, remaining).map((item) => item.public_id)
    if (!recipients.length && !deliveries.length) return
    setBusy(true); setError('')
    try {
      const result = await apiJson<{ retry: { retried_recipients: string[]; retried_deliveries: string[]; exhausted: string[] } }>(`/api/admin/communications/broadcasts/${encodeURIComponent(broadcast.public_id)}/retry-failed`, 'POST', {
        recipient_public_ids: recipients,
        delivery_public_ids: deliveries,
      })
      if (selectionMatches('broadcast', publicId)) await openBroadcast(publicId)
      await loadList()
      const retried = result.retry.retried_recipients.length + result.retry.retried_deliveries.length
      toast.success(`已重试 ${retried} 项${result.retry.exhausted.length ? `，${result.retry.exhausted.length} 项已达上限` : ''}`)
    } catch (cause) { setError(errMsg(cause, '失败项重试失败')) }
    finally { setBusy(false) }
  }

  const cancelBroadcast = async () => {
    if (!broadcast || !selectionMatches('broadcast', broadcast.public_id)) { setError('当前选择已变化，请重新打开群发后再操作'); return }
    const publicId = broadcast.public_id
    if (!await confirm({ title: '取消群发', desc: '仍在待处理队列的受众和邮件将取消；已生成的站内信无法撤回，已进入 SMTP 的邮件可能仍会完成。', confirmText: '取消群发', danger: true })) return
    if (!selectionMatches('broadcast', publicId)) { setError('当前选择已变化，请重新打开群发后再操作'); return }
    setBusy(true); setError('')
    try {
      await apiJson(`/api/admin/communications/broadcasts/${encodeURIComponent(publicId)}/cancel`, 'POST', {})
      if (selectionMatches('broadcast', publicId)) await openBroadcast(publicId)
      await loadList(); toast.success('群发已取消')
    } catch (cause) { setError(errMsg(cause, '群发取消失败')) }
    finally { setBusy(false) }
  }

  const downloadAttachment = async (attachmentId: string, name: string) => {
    if (!bug || !selectionMatches('bug', bug.public_id)) { setError('当前选择已变化，请重新打开反馈后再操作'); return }
    const bugPublicId = bug.public_id
    const controller = new AbortController()
    attachmentControllersRef.current.add(controller)
    try {
      const response = await fetch(`/api/feedback/bugs/${encodeURIComponent(bugPublicId)}/attachments/${encodeURIComponent(attachmentId)}`, {
        credentials: 'include',
        cache: 'no-store',
        referrerPolicy: 'no-referrer',
        signal: controller.signal,
      })
      if (controller.signal.aborted || !selectionMatches('bug', bugPublicId)) return
      if (!response.ok) throw new Error('附件读取失败')
      const blob = await response.blob()
      if (controller.signal.aborted || !selectionMatches('bug', bugPublicId)) return
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement('a'); anchor.href = url; anchor.download = name; anchor.click()
      setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch (cause) {
      if (!controller.signal.aborted && selectionMatches('bug', bugPublicId)) {
        setError(errMsg(cause, '附件读取失败'))
      }
    } finally {
      attachmentControllersRef.current.delete(controller)
    }
  }

  const selectedId = selection?.publicId || ''
  const activeFolder = FOLDERS.find((item) => item.key === folder) ?? FOLDERS[0]

  const renderList = () => {
    if (listLoading) return <Loading text="正在整理邮箱…" />
    if (folder === 'inbox' || folder === 'sent') {
      if (!threads.length) return <EmptyState text={folder === 'inbox' ? '暂无来信' : '暂无已发送会话'} />
      return threads.map((item) => <ListButton key={item.public_id} selected={selectedId === item.public_id} onClick={() => void openThread(item.public_id)}><span className="flex min-w-0 items-center gap-2"><span className="truncate text-sm font-medium">{item.subject || '无主题消息'}</span><Badge variant="outline" className="ml-auto shrink-0">{THREAD_KIND_LABELS[item.kind] || '消息'}</Badge></span><span className="mt-1 line-clamp-2 break-words text-xs leading-relaxed text-muted-foreground">{item.latest_body}</span><time className="mt-1 block text-right font-mono text-[0.6875rem] tabular-nums text-muted-foreground">{fmtTime(item.latest_at)}</time></ListButton>)
    }
    if (folder === 'broadcasts') {
      if (!broadcasts.length) return <EmptyState text="暂无群发记录" />
      return broadcasts.map((item) => <ListButton key={item.public_id} selected={selectedId === item.public_id} onClick={() => void openBroadcast(item.public_id)}><span className="flex min-w-0 items-center gap-2"><span className="truncate text-sm font-medium">{item.subject}</span><BroadcastStatus state={item.state} /></span><span className="mt-1 flex items-center gap-2 text-xs text-muted-foreground"><Users className="size-3.5" />{item.audience_count} 人<span className="ml-auto font-mono tabular-nums">{fmtTime(item.updated_at)}</span></span>{Boolean(item.failed_recipient_count || item.failed_delivery_count) && <span className="mt-1 block text-xs text-destructive">失败 {Number(item.failed_recipient_count || 0) + Number(item.failed_delivery_count || 0)} 项</span>}</ListButton>)
    }
    if (folder === 'feedback') {
      if (!bugs.length) return <EmptyState text="当前筛选下暂无反馈" />
      return bugs.map((item) => <ListButton key={item.public_id} selected={selectedId === item.public_id} onClick={() => void openBug(item.public_id)}><span className="flex min-w-0 items-center gap-2"><span className="truncate text-sm font-medium">{item.title}</span><Badge variant={item.status === 'new' ? 'default' : 'secondary'}>{BUG_STATUS_LABELS[item.status] || item.status}</Badge></span><span className="mt-1 flex min-w-0 items-center gap-2 text-xs text-muted-foreground"><span className="truncate">{item.username || '访客'} · {BUG_CATEGORY_LABELS[item.category] || item.category}</span><time className="ml-auto shrink-0 font-mono tabular-nums">{fmtTime(item.updated_at)}</time></span></ListButton>)
    }
    if (!failures.length) return <EmptyState text="暂无失败投递" />
    return failures.map((item) => <ListButton key={item.public_id} selected={selectedId === item.public_id} onClick={() => openFailure(item)}><span className="flex min-w-0 items-center gap-2"><span className="truncate text-sm font-medium">{item.username || '未关联用户'}</span><Badge variant="destructive">{item.channel === 'email' ? '邮件' : '站内信'}</Badge></span><span className="mt-1 block truncate font-mono text-xs text-destructive">{item.last_error || '未知错误'}</span><span className="mt-1 flex text-[0.6875rem] text-muted-foreground"><span>{item.attempt_count}/{item.max_attempts} 次</span><time className="ml-auto font-mono">{fmtTime(item.updated_at)}</time></span></ListButton>)
  }

  return (
    <section className="grid min-h-[36rem] min-w-0 overflow-hidden rounded-xl border bg-card lg:grid-cols-[minmax(15rem,20rem)_minmax(0,1fr)] xl:h-[calc(100dvh-11rem)] xl:grid-cols-[10.5rem_minmax(16rem,21rem)_minmax(0,1fr)]">
      <nav aria-label="通信文件夹" className="border-b p-2 lg:col-span-2 xl:col-span-1 xl:border-r xl:border-b-0">
        <Button type="button" size="sm" className="mb-2 w-full justify-start" onClick={() => { setComposing(true); resetDetail() }}><MailPlus className="size-4" />新建群发</Button>
        <div className="grid grid-cols-2 gap-1 sm:grid-cols-5 xl:grid-cols-1">
          {FOLDERS.map((item) => {
            const Icon = item.icon
            return <Button key={item.key} type="button" variant={!composing && folder === item.key ? 'secondary' : 'ghost'} size="sm" className="min-w-0 justify-start" onClick={() => chooseFolder(item.key)}><Icon className="size-4 shrink-0" /><span className="truncate">{item.label}</span></Button>
          })}
        </div>
        <div className="mt-2 hidden border-t pt-2 text-[0.6875rem] leading-4 text-muted-foreground xl:block">
          通信、群发和反馈共用同一会话真相；邮件只是可追踪投递渠道。
        </div>
      </nav>

      {composing ? (
        <div className="min-w-0 lg:col-span-2 xl:col-span-2">
          <BroadcastCompose onClose={() => setComposing(false)} onCreated={(publicId) => { setComposing(false); setFolder('broadcasts'); setPage(1); void openBroadcast(publicId) }} />
        </div>
      ) : (
        <>
          <div className={cn('min-w-0 border-b lg:border-r lg:border-b-0', selection && 'hidden lg:block')}>
            <header className="flex min-h-12 min-w-0 items-center gap-2 border-b px-3 py-2">
              <activeFolder.icon className="size-4 shrink-0 text-muted-foreground" />
              <h2 className="truncate text-sm font-semibold">{activeFolder.label}</h2>
              <Badge variant="secondary" className="tabular-nums">{total}</Badge>
              {folder === 'feedback' && <Select value={bugFilter} onValueChange={(value) => { setBugFilter(value); setPage(1); resetDetail() }}><SelectTrigger size="sm" className="ml-auto h-8 w-28"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="all">全部状态</SelectItem>{Object.entries(BUG_STATUS_LABELS).map(([value, label]) => <SelectItem key={value} value={value}>{label}</SelectItem>)}</SelectContent></Select>}
              <Button type="button" variant="ghost" size="icon" className={folder === 'feedback' ? '' : 'ml-auto'} aria-label="刷新列表" onClick={() => void loadList()} disabled={listLoading}><RefreshCw className={cn('size-4', listLoading && 'animate-spin')} /></Button>
            </header>
            <div className="max-h-[calc(100dvh-14rem)] min-h-0 overflow-y-auto overscroll-contain" data-scroll-region="admin-mail-list" data-overflow-allowed="y">{!selection && <ErrorMsg msg={error} className="border-b px-3 py-2" />}{renderList()}</div>
            <div className="border-t"><Pagination page={page} perPage={perPage} total={total} onPageChange={(next) => { setPage(next); resetDetail() }} /></div>
          </div>

          <div className="min-h-0 min-w-0">
            {selection && <div className="flex h-12 items-center border-b px-2 lg:hidden"><Button type="button" variant="ghost" size="sm" onClick={resetDetail}><ArrowLeft className="size-4" />返回列表</Button></div>}
            {detailLoading ? <Loading text="正在读取详情…" /> : !selection ? <EmptyState text="从列表选择一项，或新建群发通知" className="h-full min-h-72" /> : (
              <div className="flex h-full min-h-0 min-w-0 flex-col">
                {error && <ErrorMsg msg={error} className="border-b px-3 py-2" />}
                {selection.kind === 'thread' && <ThreadView thread={thread} reply={reply} onReplyChange={setReply} onSend={() => void sendReply()} sending={busy} email={replyEmail} onEmailChange={setReplyEmail} viewerKind="admin" />}

                {selection.kind === 'bug' && bug && <>
                  <header className="border-b px-3 py-3">
                    <div className="flex min-w-0 flex-wrap items-center gap-2"><h3 className="min-w-0 flex-1 break-words text-sm font-semibold">{bug.title}</h3><Badge>{BUG_STATUS_LABELS[bug.status] || bug.status}</Badge></div>
                    <div className="mt-1 flex min-w-0 flex-wrap items-center gap-2 text-xs text-muted-foreground"><span>{bug.reporter_username || bug.username || '访客'}</span><span>{BUG_CATEGORY_LABELS[bug.category] || bug.category}</span><span>{BUG_IMPACT_LABELS[bug.impact] || bug.impact}</span><span className="break-all font-mono">{bug.public_id}</span></div>
                    <div className="mt-3 rounded-lg border bg-muted/20 p-3"><h4 className="mb-2 text-xs font-semibold">安全诊断摘要</h4><DiagnosticSummary bug={bug} /></div>
                    {bug.attachments.length > 0 && <div className="mt-2 flex flex-wrap gap-2">{bug.attachments.map((item) => <Button key={item.public_id} type="button" variant="outline" size="sm" onClick={() => void downloadAttachment(item.public_id, item.original_name)}><Paperclip className="size-3.5" /><span className="max-w-48 truncate">{item.original_name}</span></Button>)}</div>}
                    {BUG_TRANSITIONS[bug.status]?.length > 0 && <div className="mt-3 grid min-w-0 gap-2 border-t pt-3 sm:grid-cols-[10rem_minmax(10rem,1fr)_auto]"><Select value={statusDraft} onValueChange={setStatusDraft}><SelectTrigger size="sm"><SelectValue placeholder="下一状态" /></SelectTrigger><SelectContent>{BUG_TRANSITIONS[bug.status].map((value) => <SelectItem key={value} value={value}>{BUG_STATUS_LABELS[value] || value}</SelectItem>)}</SelectContent></Select><Input value={statusNote} maxLength={2000} onChange={(event) => setStatusNote(event.target.value)} placeholder="处理说明（可选）" /><Button type="button" size="sm" disabled={busy || !statusDraft || (statusDraft === 'duplicate' && !duplicateOf.trim())} onClick={() => void updateBugStatus()}>更新状态</Button>{statusDraft === 'duplicate' && <Input className="sm:col-start-2" value={duplicateOf} onChange={(event) => setDuplicateOf(event.target.value)} placeholder="重复反馈的公开编号 bug_…" />}</div>}
                  </header>
                  <ThreadView thread={thread} reply={reply} onReplyChange={setReply} onSend={() => void sendReply()} sending={busy} email={replyEmail} onEmailChange={setReplyEmail} viewerKind="admin" />
                </>}

                {selection.kind === 'broadcast' && broadcast && <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain p-3" data-scroll-region="broadcast-detail" data-overflow-allowed="y">
                  <div className="flex min-w-0 flex-wrap items-start gap-2"><div className="min-w-0 flex-1"><h3 className="break-words text-sm font-semibold">{broadcast.subject}</h3><p className="mt-1 text-xs text-muted-foreground">{AUDIENCE_KIND_LABELS[broadcast.audience_kind] || broadcast.audience_kind} · {broadcast.audience_count} 人 · {broadcast.channels.map((item) => item === 'in_app' ? '站内信' : '邮件').join(' + ')}</p></div><BroadcastStatus state={broadcast.state} /></div>
                  <div className="mt-3 whitespace-pre-wrap break-words rounded-lg border bg-muted/20 p-3 text-sm leading-relaxed">{broadcast.body_text}</div>
                  <div className="mt-3 grid gap-3 xl:grid-cols-2"><section><h4 className="mb-2 text-xs font-semibold">受众处理</h4><CountGrid values={broadcast.recipients} /></section><section><h4 className="mb-2 text-xs font-semibold">投递状态</h4><CountGrid values={broadcast.deliveries} /></section></div>
                  <div className="mt-3 flex flex-wrap items-center gap-2 text-xs text-muted-foreground"><CalendarClock className="size-4" /><span>计划 {broadcast.scheduled_at ? fmtTime(broadcast.scheduled_at) : '未批准'}</span><span>更新 {fmtTime(broadcast.updated_at)}</span><span className="break-all font-mono">{broadcast.public_id}</span></div>
                  {(broadcast.failed_recipients.length > 0 || broadcast.failed_deliveries.length > 0) && <section className="mt-3 rounded-xl border border-destructive/25"><header className="flex flex-wrap items-center gap-2 border-b px-3 py-2"><AlertTriangle className="size-4 text-destructive" /><h4 className="text-sm font-semibold">失败项</h4><Badge variant="destructive">{broadcast.failed_recipients.length + broadcast.failed_deliveries.length}</Badge>{broadcast.state !== 'cancelled' && <Button type="button" size="sm" variant="outline" className="ml-auto" disabled={busy} onClick={() => void retryBroadcast()}><RotateCcw className="size-4" />重试失败项</Button>}</header><div className="divide-y">{[...broadcast.failed_recipients.map((item) => ({ key: item.public_id, user: item.username, error: item.last_error, attempts: `${item.attempt_count}/${item.max_attempts}`, channel: '受众处理' })), ...broadcast.failed_deliveries.map((item) => ({ key: item.public_id, user: item.username, error: item.last_error, attempts: `${item.attempt_count}/${item.max_attempts}`, channel: item.channel === 'email' ? '邮件' : '站内信' }))].map((item) => <div key={item.key} className="grid min-w-0 gap-1 px-3 py-2 text-xs sm:grid-cols-[minmax(7rem,1fr)_5rem_4rem_minmax(8rem,1fr)]"><span className="truncate">{item.user || '未关联用户'}</span><span className="text-muted-foreground">{item.channel}</span><span className="font-mono tabular-nums">{item.attempts}</span><span className="break-words font-mono text-destructive">{item.error || '未知错误'}</span></div>)}</div></section>}
                  {['draft', 'scheduled', 'running'].includes(broadcast.state) && <div className="mt-3 flex justify-end"><Button type="button" variant="destructive" size="sm" disabled={busy} onClick={() => void cancelBroadcast()}><XCircle className="size-4" />取消未完成投递</Button></div>}
                </div>}

                {selection.kind === 'delivery' && failure && <div className="p-4"><div className="flex items-center gap-2"><ShieldAlert className="size-5 text-destructive" /><h3 className="text-sm font-semibold">失败投递</h3><Badge variant="destructive">{failure.channel === 'email' ? '邮件' : '站内信'}</Badge></div><dl className="mt-4 grid gap-3 text-sm sm:grid-cols-2"><div><dt className="text-xs text-muted-foreground">用户</dt><dd>{failure.username || '未关联用户'}</dd></div><div><dt className="text-xs text-muted-foreground">尝试</dt><dd className="font-mono">{failure.attempt_count}/{failure.max_attempts}</dd></div><div className="sm:col-span-2"><dt className="text-xs text-muted-foreground">脱敏错误码</dt><dd className="mt-1 break-words rounded-lg border bg-muted/20 p-2 font-mono text-xs text-destructive">{failure.last_error || '未知错误'}</dd></div></dl>{failure.broadcast_public_id ? <Button type="button" className="mt-4" size="sm" onClick={() => void openBroadcast(failure.broadcast_public_id || '')}><Megaphone className="size-4" />打开群发并重试</Button> : <p className="mt-4 rounded-lg border bg-muted/20 p-3 text-xs leading-relaxed text-muted-foreground">该项不属于群发通知。为避免重复发送验证码或事务邮件，请先修复 SMTP 或查看日志，不在此手动重置。</p>}</div>}
              </div>
            )}
          </div>
        </>
      )}
      {confirmDialog}
    </section>
  )
}
