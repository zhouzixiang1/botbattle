import type { ReactNode } from 'react'
import {
  Activity,
  AlertTriangle,
  Bot,
  Clock3,
  Cpu,
  ListOrdered,
  MemoryStick,
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
import {
  RuntimeEnvironmentBadge,
  type ExecutionEnvironment,
} from '@/components/runtime-environment'

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
  bot_a_environment?: ExecutionEnvironment | null
  bot_b_environment?: ExecutionEnvironment | null
  rated: boolean
  rating_reason: string
  retryable: boolean
  cancel_requested: boolean
  reason: string
  capacity_blocked_code?: string
  capacity_blocked_reason?: string
  /** Compatibility with the bounded public projection. */
  blocked_code?: string
  blocked_reason?: string
  created_at?: string | null
  started_at?: string | null
  terminal_at?: string | null
}

export interface ExecutionCapacity {
  match_slots: { used: number; capacity: number }
  sandbox_units: { used: number; capacity: number }
  host_cpu_millis?: { used: number; capacity: number }
  host_memory_mb?: { used: number; capacity: number }
  running_matches: number
}

export interface ExecutionMaintenanceSnapshot {
  requested: boolean
  ready: boolean
  reason: string
  active_count: number
  uploads_in_flight: number
  active_local_ai_leases?: number
  untracked_running_matches?: number
  docker_launch_state?: string
  owned_execution_tasks?: number
  readiness_unavailable?: string[]
}

export type AutoSchedulerState =
  | 'disabled'
  | 'foreground_busy'
  | 'contest_guard'
  | 'cooldown'
  | 'ready'
  | 'yielding'
  | 'running'

export interface AutoSchedulerSnapshot {
  mode: 'idle_only'
  state: AutoSchedulerState
  reason: string
  idle_required_seconds: number
  cooldown_seconds: number
  max_active: number
  queued_target: number
  next_eligible_at?: string | null
}

export interface ExecutionQueueSnapshot {
  dispatcher: {
    state: string
    accepting: boolean
    auto_enabled: boolean
    maintenance?: boolean
    pause_reason: string
    retry_at?: string | null
  }
  capacity: ExecutionCapacity
  active: ExecutionQueueJob[]
  queued: ExecutionQueueJob[]
  queued_count: number
  maintenance?: ExecutionMaintenanceSnapshot
  /** Optional while older servers roll forward to the idle-only contract. */
  auto_scheduler?: AutoSchedulerSnapshot
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
  capacity_blocked_code?: string
  capacity_blocked_reason?: string
  blocked_code?: string
  blocked_reason?: string
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
  contest: '只计赛事成绩，不计平台排行榜',
  human: '人机对战，不计平台排行榜',
  self_play: '自博弈，不计平台排行榜',
  bot_missing: 'Bot 信息不完整，不计平台排行榜',
  same_owner: '同一所有者，不计平台排行榜',
  remote_local: '本地 Bot 练习，不计平台排行榜',
  ranked_bot_not_selected: '至少一方未派遣参榜，不计平台排行榜',
  eligible: '计入平台排行榜',
}

const EXECUTION_REASON_LABEL: Record<string, string> = {
  auto_yield_foreground: '自动排位为前台任务让路',
  auto_idle_policy_cutover: '自动排位策略升级后收口',
}

const AUTO_SCHEDULER_REASON_LABEL: Record<string, string> = {
  auto_disabled: '管理员已关闭闲时排位',
  foreground_queued_or_active: '等待用户挑战、人机或赛事任务完成',
  contest_guard: '真实赛事运行、休息或临近开赛，暂不启动自动排位',
  idle_ready: '闲时门禁已满足，仍等待候选、评分与资源安全门',
  auto_running: '正在执行一场闲时排位',
  auto_yield_foreground: '自动排位为前台任务让路',
  auto_idle_policy_cutover: '自动排位策略升级后收口',
}

export interface AutoSchedulerPresentation {
  label: string
  detail: string
}

