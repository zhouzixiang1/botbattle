import type { ReactNode } from 'react'
import {
  Activity,
  Bot,
  Clock3,
  Cpu,
  ListOrdered,
  PauseCircle,
  PlayCircle,
  RotateCcw,
  Square,
} from 'lucide-react'
import { Link } from 'react-router-dom'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { ErrorMsg, Loading } from '@/components/ui/status'
import { fmtTime } from '@/lib/format'
import { cn } from '@/lib/utils'

export type ExecutionSource = 'manual' | 'human' | 'contest' | 'auto'
export type ExecutionStatus =
  | 'queued'
  | 'starting'
  | 'running'
  | 'settling'
  | 'completed'
  | 'cancelled'
  | 'interrupted'

export interface ExecutionQueueJob {
  public_id: string
  request_id: string
  source: ExecutionSource
  status: ExecutionStatus
  game_id: string
  match_type: string
  match_id?: string | null
  sandbox_units: number
  rated: boolean
  rating_reason: string
  retryable: boolean
  cancel_requested: boolean
  reason: string
  created_at?: string | null
  started_at?: string | null
  terminal_at?: string | null
}

export interface ExecutionCapacity {
  match_slots: { used: number; capacity: number }
  sandbox_units: { used: number; capacity: number }
  running_matches: number
}

export interface ExecutionQueueSnapshot {
  dispatcher: {
    state: string
    accepting: boolean
    auto_enabled: boolean
    pause_reason: string
    retry_at?: string | null
  }
  capacity: ExecutionCapacity
  active: ExecutionQueueJob[]
  queued: ExecutionQueueJob[]
  queued_count: number
}

export interface ExecutionRequestSnapshot {
  public_id: string
  request: ExecutionQueueJob
  ahead_jobs: number
  ahead_sandbox_units: number
  capacity: ExecutionCapacity
  eta: {
    min_seconds: number
    max_seconds: number
    dynamic: true
    note: string
  }
}

const SOURCE_LABEL: Record<ExecutionSource, string> = {
  manual: '用户挑战',
  human: '真人对战',
  contest: '锦标赛',
  auto: '自动排位',
}

const STATUS_LABEL: Record<ExecutionStatus, string> = {
  queued: '排队中',
  starting: '启动中',
  running: '运行中',
  settling: '收尾中',
  completed: '已完成',
  cancelled: '已取消',
  interrupted: '已中断',
}

const GAME_LABEL: Record<string, string> = {
  holdem: '德州扑克',
  gomoku: '五子棋',
  pencil: '点格棋',
}

const RATING_REASON_LABEL: Record<string, string> = {
  contest: '赛事对局不计评分',
  human: '人机对战不计评分',
  self_play: '自博弈不计评分',
  bot_missing: 'Bot 信息不完整，不计评分',
  same_owner: '同一所有者，不计评分',
  eligible: '计入评分',
}

function dispatcherLabel(state: string, accepting: boolean): string {
  if (state === 'paused') return '安全暂停'
  if (state === 'starting') return '正在启动'
  if (state === 'stopping' || state === 'draining') return '正在停止'
  if (state === 'stopped') return '已停止'
  return accepting ? '接收任务' : '暂不接收'
}

function CapacityMeter({ capacity }: { capacity: ExecutionCapacity }) {
  const matchSlots = capacity.match_slots || { used: 0, capacity: 0 }
  const sandboxUnits = capacity.sandbox_units || { used: 0, capacity: 0 }
  return (
    <dl className="grid grid-cols-2 gap-2 text-xs">
      <div className="rounded-md border border-border bg-muted/20 px-2.5 py-2">
        <dt className="inline-flex items-center gap-1 text-muted-foreground">
          <Activity className="size-3.5" /> 对局槽
        </dt>
        <dd className="mt-0.5 font-mono font-semibold tabular-nums">
          {matchSlots.used} / {matchSlots.capacity}
        </dd>
      </div>
      <div className="rounded-md border border-border bg-muted/20 px-2.5 py-2">
        <dt className="inline-flex items-center gap-1 text-muted-foreground">
          <Cpu className="size-3.5" /> 沙箱单位
        </dt>
        <dd className="mt-0.5 font-mono font-semibold tabular-nums">
          {sandboxUnits.used} / {sandboxUnits.capacity}
        </dd>
      </div>
    </dl>
  )
}

