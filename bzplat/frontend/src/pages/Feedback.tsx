import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { Bug, CheckCircle2, Inbox, Paperclip, Send } from 'lucide-react'
import { useTheme } from 'next-themes'
import { toast } from 'sonner'

import { apiFetch, apiGet, apiJson, apiUpload, errMsg, lastSafeApiFailure, userToken, type ApiRequestInit, type CurrentUser } from '@/api'
import CaptchaField, { type CaptchaValue } from '@/components/CaptchaField'
import type { BugDetail, BugSummary, ThreadDetail } from '@/components/communications/types'
import { BUG_CATEGORY_LABELS, BUG_IMPACT_LABELS, BUG_STATUS_LABELS } from '@/components/communications/types'
import { ThreadView } from '@/components/communications/thread-view'
import { DataRegion, PageFrame, PageHeader } from '@/components/layout'
import { useAuth } from '@/components/useAuth'
import { Badge } from '@/components/ui/badge'
import { Button, buttonVariants } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { EmptyState, ErrorMsg, Loading } from '@/components/ui/status'
import { Textarea } from '@/components/ui/textarea'
import { fmtTime } from '@/lib/format'

const TRACKS_KEY = 'botbattle_feedback_tracking_v1'

interface GuestTrack { public_id: string; tracking_token: string; created_at: string }
interface LoadedReport { bug_report: BugDetail; thread: ThreadDetail }
interface IdentityOperation {
  controller: AbortController
  epoch: number
  userId: number | null
  authToken: string | null
}

function readTracks(): GuestTrack[] {
  try {
    const value = JSON.parse(localStorage.getItem(TRACKS_KEY) || '[]') as GuestTrack[]
    return Array.isArray(value) ? value.filter((item) => item.public_id && item.tracking_token).slice(0, 10) : []
  } catch { return [] }
}