function minutes(seconds: number | undefined, fallback: number): number {
  return Math.max(
    1,
    Math.ceil(typeof seconds === 'number' && Number.isFinite(seconds) ? seconds / 60 : fallback),
  )
}

/**
 * Keep old snapshots readable during a rolling deploy, while preferring the
 * server-owned idle scheduler state whenever it is available.
 */
export function autoSchedulerPresentation(
  snapshot: ExecutionQueueSnapshot,
): AutoSchedulerPresentation {
  const scheduler = snapshot.auto_scheduler
  if (!scheduler) {
    return {
      label: '策略同步中',
      detail: '当前服务尚未返回闲时排位调度状态；正在兼容同步，请以现有队列为准',
    }
  }

  const idleMinutes = minutes(scheduler.idle_required_seconds, 5)
  const cooldownMinutes = minutes(scheduler.cooldown_seconds, 5)
  const maxActive = Math.max(1, Number(scheduler.max_active || 1))
  const queuedTarget = Math.max(1, Number(scheduler.queued_target || 1))
  const limits = `最多 ${maxActive} 场、${queuedTarget} 个候选`
  const reason = scheduler.reason === 'idle_grace'
    ? `正在等待连续空闲 ${idleMinutes} 分钟`
    : scheduler.reason === 'auto_cooldown'
      ? `上一场自动排位后正在冷却 ${cooldownMinutes} 分钟`
      : scheduler.reason
        ? AUTO_SCHEDULER_REASON_LABEL[scheduler.reason] || ''
        : ''
  const activeAuto = snapshot.active.some((job) => job.source === 'auto')
  const foregroundBusy = [...snapshot.active, ...snapshot.queued]
    .some((job) => job.source !== 'auto')

  if (
    scheduler?.state === 'yielding'
    || scheduler?.reason === 'auto_yield_foreground'
    || scheduler?.reason === 'auto_idle_policy_cutover'
  ) {
    return {
      label: '安全收口中',
      detail: `${reason || '自动排位正在安全收口'}；精确清理后释放容量；${limits}`,
    }
  }

  if (!snapshot.dispatcher.auto_enabled || scheduler?.state === 'disabled') {
    return {
      label: '已关闭',
      detail: activeAuto
        ? `不再启动新局；当前自动局自然结束；${limits}`
        : `不再生成或启动新的闲时排位；${limits}`,
    }
  }

  switch (scheduler?.state) {
    case 'foreground_busy':
      return {
        label: '前台优先',
        detail: `${reason || '等待用户挑战、人机或赛事任务完成'}；${limits}`,
      }
    case 'contest_guard':
      return {
        label: '赛事保护',
        detail: `${reason || '真实赛事期间不启动自动排位'}；${limits}`,
      }
    case 'cooldown': {
      const next = scheduler.next_eligible_at
        ? `；最早 ${fmtTime(scheduler.next_eligible_at)}`
        : ''
      return {
        label: '等待闲时',
        detail: `${reason || `等待连续空闲或冷却 ${cooldownMinutes} 分钟`}${next}；${limits}`,
      }
    }
    case 'ready':
      return {
        label: '闲时就绪',
        detail: `闲时门禁已满足，仍等待候选、评分与资源安全门；${limits}`,
      }
    case 'running':
      return {
        label: '闲时运行中',
        detail: `${reason || '当前正在运行'}；${limits}，前台到达时会安全让路并保留一个运行位`,
      }
    default:
      if (activeAuto) {
        return {
          label: '闲时运行中',
          detail: `${limits}，前台到达时会安全让路并保留一个运行位`,
        }
      }
      if (foregroundBusy) {
        return {
          label: '前台优先',
          detail: `等待用户挑战、人机或赛事任务完成；${limits}`,
        }
      }
      return {
        label: '等待闲时',
        detail: `启用不等于立即运行；连续空闲 ${idleMinutes} 分钟后开始；${limits}`,
      }
  }
}