function JobRow({ job, position }: { job: ExecutionQueueJob; position?: number }) {
  const active = job.status === 'starting' || job.status === 'running' || job.status === 'settling'
  return (
    <li className="min-w-0 rounded-lg border border-border bg-muted/20 px-3 py-2.5">
      <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
        <div className="flex flex-wrap items-center gap-1.5">
          {position != null && (
            <span className="inline-flex items-center gap-1 font-mono font-semibold">
              <ListOrdered className="size-3" /> #{position}
            </span>
          )}
          <Badge variant={active ? 'default' : 'secondary'}>{SOURCE_LABEL[job.source] || job.source}</Badge>
          <span className="text-muted-foreground">{GAME_LABEL[job.game_id] || job.game_id}</span>
          <span className="text-muted-foreground">{STATUS_LABEL[job.status] || job.status}</span>
        </div>
        {job.match_id && (
          <Link
            to={`/match/${encodeURIComponent(job.match_id)}`}
            className="inline-flex min-h-11 items-center font-medium text-primary hover:underline"
          >
            进入观赛
          </Link>
        )}
      </div>
      <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted-foreground">
        <span>{job.sandbox_units} 个沙箱单位</span>
        <span>{job.rated ? '计入评分' : RATING_REASON_LABEL[job.rating_reason] || '不计评分'}</span>
        {job.cancel_requested && <span>正在安全取消</span>}
      </div>
    </li>
  )
}