function saveTrack(track: GuestTrack) {
  const next = [track, ...readTracks().filter((item) => item.public_id !== track.public_id)].slice(0, 10)
  localStorage.setItem(TRACKS_KEY, JSON.stringify(next))
}

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${Math.ceil(bytes / 1024)} KiB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`
}

function coarseBrowser(): 'chrome' | 'firefox' | 'safari' | 'edge' | 'other' | 'unknown' {
  const value = navigator.userAgent.toLowerCase()
  if (value.includes('edg/')) return 'edge'
  if (value.includes('firefox/')) return 'firefox'
  if (value.includes('chrome/') || value.includes('chromium/')) return 'chrome'
  if (value.includes('safari/')) return 'safari'
  return value ? 'other' : 'unknown'
}

function coarseOs(): 'windows' | 'macos' | 'linux' | 'android' | 'ios' | 'other' | 'unknown' {
  const value = navigator.userAgent.toLowerCase()
  if (value.includes('android')) return 'android'
  if (/iphone|ipad|ipod/.test(value)) return 'ios'
  if (value.includes('windows')) return 'windows'
  if (value.includes('mac os')) return 'macos'
  if (value.includes('linux')) return 'linux'
  return value ? 'other' : 'unknown'
}

async function guestFetch<T>(path: string, token: string, method: 'GET' | 'POST' = 'GET', body?: unknown, signal?: AbortSignal): Promise<T> {
  return apiFetch<T>(path, {
    method,
    headers: { 'X-Feedback-Token': token },
    body: body === undefined ? undefined : body as BodyInit,
    cache: 'no-store',
    referrerPolicy: 'no-referrer',
    signal,
    suppressAuth: true,
    credentials: 'omit',
  })
}

function frozenAuthRequestOptions(
  signal: AbortSignal,
): Omit<ApiRequestInit, 'method' | 'body'> {
  const authToken = userToken.get()
  return {
    signal,
    suppressAuth: true,
    credentials: authToken ? 'omit' : 'include',
    headers: authToken ? { Authorization: `Bearer ${authToken}` } : undefined,
    cache: 'no-store',
    referrerPolicy: 'no-referrer',
  }
}

export default function Feedback() {
  const { user } = useAuth()
  return <FeedbackForIdentity key={user?.id ?? 'guest'} user={user} />
}

function FeedbackForIdentity({ user }: { user: CurrentUser | null }) {
  const userId = user?.id ?? null
  const { theme = 'system' } = useTheme()
  const [category, setCategory] = useState('page')
  const [impact, setImpact] = useState('minor')
  const [title, setTitle] = useState('')
  const [body, setBody] = useState('')
  const [files, setFiles] = useState<File[]>([])
  const [captcha, setCaptcha] = useState<CaptchaValue>({ captcha_id: '', captcha_answer: '' })
  const [reports, setReports] = useState<BugSummary[]>([])
  const [tracks, setTracks] = useState<GuestTrack[]>(readTracks)
  const [selected, setSelected] = useState('')
  const [loaded, setLoaded] = useState<LoadedReport | null>(null)
  const [reply, setReply] = useState('')
  const [loading, setLoading] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [sending, setSending] = useState(false)
  const [error, setError] = useState('')
  const detailRequestRef = useRef<{ seq: number; controller: AbortController | null }>({ seq: 0, controller: null })
  const listRequestRef = useRef<{ seq: number; controller: AbortController | null }>({ seq: 0, controller: null })
  const identityEpochRef = useRef<{ epoch: number; userId: number | null }>({ epoch: 0, userId })
  const loadedIdentityEpochRef = useRef<number | null>(null)
  const operationControllersRef = useRef<Set<AbortController>>(new Set())
  if (identityEpochRef.current.userId !== userId) {
    identityEpochRef.current = {
      epoch: identityEpochRef.current.epoch + 1,
      userId,
    }
  }

  const beginIdentityOperation = (): IdentityOperation => {
    const controller = new AbortController()
    operationControllersRef.current.add(controller)
    return {
      controller,
      epoch: identityEpochRef.current.epoch,
      userId: identityEpochRef.current.userId,
      authToken: userToken.get(),
    }
  }

  const operationIsCurrent = (operation: IdentityOperation) => (
    !operation.controller.signal.aborted &&
    identityEpochRef.current.epoch === operation.epoch &&
    identityEpochRef.current.userId === operation.userId
  )

  const finishIdentityOperation = (operation: IdentityOperation) => {
    operationControllersRef.current.delete(operation.controller)
  }

  const abortIdentityOperations = () => {
    for (const controller of operationControllersRef.current) controller.abort()
    operationControllersRef.current.clear()
  }

  const identityRequestOptions = (
    operation: IdentityOperation,
  ): Omit<ApiRequestInit, 'method' | 'body'> => {
    if (operation.userId === null) {
      return {
        signal: operation.controller.signal,
        suppressAuth: true,
        credentials: 'omit',
      }
    }
    if (operation.authToken) {
      return {
        signal: operation.controller.signal,
        suppressAuth: true,
        credentials: 'omit',
        headers: { Authorization: `Bearer ${operation.authToken}` },
      }
    }
    return {
      signal: operation.controller.signal,
      suppressAuth: true,
      credentials: 'include',
    }
  }

  const selectedTrack = useMemo(() => tracks.find((item) => item.public_id === selected), [selected, tracks])

  const loadList = useCallback(async (expectedEpoch = identityEpochRef.current.epoch) => {
    if (identityEpochRef.current.epoch !== expectedEpoch) return
    listRequestRef.current.controller?.abort()
    const controller = new AbortController()
    const seq = listRequestRef.current.seq + 1
    const identity = { ...identityEpochRef.current }
    const requestOptions = frozenAuthRequestOptions(controller.signal)
    listRequestRef.current = { seq, controller }
    const isCurrent = () => (
      listRequestRef.current.seq === seq &&
      listRequestRef.current.controller === controller &&
      identityEpochRef.current.epoch === identity.epoch &&
      identityEpochRef.current.userId === identity.userId &&
      !controller.signal.aborted
    )
    setLoading(true)
    setError('')
    try {
      if (identity.userId === null) {
        setReports([])
        setTracks(readTracks())
        return
      }
      const result = await apiGet<{ bug_reports: BugSummary[] }>(
        '/api/feedback/bugs?per_page=100',
        requestOptions,
      )
      if (!isCurrent()) return
      setReports(result.bug_reports || [])
    } catch (cause) {
      if (isCurrent()) setError(errMsg(cause, '反馈记录加载失败'))
    } finally {
      if (isCurrent()) setLoading(false)
    }
  }, [userId])

  useEffect(() => {
    abortIdentityOperations()
    detailRequestRef.current.controller?.abort()
    detailRequestRef.current = {
      seq: detailRequestRef.current.seq + 1,
      controller: null,
    }
    listRequestRef.current.controller?.abort()
    setSelected('')
    loadedIdentityEpochRef.current = null
    setLoaded(null)
    setReports([])
    setReply('')
    setLoading(false)
    setSubmitting(false)
    setUploading(false)
    setSending(false)
    setError('')
    void loadList(identityEpochRef.current.epoch)
  }, [userId, loadList])

  useEffect(() => () => {
    abortIdentityOperations()
    detailRequestRef.current.controller?.abort()
    listRequestRef.current.controller?.abort()
  }, [])

  const selectReport = async (
    publicId: string,
    expectedEpoch = identityEpochRef.current.epoch,
  ) => {
    if (identityEpochRef.current.epoch !== expectedEpoch) return
    detailRequestRef.current.controller?.abort()
    const controller = new AbortController()
    const seq = detailRequestRef.current.seq + 1
    const identity = { ...identityEpochRef.current }
    const requestOptions = frozenAuthRequestOptions(controller.signal)
    detailRequestRef.current = { seq, controller }
    const isCurrent = () => (
      detailRequestRef.current.seq === seq &&
      detailRequestRef.current.controller === controller &&
      identityEpochRef.current.epoch === identity.epoch &&
      identityEpochRef.current.userId === identity.userId &&
      !controller.signal.aborted
    )
    setSelected(publicId)
    setLoading(true)
    setError('')
    try {
      if (identity.userId !== null) {
        const result = await apiGet<{ bug_report: BugDetail }>(
          `/api/feedback/bugs/${encodeURIComponent(publicId)}`,
          requestOptions,
        )
        if (!isCurrent()) return
        if (result.bug_report.public_id !== publicId) throw new Error('反馈详情标识不一致')
        const thread = await apiGet<ThreadDetail>(
          `/api/communications/threads/${encodeURIComponent(result.bug_report.conversation_public_id)}`,
          requestOptions,
        )
        if (!isCurrent()) return
        if (thread.conversation.public_id !== result.bug_report.conversation_public_id) throw new Error('反馈会话标识不一致')
        loadedIdentityEpochRef.current = identity.epoch
        setLoaded({ bug_report: result.bug_report, thread })
      } else {
        const track = readTracks().find((item) => item.public_id === publicId)
        if (!track) throw new Error('本机没有该反馈的追踪凭据')
        const result = await guestFetch<LoadedReport>(`/api/feedback/bugs/${encodeURIComponent(publicId)}/track`, track.tracking_token, 'GET', undefined, controller.signal)
        if (!isCurrent()) return
        if (result.bug_report.public_id !== publicId) throw new Error('反馈详情标识不一致')
        if (result.thread.conversation.public_id !== result.bug_report.conversation_public_id) throw new Error('反馈会话标识不一致')
        loadedIdentityEpochRef.current = identity.epoch
        setLoaded(result)
      }
    } catch (cause) {
      if (!isCurrent()) return
      loadedIdentityEpochRef.current = null
      setLoaded(null)
      setError(errMsg(cause, '反馈详情读取失败'))
    } finally { if (isCurrent()) setLoading(false) }
  }

  const submit = async () => {
    if (!title.trim()) { setError('请用一句话说明看到的问题'); return }
    const operation = beginIdentityOperation()
    const startedAsGuest = operation.userId === null
    const attachmentFiles = [...files]
    setSubmitting(true)
    setError('')
    try {
      const failure = lastSafeApiFailure()
      const result = await apiJson<{ bug_report: { public_id: string; tracking_token?: string; created_at: string } }>(
        '/api/feedback/bugs',
        'POST',
        {
          category,
          impact,
          title: title.trim(),
          // 小白界面只要求一句话；用户未补充时以同一现象文本满足后端消息真相契约。
          body: body.trim() || title.trim(),
          current_route: location.hash.replace(/^#/, '').split(/[?#]/, 1)[0] || '/',
          diagnostics: {
            browser_family: coarseBrowser(),
            os_family: coarseOs(),
            viewport_width: window.innerWidth,
            viewport_height: window.innerHeight,
            locale: navigator.language || '',
            timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || '',
            theme: ['light', 'dark', 'system'].includes(theme) ? theme : 'unknown',
            failed_api_template: failure?.template || null,
            failed_api_status: failure?.status || null,
            trace_id: failure?.trace_id || '',
          },
          ...(startedAsGuest ? captcha : {}),
        },
        identityRequestOptions(operation),
      )
      if (!operationIsCurrent(operation)) return
      const created = result.bug_report
      if (startedAsGuest && created.tracking_token) {
        saveTrack({ public_id: created.public_id, tracking_token: created.tracking_token, created_at: created.created_at })
        setTracks(readTracks())
      }
      let failedUploads = 0
      for (const file of attachmentFiles) {
        if (!operationIsCurrent(operation)) return
        try {
          await apiUpload(
            `/api/feedback/bugs/${encodeURIComponent(created.public_id)}/attachments`,
            file,
            created.tracking_token ? { tracking_token: created.tracking_token } : {},
            'POST',
            identityRequestOptions(operation),
          )
        } catch {
          if (!operationIsCurrent(operation)) return
          failedUploads += 1
        }
      }
      if (!operationIsCurrent(operation)) return
      setTitle(''); setBody(''); setFiles([])
      await loadList(operation.epoch)
      if (!operationIsCurrent(operation)) return
      await selectReport(created.public_id, operation.epoch)
      if (!operationIsCurrent(operation)) return
      if (failedUploads) setError(`问题已提交，但 ${failedUploads} 张截图上传失败；可在右侧详情重试`)
      else toast.success('问题已提交，可在此继续追踪')
    } catch (cause) {
      if (operationIsCurrent(operation)) setError(errMsg(cause, '提交失败，请按提示检查后重试'))
    } finally {
      const current = operationIsCurrent(operation)
      finishIdentityOperation(operation)
      if (current) setSubmitting(false)
    }
  }

  const sendReply = async () => {
    if (
      !loaded ||
      selected !== loaded.bug_report.public_id ||
      loadedIdentityEpochRef.current !== identityEpochRef.current.epoch ||
      !reply.trim()
    ) return
    const operation = beginIdentityOperation()
    const report = loaded
    const track = selectedTrack
    const replyBody = reply.trim()
    if (operation.userId === null && !track) {
      finishIdentityOperation(operation)
      return
    }
    setSending(true)
    setError('')
    try {
      const bugId = report.bug_report.public_id
      if (operation.userId !== null) {
        await apiJson(
          `/api/communications/threads/${encodeURIComponent(report.thread.conversation.public_id)}/reply`,
          'POST',
          { body: replyBody },
          identityRequestOptions(operation),
        )
      } else if (track) {
        await guestFetch(
          `/api/feedback/bugs/${encodeURIComponent(bugId)}/track/reply`,
          track.tracking_token,
          'POST',
          { body: replyBody },
          operation.controller.signal,
        )
      }
      if (!operationIsCurrent(operation)) return
      setReply('')
      await selectReport(bugId, operation.epoch)
      if (!operationIsCurrent(operation)) return
      toast.success('补充信息已发送')
    } catch (cause) {
      if (operationIsCurrent(operation)) setError(errMsg(cause, '回复失败'))
    } finally {
      const current = operationIsCurrent(operation)
      finishIdentityOperation(operation)
      if (current) setSending(false)
    }
  }

  const downloadAttachment = async (attachmentId: string, name: string) => {
    if (
      !loaded ||
      selected !== loaded.bug_report.public_id ||
      loadedIdentityEpochRef.current !== identityEpochRef.current.epoch
    ) return
    const operation = beginIdentityOperation()
    const reportPublicId = loaded.bug_report.public_id
    const track = selectedTrack
    if (operation.userId === null && !track) {
      finishIdentityOperation(operation)
      return
    }
    try {
      const headers = new Headers()
      if (operation.authToken) headers.set('Authorization', `Bearer ${operation.authToken}`)
      if (operation.userId === null && track) headers.set('X-Feedback-Token', track.tracking_token)
      const response = await fetch(`/api/feedback/bugs/${encodeURIComponent(reportPublicId)}/attachments/${encodeURIComponent(attachmentId)}`, {
        headers,
        credentials: operation.userId === null || operation.authToken ? 'omit' : 'include',
        cache: 'no-store',
        referrerPolicy: 'no-referrer',
        signal: operation.controller.signal,
      })
      if (!operationIsCurrent(operation)) return
      if (!response.ok) throw new Error('附件读取失败')
      const blob = await response.blob()
      if (!operationIsCurrent(operation)) return
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url; anchor.download = name; anchor.click()
      setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch (cause) {
      if (operationIsCurrent(operation)) setError(errMsg(cause, '附件读取失败'))
    } finally {
      finishIdentityOperation(operation)
    }
  }

  const addAttachments = async (nextFiles: File[]) => {
    if (
      !loaded ||
      selected !== loaded.bug_report.public_id ||
      loadedIdentityEpochRef.current !== identityEpochRef.current.epoch ||
      !nextFiles.length
    ) return
    const operation = beginIdentityOperation()
    const reportPublicId = loaded.bug_report.public_id
    const track = selectedTrack
    if (operation.userId === null && !track) {
      finishIdentityOperation(operation)
      return
    }
    setUploading(true); setError('')
    try {
      const fields: Record<string, string> = operation.userId === null && track
        ? { tracking_token: track.tracking_token }
        : {}
      let completed = 0
      let failed = 0
      for (const file of nextFiles.slice(0, 5)) {
        if (!operationIsCurrent(operation)) return
        try {
          await apiUpload(
            `/api/feedback/bugs/${encodeURIComponent(reportPublicId)}/attachments`,
            file,
            fields,
            'POST',
            identityRequestOptions(operation),
          )
          completed += 1
        } catch {
          if (!operationIsCurrent(operation)) return
          failed += 1
        }
      }
      if (!operationIsCurrent(operation)) return
      await selectReport(reportPublicId, operation.epoch)
      if (!operationIsCurrent(operation)) return
      if (failed) setError(`已补充 ${completed} 张，${failed} 张上传失败`)
      else toast.success(`已补充 ${completed} 张截图`)
    } catch (cause) {
      if (operationIsCurrent(operation)) setError(errMsg(cause, '截图上传失败'))
    } finally {
      const current = operationIsCurrent(operation)
      finishIdentityOperation(operation)
      if (current) setUploading(false)
    }
  }

  const visibleReports: BugSummary[] = user ? reports : tracks.map((item) => ({
    public_id: item.public_id, conversation_public_id: '', category: 'other', impact: 'minor',
    title: '访客反馈', current_route: '', status: '', created_at: item.created_at, updated_at: item.created_at,
  }))

  return (
    <PageFrame width="wide" layout="feedback-center">
      <PageHeader
        title="问题反馈"
        description="选择问题类型和影响程度即可；平台会自动附带不含隐私的设备与失败接口摘要。"
        actions={user ? <Button asChild variant="outline" size="sm"><Link to="/messages"><Inbox className="size-4" />查看站内信</Link></Button> : undefined}
      />
      {error && <ErrorMsg msg={error} />}
      <div className="grid min-w-0 gap-3 xl:grid-cols-[minmax(20rem,30rem)_minmax(0,1fr)]">
        <DataRegion title="提交新问题" description="请不要填写密码、验证码、访问令牌或实名信息" contentClassName="space-y-3 p-4">
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1.5"><Label>哪里有问题</Label><Select value={category} onValueChange={setCategory}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent>{Object.entries(BUG_CATEGORY_LABELS).map(([value, label]) => <SelectItem key={value} value={value}>{label}</SelectItem>)}</SelectContent></Select></div>
            <div className="space-y-1.5"><Label>影响程度</Label><Select value={impact} onValueChange={setImpact}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent>{Object.entries(BUG_IMPACT_LABELS).map(([value, label]) => <SelectItem key={value} value={value}>{label}</SelectItem>)}</SelectContent></Select></div>
          </div>
          <div className="space-y-1.5"><Label htmlFor="feedback-title">一句话说明</Label><Input id="feedback-title" value={title} onChange={(event) => setTitle(event.target.value)} maxLength={160} placeholder="例如：对局回放点下一步没有变化" /></div>
          <div className="space-y-1.5"><Label htmlFor="feedback-body">补充说明（可选）</Label><Textarea id="feedback-body" value={body} onChange={(event) => setBody(event.target.value)} maxLength={20_000} rows={4} placeholder="如果方便，补充点了什么、期望看到什么；不需要写技术原因" /></div>
          <div className="space-y-1.5">
            <Label htmlFor="feedback-files">截图（可选）</Label>
            <input id="feedback-files" className="peer sr-only" type="file" accept="image/png,image/jpeg,image/webp,image/gif" multiple onChange={(event) => setFiles(Array.from(event.target.files || []).slice(0, 5))} />
            <Label
              htmlFor="feedback-files"
              className={buttonVariants({ variant: 'outline', size: 'sm', className: 'min-h-11 w-full cursor-pointer peer-focus-visible:ring-[3px] peer-focus-visible:ring-ring/50' })}
            >
              <Paperclip className="size-4" />选择截图（最多 5 张）
            </Label>
            {files.length ? (
              <ul data-testid="feedback-file-summary" className="space-y-1 rounded-md bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
                {files.map((file, index) => (
                  <li key={`${file.name}-${file.size}-${index}`} className="flex min-w-0 items-center gap-2">
                    <span className="min-w-0 flex-1 truncate text-foreground">{file.name}</span>
                    <span className="shrink-0 font-mono">{formatFileSize(file.size)}</span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-xs text-muted-foreground">不用整理日志；平台只打包安全诊断摘要</p>
            )}
          </div>
          {!user && <CaptchaField onChange={setCaptcha} />}
          <Button className="w-full" disabled={submitting || !title.trim()} aria-busy={submitting} onClick={() => void submit()}><Send className="size-4" />{submitting ? '正在提交…' : '提交并开始追踪'}</Button>
        </DataRegion>

        <section className="grid min-h-[32rem] min-w-0 overflow-hidden rounded-xl border bg-card lg:grid-cols-[minmax(14rem,20rem)_minmax(0,1fr)]">
          <div className="min-w-0 border-b lg:border-r lg:border-b-0">
            <header className="border-b px-3 py-2.5"><h2 className="text-sm font-semibold">{user ? '我的反馈' : '本机访客反馈'}</h2><p className="mt-0.5 text-xs text-muted-foreground">{user ? '登录账号下的提交记录' : '追踪凭据仅保存在这台设备'}</p></header>
            {loading ? <Loading /> : visibleReports.length === 0 ? <EmptyState text="暂无反馈记录" icon={<Bug className="size-5 opacity-50" />} className="py-10" /> : <ul className="divide-y">{visibleReports.map((item) => <li key={item.public_id}><button type="button" onClick={() => void selectReport(item.public_id)} className="w-full min-w-0 cursor-pointer px-3 py-2.5 text-left hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"><span className="flex items-center gap-2"><span className="truncate text-sm font-medium">{item.title}</span>{item.status && <Badge variant="secondary" className="ml-auto shrink-0">{BUG_STATUS_LABELS[item.status] || item.status}</Badge>}</span><span className="mt-1 block font-mono text-xs tabular-nums text-muted-foreground">{fmtTime(item.updated_at)}</span></button></li>)}</ul>}
          </div>
          <div className="min-h-0 min-w-0">
            {!loaded ? <EmptyState text="选择一条反馈查看处理进度" className="py-16" /> : <div className="flex min-h-0 flex-col">
              <div className="border-b px-4 py-3">
                <div className="flex flex-wrap items-center gap-2"><h3 className="min-w-0 flex-1 break-words text-sm font-semibold">{loaded.bug_report.title}</h3><Badge>{BUG_STATUS_LABELS[loaded.bug_report.status] || loaded.bug_report.status}</Badge></div>
                <div className="mt-2 flex flex-wrap gap-2 text-xs text-muted-foreground"><span>{BUG_CATEGORY_LABELS[loaded.bug_report.category]}</span><span>{BUG_IMPACT_LABELS[loaded.bug_report.impact]}</span><span className="font-mono">{loaded.bug_report.public_id}</span></div>
                <div className="mt-2 flex min-w-0 flex-wrap items-center gap-2">
                  {loaded.bug_report.attachments.map((attachment) => <Button key={attachment.public_id} type="button" variant="outline" size="sm" onClick={() => void downloadAttachment(attachment.public_id, attachment.original_name)}><Paperclip className="size-3.5" /><span className="max-w-44 truncate">{attachment.original_name}</span></Button>)}
                  {loaded.bug_report.attachments.length < 5 && <div className="min-w-48 flex-1"><input id="feedback-more-files" className="peer sr-only" type="file" accept="image/png,image/jpeg,image/webp,image/gif" multiple disabled={uploading} aria-label="补充截图" onChange={(event) => { const selectedFiles = Array.from(event.target.files || []); event.target.value = ''; void addAttachments(selectedFiles) }} /><Label htmlFor="feedback-more-files" aria-disabled={uploading} className={buttonVariants({ variant: 'outline', size: 'sm', className: 'min-h-11 w-full cursor-pointer peer-disabled:pointer-events-none peer-disabled:opacity-50 peer-focus-visible:ring-[3px] peer-focus-visible:ring-ring/50' })}><Paperclip className="size-4" />{uploading ? '正在上传…' : '选择补充截图'}</Label></div>}
                  {uploading && <span className="text-xs text-muted-foreground">正在上传…</span>}
                </div>
                <div className="mt-2 flex flex-wrap gap-2">{loaded.bug_report.events.filter((event) => event.event_type === 'status_changed').map((event) => <span key={event.public_id} className="inline-flex items-center gap-1 text-xs text-muted-foreground"><CheckCircle2 className="size-3.5" />{BUG_STATUS_LABELS[event.to_status] || event.to_status}{event.note ? `：${event.note}` : ''}</span>)}</div>
              </div>
              <ThreadView thread={loaded.thread} reply={reply} onReplyChange={setReply} onSend={() => void sendReply()} sending={sending} />
            </div>}
          </div>
        </section>
      </div>
    </PageFrame>
  )
}