function dispatcherLabel(state: string, accepting: boolean, maintenance = false): string {
  if (state === 'paused') return '安全暂停'
  if (maintenance) return '维护中'
  if (state === 'starting') return '正在启动'
  if (state === 'stopping' || state === 'draining') return '正在停止'
  if (state === 'stopped') return '已停止'
  return accepting ? '接收任务' : '暂不接收'
}

function CapacityMeter({ capacity }: { capacity: ExecutionCapacity }) {
  const matchSlots = capacity.match_slots || { used: 0, capacity: 0 }
  const sandboxUnits = capacity.sandbox_units || { used: 0, capacity: 0 }
  const hostCpu = capacity.host_cpu_millis
  const hostMemory = capacity.host_memory_mb
  const showHostResources = Boolean(hostCpu && hostMemory)
  const cpuLabel = (value: number) => `${Number((value / 1000).toFixed(1))} 核`
  const memoryLabel = (value: number) => (
    value >= 1024 && value % 1024 === 0 ? `${value / 1024} GiB` : `${value} MiB`
  )
  return (
    <dl
      className={cn('grid grid-cols-2 gap-2 text-xs', showHostResources && 'lg:grid-cols-4')}
      aria-label="执行容量"
    >
      <div className="rounded-md border border-border bg-muted/20 px-2.5 py-2">
        <dt className="inline-flex items-center gap-1 text-muted-foreground">
          <Activity className="size-3.5" /> 同时运行
        </dt>
        <dd className="mt-0.5 font-mono font-semibold tabular-nums">
          {matchSlots.used} / {matchSlots.capacity} 场
        </dd>
      </div>
      <div className="rounded-md border border-border bg-muted/20 px-2.5 py-2">
        <dt className="inline-flex items-center gap-1 text-muted-foreground">
          <Cpu className="size-3.5" /> 平台 Bot 运行位
        </dt>
        <dd className="mt-0.5 font-mono font-semibold tabular-nums">
          {sandboxUnits.used} / {sandboxUnits.capacity}
        </dd>
      </div>
      {hostCpu && (
        <div
          className="rounded-md border border-border bg-muted/20 px-2.5 py-2"
          data-testid="host-cpu-capacity"
        >
          <dt className="inline-flex items-center gap-1 text-muted-foreground">
            <Cpu className="size-3.5" /> 主机 CPU
          </dt>
          <dd className="mt-0.5 font-mono font-semibold tabular-nums">
            {cpuLabel(hostCpu.used)} / {cpuLabel(hostCpu.capacity)}
          </dd>
        </div>
      )}
      {hostMemory && (
        <div
          className="rounded-md border border-border bg-muted/20 px-2.5 py-2"
          data-testid="host-memory-capacity"
        >
          <dt className="inline-flex items-center gap-1 text-muted-foreground">
            <MemoryStick className="size-3.5" /> 主机内存
          </dt>
          <dd className="mt-0.5 font-mono font-semibold tabular-nums">
            {memoryLabel(hostMemory.used)} / {memoryLabel(hostMemory.capacity)}
          </dd>
        </div>
      )}
    </dl>
  )
}

function capacityBlockedReason(job: ExecutionQueueJob): string {
  const code = job.capacity_blocked_code || job.blocked_code
  return job.capacity_blocked_reason
    || job.blocked_reason
    || (code ? '当前主机资源不足；请求会保留排队，资源满足后再开始。' : '')
}