export function ExecutionQueuePanel({
  snapshot,
  loading = false,
  error = '',
  action,
  maxQueued,
  className,
  onRetry,
  stale = false,
  lastUpdatedAt,
  compactOnMobile = false,
}: {
  snapshot: ExecutionQueueSnapshot | null
  loading?: boolean
  error?: string
  action?: ReactNode
  maxQueued?: number
  className?: string
  onRetry?: () => void
  stale?: boolean
  lastUpdatedAt?: number | null
  compactOnMobile?: boolean
}) {
  const queued = maxQueued == null
    ? snapshot?.queued || []
    : (snapshot?.queued || []).slice(0, maxQueued)
  const hidden = Math.max(0, (snapshot?.queued.length || 0) - queued.length)
  const paused = snapshot?.dispatcher.state === 'paused'

  const renderSnapshot = () => snapshot ? (
    <div className="space-y-3 px-3 py-3 sm:px-4">
      <CapacityMeter capacity={snapshot.capacity} />

      {paused && (
        <div className="flex items-start gap-2 rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2.5 text-xs">
          <PauseCircle className="mt-0.5 size-4 shrink-0 text-destructive" aria-hidden="true" />
          <span className="break-words [overflow-wrap:anywhere]">
            {snapshot.dispatcher.pause_reason || '调度器已安全暂停，等待管理员恢复'}
            {snapshot.dispatcher.retry_at && (
              <span className="mt-1 block text-muted-foreground">
                下次自动重试：{fmtTime(snapshot.dispatcher.retry_at)}
              </span>
            )}
          </span>
        </div>
      )}

      {snapshot.active.length > 0 && (
        <section>
          <div className="mb-2 flex items-center justify-between gap-2 text-xs">
            <span className="inline-flex items-center gap-1.5 font-medium">
              <PlayCircle className="size-3.5 text-primary" aria-hidden="true" /> 占用容量
            </span>
            <span className="text-muted-foreground">{snapshot.active.length} 场</span>
          </div>
          <ul className="grid min-w-0 gap-2 xl:grid-cols-2">
            {snapshot.active.map((job) => <JobRow key={job.public_id} job={job} />)}
          </ul>
        </section>
      )}

      {queued.length > 0 ? (
        <section>
          <div className="mb-2 flex items-center justify-between gap-2 text-xs">
            <span className="inline-flex items-center gap-1.5 font-medium">
              <Clock3 className="size-3.5 text-muted-foreground" aria-hidden="true" /> 等待执行
            </span>
            <span className="text-muted-foreground">共 {snapshot.queued_count} 项</span>
          </div>
          <ol className="grid min-w-0 gap-2 xl:grid-cols-2">
            {queued.map((job, index) => <JobRow key={job.public_id} job={job} position={index + 1} />)}
          </ol>
          {hidden > 0 && (
            <p className="mt-2 text-right text-xs text-muted-foreground">另有 {hidden} 项在后续队列</p>
          )}
        </section>
      ) : snapshot.active.length === 0 && !paused ? (
        <div className="flex items-start gap-2 rounded-lg border border-dashed border-border px-3 py-3 text-xs text-muted-foreground">
          <PlayCircle className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
          <span>当前没有等待或执行中的任务</span>
        </div>
      ) : null}
    </div>
  ) : null

  return (
    <Card
      className={cn('gap-0 overflow-hidden py-0', className)}
      data-testid="execution-queue-panel"
      aria-busy={loading}
    >
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-3 py-2.5 sm:px-4">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="inline-flex items-center gap-1.5 text-sm font-semibold">
              <Bot className="size-4 text-primary" aria-hidden="true" /> 全局执行队列
            </h2>
            {snapshot && (
              <span role="status" aria-live="polite">
                <Badge variant={snapshot.dispatcher.accepting && !paused ? 'default' : 'secondary'}>
                  {dispatcherLabel(snapshot.dispatcher.state, snapshot.dispatcher.accepting)}
                </Badge>
              </span>
            )}
            {stale && <Badge variant="outline">数据可能已过期</Badge>}
          </div>
          <p className="mt-0.5 text-xs leading-relaxed text-muted-foreground">
            人工、人机、赛事与自动排位共享对局槽和沙箱容量；优先级会随等待时间动态变化。
          </p>
          {(lastUpdatedAt || (loading && snapshot)) && (
            <p className="mt-0.5 text-xs text-muted-foreground">
              {loading && snapshot
                ? '正在刷新队列…'
                : `上次更新：${new Date(lastUpdatedAt as number).toLocaleTimeString('zh-CN', { hour12: false })}`}
            </p>
          )}
        </div>
        {action}
      </div>

      {error && (
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-3 py-3 sm:px-4" role="alert">
          <div className="min-w-0">
            <ErrorMsg msg={error} />
            {snapshot && <p className="mt-1 text-xs text-muted-foreground">以下保留上次成功获取的数据。</p>}
          </div>
          {onRetry && (
            <Button type="button" size="sm" variant="outline" className="min-h-11" onClick={onRetry}>
              <RotateCcw className="size-3.5" aria-hidden="true" /> 立即重试
            </Button>
          )}
        </div>
      )}
      {loading && !snapshot ? (
        <div className="py-4" role="status" aria-live="polite"><Loading /></div>
      ) : !snapshot ? null : compactOnMobile ? (
        <>
          <details className="group sm:hidden">
            <summary className="flex min-h-11 cursor-pointer list-none items-center justify-between gap-2 px-3 py-2.5 text-sm font-medium focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50">
              <span>查看队列详情</span>
              <span className="text-xs font-normal text-muted-foreground">
                运行 {snapshot.active.length} · 等待 {snapshot.queued_count}
              </span>
            </summary>
            {renderSnapshot()}
          </details>
          <div className="hidden sm:block">{renderSnapshot()}</div>
        </>
      ) : renderSnapshot()}
    </Card>
  )
}

function durationRange(minSeconds: number, maxSeconds: number): string {
  const format = (seconds: number) => seconds < 60 ? `${seconds} 秒` : `${Math.ceil(seconds / 60)} 分钟`
  return `${format(minSeconds)}–${format(maxSeconds)}`
}