function JobRow({ job, position }: { job: ExecutionQueueJob; position?: number }) {
  const active = job.status === 'starting' || job.status === 'running' || job.status === 'settling'
  const blockedReason = capacityBlockedReason(job)
  const executionReason = EXECUTION_REASON_LABEL[job.reason]
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
          <RuntimeEnvironmentBadge environment={job.bot_a_environment} />
          {job.bot_b_environment !== job.bot_a_environment && (
            <RuntimeEnvironmentBadge environment={job.bot_b_environment} />
          )}
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
        <span>{job.sandbox_units === 0 ? '不占用平台运行位' : `占用 ${job.sandbox_units} 个平台 Bot 运行位`}</span>
        <span>{job.rated ? '计入平台排行榜' : RATING_REASON_LABEL[job.rating_reason] || '不计平台排行榜'}</span>
        {job.source === 'auto' && job.status === 'queued' && <span>等待平台闲时，不占前台顺位</span>}
        {job.cancel_requested && <span>正在安全取消</span>}
        {executionReason && <span>{executionReason}</span>}
      </div>
      {blockedReason && (
        <p className="mt-1.5 flex min-w-0 items-start gap-1.5 text-xs text-warning-foreground" role="status">
          <AlertTriangle className="mt-0.5 size-3.5 shrink-0 text-warning" aria-hidden="true" />
          <span className="min-w-0 break-words [overflow-wrap:anywhere]">{blockedReason}</span>
        </p>
      )}
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
  const maintenance = Boolean(snapshot?.maintenance?.requested || snapshot?.dispatcher.maintenance)
  const paused = snapshot?.dispatcher.state === 'paused'
  const autoPresentation = snapshot ? autoSchedulerPresentation(snapshot) : null
  let foregroundPosition = 0
  const queuedRows = queued.map((job) => ({
    job,
    position: job.source === 'auto' ? undefined : ++foregroundPosition,
  }))

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

      {maintenance && snapshot?.maintenance?.ready && (
        <div className="flex items-start gap-2 rounded-lg border border-primary/25 bg-primary/5 px-3 py-2.5 text-xs" role="status">
          <Square className="mt-0.5 size-4 shrink-0 fill-primary/15 text-primary" aria-hidden="true" />
          <span className="break-words [overflow-wrap:anywhere]">
            运行环境已排空；等待任务已保留，恢复后会继续执行。
          </span>
        </div>
      )}

      <div className="grid min-w-0 gap-3 lg:grid-cols-2">
        <section className="min-w-0 rounded-lg border border-border bg-background p-2.5">
          <div className="mb-2 flex items-center justify-between gap-2 text-xs">
            <span className="inline-flex items-center gap-1.5 font-medium">
              <PlayCircle className="size-3.5 text-primary" aria-hidden="true" /> 正在执行
            </span>
            <span className="shrink-0 text-muted-foreground">{snapshot.active.length} 场</span>
          </div>
          {snapshot.active.length > 0 ? (
            <ul className="grid min-w-0 gap-2">
              {snapshot.active.map((job) => <JobRow key={job.public_id} job={job} />)}
            </ul>
          ) : (
            <p className="rounded-md border border-dashed border-border px-3 py-3 text-xs text-muted-foreground">
              当前没有运行中的对局
            </p>
          )}
        </section>

        <section className="min-w-0 rounded-lg border border-border bg-background p-2.5">
          <div className="mb-2 flex items-center justify-between gap-2 text-xs">
            <span className="inline-flex items-center gap-1.5 font-medium">
              <Clock3 className="size-3.5 text-muted-foreground" aria-hidden="true" /> 等待执行
            </span>
            <span className="shrink-0 text-muted-foreground">共 {snapshot.queued_count} 项</span>
          </div>
          {queued.length > 0 ? (
            <>
              <ol className="grid min-w-0 gap-2">
                {queuedRows.map(({ job, position }) => (
                  <JobRow key={job.public_id} job={job} position={position} />
                ))}
              </ol>
              {hidden > 0 && (
                <p className="mt-2 text-right text-xs text-muted-foreground">另有 {hidden} 项在后续队列</p>
              )}
            </>
          ) : (
            <p className="rounded-md border border-dashed border-border px-3 py-3 text-xs text-muted-foreground">
              当前没有等待任务
            </p>
          )}
        </section>
      </div>
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
                  {dispatcherLabel(
                    snapshot.dispatcher.state,
                    snapshot.dispatcher.accepting,
                    Boolean(snapshot.dispatcher.maintenance),
                  )}
                </Badge>
              </span>
            )}
            {stale && <Badge variant="outline">数据可能已过期</Badge>}
          </div>
          <p className="mt-0.5 text-xs leading-relaxed text-muted-foreground">
            {snapshot
              ? `全站当前对局槽上限 ${snapshot.capacity.match_slots.capacity} 场；`
              : '正在获取全站对局槽容量；'}
            主机资源不足的任务继续排队。
            {snapshot?.auto_scheduler
              ? '用户挑战、人机和赛事始终优先；闲时排位不计入前台顺位或 ETA。'
              : '闲时排位策略状态正在同步，以当前队列为准。'}
          </p>
          {autoPresentation && (
            <p
              className="mt-0.5 flex min-w-0 items-start gap-1.5 text-xs leading-relaxed text-muted-foreground"
              data-testid="auto-scheduler-status"
              role="status"
              aria-live="polite"
            >
              <Clock3 className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
              <span className="min-w-0 break-words [overflow-wrap:anywhere]">
                闲时排位：{autoPresentation.label} · {autoPresentation.detail}
              </span>
            </p>
          )}
          {(lastUpdatedAt || (loading && snapshot)) && (
            <p className="mt-0.5 text-xs text-muted-foreground">
              {loading && snapshot
                ? '正在刷新队列…'
                : `上次更新：${new Date(lastUpdatedAt as number).toLocaleTimeString('zh-CN', { hour12: false })}`}
            </p>
          )}
        </div>
        {action && <div className="w-full min-w-0 max-w-full sm:w-auto">{action}</div>}
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
  const blockedReason = snapshot.capacity_blocked_reason
    || snapshot.blocked_reason
    || capacityBlockedReason(request)
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

      <div className="flex min-w-0 flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
        <RuntimeEnvironmentBadge environment={request.bot_a_environment} />
        {request.bot_b_environment !== request.bot_a_environment && (
          <RuntimeEnvironmentBadge environment={request.bot_b_environment} />
        )}
        <span>{request.rated ? '计入平台排行榜' : RATING_REASON_LABEL[request.rating_reason] || '不计平台排行榜'}</span>
      </div>

      <CapacityMeter capacity={capacity} />

      {request.status === 'queued' && (
        <div className="rounded-lg border border-border bg-muted/20 px-3 py-2.5 text-xs">
          <p>{request.source === 'auto'
            ? '等待平台闲时；不占用户挑战、人机或赛事的前台顺位。'
            : `前方 ${snapshot.ahead_jobs} 项前台任务；闲时排位不计入此顺位。`}</p>
          {blockedReason ? (
            <p className="mt-1 flex min-w-0 items-start gap-1.5 text-warning-foreground" role="status">
              <AlertTriangle className="mt-0.5 size-3.5 shrink-0 text-warning" aria-hidden="true" />
              <span className="min-w-0 break-words [overflow-wrap:anywhere]">{blockedReason}</span>
            </p>
          ) : (
            <p className="mt-1 text-muted-foreground">
              动态预计等待 {durationRange(eta.min_seconds, eta.max_seconds)}；{eta.note}。
            </p>
          )}
        </div>
      )}

      {request.status === 'interrupted' && (
        <div className="rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2.5 text-xs">
          <p className="font-medium">本次执行因平台恢复而中断，不计平台排行榜。</p>
          <p className="mt-1 text-muted-foreground">{request.reason || '可重新排队，系统不会复活原对局。'}</p>
        </div>
      )}

      {request.status === 'cancelled' && (
        <p className="rounded-lg border border-border bg-muted/20 px-3 py-2.5 text-xs">
          {EXECUTION_REASON_LABEL[request.reason] || '请求已取消，相关容量已安全释放。'}
        </p>
      )}

      {request.status === 'completed' && !request.match_id && (
        <p className="rounded-lg border border-border bg-muted/20 px-3 py-2.5 text-xs">
          请求已结束，但没有生成可查看的对局；你可以返回表单重新发起。
        </p>
      )}

      {request.cancel_requested && request.status !== 'cancelled' && (
      <p className="text-xs text-muted-foreground">取消请求已记录；平台会在当前任务安全停止后释放运行位。</p>
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