export function ExecutionRequestCard({
  snapshot,
  busy = false,
  busyAction = null,
  onCancel,
  onRetry,
  onReset,
}: {
  snapshot: ExecutionRequestSnapshot
  busy?: boolean
  busyAction?: 'cancel' | 'retry' | null
  onCancel: () => void
  onRetry: () => void
  onReset?: () => void
}) {
  const { request, capacity, eta } = snapshot
  const terminal = request.status === 'completed' || request.status === 'cancelled' || request.status === 'interrupted'
  const cancellable = request.status === 'queued' || request.status === 'starting' || request.status === 'running'
  return (
    <Card
      className="mx-auto max-w-2xl gap-3 p-4"
      data-testid="execution-request-card"
      aria-busy={busy}
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h2 className="text-sm font-semibold">执行请求已受理</h2>
          <p className="mt-0.5 text-xs text-muted-foreground">
            {SOURCE_LABEL[request.source] || request.source} · {STATUS_LABEL[request.status] || request.status}
          </p>
        </div>
        <span role="status" aria-live="polite" aria-atomic="true">
          <Badge variant={request.status === 'interrupted' ? 'destructive' : terminal ? 'secondary' : 'default'}>
            {STATUS_LABEL[request.status] || request.status}
          </Badge>
        </span>
      </div>

      <CapacityMeter capacity={capacity} />

      {request.status === 'queued' && (
        <div className="rounded-lg border border-border bg-muted/20 px-3 py-2.5 text-xs">
          <p>前方 {snapshot.ahead_jobs} 项任务，共 {snapshot.ahead_sandbox_units} 个沙箱单位。</p>
          <p className="mt-1 text-muted-foreground">
            动态预计等待 {durationRange(eta.min_seconds, eta.max_seconds)}；{eta.note}。
          </p>
        </div>
      )}

      {request.status === 'interrupted' && (
        <div className="rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2.5 text-xs">
          <p className="font-medium">本次执行因平台恢复而中断，未计入评分。</p>
          <p className="mt-1 text-muted-foreground">{request.reason || '可重新排队，系统不会复活原对局。'}</p>
        </div>
      )}

      {request.status === 'cancelled' && (
        <p className="rounded-lg border border-border bg-muted/20 px-3 py-2.5 text-xs">
          请求已取消，相关容量已安全释放。
        </p>
      )}

      {request.status === 'completed' && !request.match_id && (
        <p className="rounded-lg border border-border bg-muted/20 px-3 py-2.5 text-xs">
          请求已结束，但没有生成可查看的对局；你可以返回表单重新发起。
        </p>
      )}

      {request.cancel_requested && request.status !== 'cancelled' && (
        <p className="text-xs text-muted-foreground">取消请求已记录；平台会先确认沙箱清零，再释放容量。</p>
      )}

      <div className="flex flex-wrap justify-end gap-2">
        {cancellable && !request.cancel_requested && (
          <Button type="button" variant="outline" className="min-h-11" disabled={busy} onClick={onCancel}>
            <Square className="size-3.5" aria-hidden="true" />
            {busyAction === 'cancel' ? '正在取消…' : request.status === 'queued' ? '取消排队' : '取消对局'}
          </Button>
        )}
        {cancellable && request.cancel_requested && (
          <Button type="button" variant="outline" className="min-h-11" disabled>
            <Square className="size-3.5" aria-hidden="true" /> 正在取消…
          </Button>
        )}
        {request.status === 'interrupted' && request.retryable && (
          <Button type="button" className="min-h-11" disabled={busy} onClick={onRetry}>
            <RotateCcw className="size-3.5" aria-hidden="true" />
            {busyAction === 'retry' ? '正在重试…' : '重新排队'}
          </Button>
        )}
        {terminal && onReset && (
          <Button type="button" variant="outline" className="min-h-11" disabled={busy} onClick={onReset}>
            {request.status === 'cancelled' ? '发起新请求' : '返回挑战表单'}
          </Button>
        )}
      </div>
    </Card>
  )
}
