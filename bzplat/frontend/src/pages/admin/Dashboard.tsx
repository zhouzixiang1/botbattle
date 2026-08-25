import { useCallback, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { CircleCheck, Clock3, PauseCircle, PlayCircle, Wrench } from 'lucide-react'
import { toast } from 'sonner'
import { apiFetch, apiJson, errMsg } from '@/api'
import { MetricCard, Card, CardHeader, CardTitle, EmptyState, Loading, ErrorMsg, RefreshBtn, Button } from './ui'
import {
  autoSchedulerPresentation,
  ExecutionQueuePanel,
  type ExecutionQueueSnapshot,
} from '@/components/execution-queue'
import { Switch } from '@/components/ui/switch'
import { useConfirm } from '@/hooks/use-confirm'
import { useSingleFlightPolling } from '@/hooks/use-single-flight-polling'
import { fmtTime } from '@/lib/format'
import { OverflowText } from '@/components/ui/overflow-text'

interface Stats {
  users: number
  users_active: number
  users_verified: number
  bots: number
  bots_active: number
  matches: number
  matches_completed: number
  matches_aborted: number
  matches_running: number
  matches_pending: number
  contests: number
  contests_running: number
  active_sessions: number
  recent_users: { id: number; username: string; email: string; role: string; created_at: string }[]
}

interface RuntimeDiagnostics {
  queue: ExecutionQueueSnapshot
}

const ROLE_LABEL: Record<string, string> = {
  user: '普通用户',
  organizer: '组织者',
  admin: '管理员',
}

type QueueAction = 'auto' | 'prepare-maintenance' | 'resume' | 'recover' | null

export default function Dashboard() {
  const [stats, setStats] = useState<Stats | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [queue, setQueue] = useState<ExecutionQueueSnapshot | null>(null)
  const [lastUpdatedAt, setLastUpdatedAt] = useState<number | null>(null)
  const [queueAction, setQueueAction] = useState<QueueAction>(null)
  const requestRevision = useRef(0)
  const queueMutationInFlight = useRef(false)
  const [confirm, confirmDialog] = useConfirm()
  const maintenanceActive = Boolean(
    queue?.maintenance?.requested || queue?.dispatcher.maintenance,
  )

  const pollDashboard = useCallback(async (signal: AbortSignal) => {
    const revision = requestRevision.current
    const [statsResult, runtimeResult] = await Promise.allSettled([
      apiFetch<Stats>('/api/admin/stats', { method: 'GET', signal }),
      apiFetch<RuntimeDiagnostics>('/api/admin/settings/runtime', { method: 'GET', signal }),
    ])
    if (statsResult.status === 'rejected') throw statsResult.reason
    if (runtimeResult.status === 'rejected') throw runtimeResult.reason
    if (signal.aborted || revision !== requestRevision.current) return
    setStats(statsResult.value)
    setQueue(runtimeResult.value.queue)
    setLastUpdatedAt(Date.now())
  }, [])

  const {
    refresh,
    polling,
    offline,
  } = useSingleFlightPolling({
    task: pollDashboard,
    enabled: queueAction === null && !maintenanceActive,
    intervalMs: 5_000,
    maxIntervalMs: 40_000,
    onSuccess: () => {
      setError('')
      setLoading(false)
    },
    onError: (e) => {
      setError(errMsg(e, '加载失败'))
      setLoading(false)
    },
  })

  const pollMaintenance = useCallback(async (signal: AbortSignal) => {
    const revision = requestRevision.current
    const updated = await apiFetch<ExecutionQueueSnapshot>(
      '/api/admin/execution-queue/maintenance',
      { method: 'GET', signal },
    )
    if (signal.aborted || revision !== requestRevision.current) return
    setQueue(updated)
    setLastUpdatedAt(Date.now())
  }, [])

  const { refresh: refreshMaintenance } = useSingleFlightPolling({
    task: pollMaintenance,
    enabled: queueAction === null && maintenanceActive,
    intervalMs: 1_500,
    maxIntervalMs: 12_000,
    onSuccess: () => setError(''),
    onError: (e) => setError(errMsg(e, '读取排空进度失败')),
  })
  const refreshCurrent = maintenanceActive ? refreshMaintenance : refresh

  const toggleAutoMatch = async (enabled: boolean) => {
    if (queueMutationInFlight.current || queue?.maintenance?.requested || queue?.dispatcher.maintenance) return
    const revision = ++requestRevision.current
    queueMutationInFlight.current = true
    setQueueAction('auto')
    try {
      const updated = await apiJson<ExecutionQueueSnapshot>(
        '/api/admin/auto-match',
        'PUT',
        { enabled },
      )
      if (revision !== requestRevision.current) return
      setQueue(updated)
      setLastUpdatedAt(Date.now())
      toast.success(enabled
        ? '闲时排位已启用；满足空闲与冷却条件后自动运行'
        : '闲时排位已关闭；当前自动局会自然结束，前台任务不受影响')
    } catch (e) {
      if (revision === requestRevision.current) {
        toast.error(errMsg(e, '更新自动排位总开关失败'))
      }
    } finally {
      queueMutationInFlight.current = false
      setQueueAction(null)
    }
  }

  const resumeQueue = async () => {
    if (queueMutationInFlight.current) return
    if (!await confirm({
      title: '清场并恢复执行队列',
      desc: '将删除当前平台实例标签下的所有容器，确认清零后补偿中断任务并恢复派发。',
      confirmText: '清场并恢复',
      danger: true,
      buttonClassName: 'min-h-12 sm:min-h-9',
    })) return
    const revision = ++requestRevision.current
    queueMutationInFlight.current = true
    setQueueAction('recover')
    try {
      const updated = await apiJson<ExecutionQueueSnapshot>(
        '/api/admin/execution-queue/resume',
        'POST',
      )
      if (revision !== requestRevision.current) return
      setQueue(updated)
      setLastUpdatedAt(Date.now())
      if (updated.dispatcher.state === 'paused') {
        toast.error(updated.dispatcher.pause_reason || '清理尚未确认，队列仍保持暂停')
      } else if (updated.maintenance?.requested) {
        toast.success('运行环境已恢复，继续完成部署排空')
      } else {
        toast.success('全局执行队列已恢复')
      }
    } catch (e) {
      toast.error(errMsg(e, '恢复全局执行队列失败'))
    } finally {
      queueMutationInFlight.current = false
      setQueueAction(null)
    }
  }

  const prepareMaintenance = async () => {
    if (!queue || queueMutationInFlight.current || queue.maintenance?.requested) return
    if (!await confirm({
      title: '准备部署维护',
      desc: '平台会立即停止接收新任务并关闭自动排位。当前对局继续到自然结束，等待中的任务不会丢失。',
      confirmText: '开始排空',
      buttonClassName: 'min-h-12 sm:min-h-9',
    })) return
    if (queueMutationInFlight.current) return
    const revision = ++requestRevision.current
    queueMutationInFlight.current = true
    setQueueAction('prepare-maintenance')
    try {
      const updated = await apiJson<ExecutionQueueSnapshot>(
        '/api/admin/execution-queue/maintenance',
        'POST',
        { reason: '管理员准备部署' },
      )
      if (revision !== requestRevision.current) return
      setQueue(updated)
      setLastUpdatedAt(Date.now())
      toast.success(updated.maintenance?.ready
        ? '已排空，可以安全停服'
        : '已停止接单；当前任务结束后即可停服')
    } catch (error) {
      if (revision !== requestRevision.current) return
      toast.error(errMsg(error, '开始排空失败'))
    } finally {
      queueMutationInFlight.current = false
      setQueueAction(null)
    }
  }

  const leaveMaintenance = async () => {
    if (queueMutationInFlight.current) return
    const revision = ++requestRevision.current
    queueMutationInFlight.current = true
    setQueueAction('resume')
    try {
      const updated = await apiJson<ExecutionQueueSnapshot>(
        '/api/admin/execution-queue/maintenance',
        'DELETE',
      )
      if (revision !== requestRevision.current) return
      setQueue(updated)
      setLastUpdatedAt(Date.now())
      toast.success('调度已恢复；自动排位仍保持关闭')
    } catch (error) {
      if (revision === requestRevision.current) {
        toast.error(errMsg(error, '恢复调度失败'))
      }
    } finally {
      queueMutationInFlight.current = false
      setQueueAction(null)
    }
  }

  if (loading && !stats) return <div role="status" aria-live="polite"><Loading /></div>
  if ((error || offline) && !stats) {
    return (
      <div className="space-y-3" role="alert">
        <ErrorMsg msg={offline ? '当前离线；联网后会自动恢复管理总览。' : error} />
        <Button type="button" variant="outline" className="min-h-11" onClick={refresh}>
          立即重试
        </Button>
      </div>
    )
  }
  if (!stats) return <EmptyState text="无数据" />

  // running 是健康的活跃状态，不能计入异常；历史 Bot 响应异常在对局记录中单独展示。
  const abnormal = stats.matches_aborted

  return (
    <div>
      <div className="mb-3 flex items-center justify-between">
        <p className="text-sm text-muted-foreground">平台总览统计</p>
        <RefreshBtn onClick={refreshCurrent} className="min-h-11" />
      </div>
      {(error || offline) && (
        <div role="alert"><ErrorMsg msg={offline ? '当前离线；以下保留上次成功数据。' : error} /></div>
      )}

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
        <MetricCard label="用户" value={stats.users} hint={`活跃 ${stats.users_active}`} />
        <MetricCard label="Bot" value={stats.bots} hint={`活跃 ${stats.bots_active}`} />
        <MetricCard label="对局" value={stats.matches} hint={`完成 ${stats.matches_completed}`} />
        <MetricCard label="异常对局" value={abnormal} danger={abnormal > 0}
          hint={`已中止 ${stats.matches_aborted} · 运行中 ${stats.matches_running}`} />
        <MetricCard label="比赛" value={stats.contests} hint={`进行中 ${stats.contests_running}`} />
        <MetricCard label="在线会话" value={stats.active_sessions} />
      </div>

      <ExecutionQueuePanel
        snapshot={queue}
        loading={!queue && polling}
        error={offline ? '当前离线；联网后会自动刷新内部队列诊断。' : error}
        stale={!!queue && (offline || !!error)}
        lastUpdatedAt={lastUpdatedAt}
        onRetry={refreshCurrent}
        maxQueued={6}
        compactOnMobile
        className="mt-5"
        action={queue ? (
          <MaintenanceControls
            queue={queue}
            action={queueAction}
            onToggleAuto={(checked) => { void toggleAutoMatch(checked) }}
            onPrepare={() => { void prepareMaintenance() }}
            onLeave={() => { void leaveMaintenance() }}
            onRecover={() => { void resumeQueue() }}
          />
        ) : null}
      />

      <div className="mt-5 grid gap-4 lg:grid-cols-2">
        <Card className="min-w-0 overflow-hidden">
          <CardHeader><CardTitle>最近注册用户</CardTitle></CardHeader>
          {stats.recent_users.length === 0 ? (
            <EmptyState text="暂无用户" />
          ) : (
            <ul className="divide-y divide-border">
              {stats.recent_users.map((u) => (
                <li key={u.id} className="grid min-w-0 gap-1 py-2 text-sm sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center sm:gap-3">
                  <Link
                    to={`/user/${encodeURIComponent(u.username)}`}
                    className="min-w-0 max-w-full truncate font-medium text-primary hover:underline"
                  >
                    <OverflowText tooltipFocusable={false}>{u.username}</OverflowText>
                  </Link>
                  <span className="flex min-w-0 flex-wrap gap-x-2 gap-y-0.5 text-xs text-muted-foreground sm:justify-end sm:text-right">
                    <span className="break-words [overflow-wrap:anywhere]">{ROLE_LABEL[u.role] || u.role}</span>
                    <span className="break-words [overflow-wrap:anywhere]">{fmtTime(u.created_at)}</span>
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card className="min-w-0 overflow-hidden">
          <CardHeader><CardTitle>对局状态分布</CardTitle></CardHeader>
          <div className="space-y-2">
            <DistRow label="完成" n={stats.matches_completed} total={stats.matches} color="bg-success" />
            <DistRow label="运行中" n={stats.matches_running} total={stats.matches} color="bg-primary" />
            <DistRow label="待开始" n={stats.matches_pending} total={stats.matches} color="bg-muted-foreground" />
            <DistRow label="中止" n={stats.matches_aborted} total={stats.matches} color="bg-destructive" />
          </div>
          {stats.matches === 0 && <EmptyState text="暂无对局" />}
        </Card>
      </div>
      {confirmDialog}
    </div>
  )
}

function MaintenanceControls({
  queue,
  action,
  onToggleAuto,
  onPrepare,
  onLeave,
  onRecover,
}: {
  queue: ExecutionQueueSnapshot
  action: QueueAction
  onToggleAuto: (enabled: boolean) => void
  onPrepare: () => void
  onLeave: () => void
  onRecover: () => void
}) {
  const requested = Boolean(queue.maintenance?.requested || queue.dispatcher.maintenance)
  const ready = Boolean(queue.maintenance?.ready)
  const faultPaused = queue.dispatcher.state === 'paused'
  const busy = action !== null
  const normal = queue.dispatcher.state === 'running' && queue.dispatcher.accepting
  const autoStatus = autoSchedulerPresentation(queue)
  const status = faultPaused
    ? {
        label: requested ? '排空受阻' : '队列异常暂停',
        detail: queue.dispatcher.pause_reason || '运行环境需要管理员确认后恢复',
        icon: PauseCircle,
        tone: 'border-destructive/30 bg-destructive/5 text-destructive',
      }
    : requested && ready
    ? {
        label: '可安全停服',
        detail: `运行环境已清空 · ${queue.queued_count} 项等待任务已保留`,
        icon: CircleCheck,
        tone: 'border-primary/30 bg-primary/5 text-primary',
      }
    : requested
      ? {
          label: '排空中',
          detail: maintenanceBlocker(queue),
          icon: Clock3,
          tone: 'border-warning/40 bg-warning/10 text-warning-foreground',
        }
      : {
            label: normal ? '正常调度' : '调度暂未开放',
            detail: `运行 ${queue.active.length} · 等待 ${queue.queued_count}`,
            icon: normal ? PlayCircle : PauseCircle,
            tone: 'border-border bg-muted/20 text-foreground',
          }
  const StatusIcon = status.icon

  return (
    <div
      className="grid w-full min-w-0 gap-2 sm:w-auto sm:grid-cols-[minmax(10rem,auto)_minmax(13rem,20rem)_auto] sm:items-stretch"
      data-testid="deployment-maintenance-control"
    >
      <div
        className={`flex min-h-11 min-w-0 items-center gap-2 rounded-lg border px-2.5 py-1.5 ${status.tone}`}
        role="status"
        aria-live="polite"
      >
        <StatusIcon className="size-4 shrink-0" aria-hidden="true" />
        <span className="min-w-0 leading-tight">
          <span className="block text-xs font-semibold">{status.label}</span>
          <span className="mt-0.5 block break-words text-[0.6875rem] opacity-75 [overflow-wrap:anywhere]">
            {status.detail}
          </span>
        </span>
      </div>

      <div className="flex min-h-11 min-w-0 items-center justify-between gap-2 rounded-lg border border-border bg-background px-2.5 py-1.5 sm:justify-start">
        <div className="min-w-0">
          <div className="text-xs font-medium">闲时排位</div>
          <div className="break-words text-[0.6875rem] leading-tight text-muted-foreground [overflow-wrap:anywhere]">
            {autoStatus.label} · {autoStatus.detail}
          </div>
        </div>
        <Switch
          checked={queue.dispatcher.auto_enabled}
          disabled={busy || requested}
          onCheckedChange={onToggleAuto}
          aria-label="闲时自动排位开关"
          className="relative before:absolute before:-inset-x-3 before:-inset-y-3.5 before:content-['']"
        />
      </div>

      {faultPaused ? (
        <Button
          type="button"
          size="sm"
          className="min-h-11 w-full sm:w-auto"
          variant="outline"
          disabled={busy}
          onClick={onRecover}
        >
          {action === 'recover' ? '正在清场…' : '清场并恢复'}
        </Button>
      ) : requested ? (
        <Button
          type="button"
          size="sm"
          className="min-h-11 w-full sm:w-auto"
          disabled={busy || !ready}
          onClick={onLeave}
        >
          <PlayCircle className="size-3.5" aria-hidden="true" />
          {action === 'resume' ? '正在恢复…' : ready ? '恢复调度' : '正在排空…'}
        </Button>
      ) : (
        <Button
          type="button"
          size="sm"
          className="min-h-11 w-full sm:w-auto"
          variant="outline"
          disabled={busy || !normal}
          onClick={onPrepare}
        >
          <Wrench className="size-3.5" aria-hidden="true" />
          准备维护
        </Button>
      )}
    </div>
  )
}

function maintenanceBlocker(queue: ExecutionQueueSnapshot): string {
  const status = queue.maintenance
  if (!status) return '正在确认运行环境已清空'
  if (status.active_count > 0) return `还有 ${status.active_count} 场对局正在自然结束`
  if ((status.uploads_in_flight || 0) > 0) return `还有 ${status.uploads_in_flight} 个上传正在完成检查`
  if ((status.active_local_ai_leases || 0) > 0) return `还有 ${status.active_local_ai_leases} 个本地 Bot 连接正在释放`
  if ((status.untracked_running_matches || 0) > 0) return `还有 ${status.untracked_running_matches} 场遗留对局仍标记为运行中`
  if (status.docker_launch_state && status.docker_launch_state !== 'idle') return '沙箱正在完成启动或清理'
  if ((status.owned_execution_tasks || 0) > 0) return `还有 ${status.owned_execution_tasks} 个执行任务正在收尾`
  const unavailable = status.readiness_unavailable || []
  if (unavailable.includes('application_recovery')) return '评分与赛事状态正在恢复，完成前不能停服'
  if (unavailable.includes('upload_activity')) return '暂时无法确认上传活动，正在等待安全检查恢复'
  if (unavailable.includes('owned_execution_tasks')) return '暂时无法确认执行任务，正在等待安全检查恢复'
  if (unavailable.length > 0) return `还有 ${unavailable.length} 项运行环境检查暂不可用`
  return '正在确认运行环境已清空'
}

function DistRow({ label, n, total, color }: { label: string; n: number; total: number; color: string }) {
  const pct = total > 0 ? Math.round((n / total) * 100) : 0
  return (
    <div className="grid min-w-0 grid-cols-[3.5rem_minmax(0,1fr)_auto] items-center gap-2 text-xs">
      <span className="text-muted-foreground">{label}</span>
      <div className="h-2 min-w-0 overflow-hidden rounded-full bg-muted">
        <div className={`h-full ${color}`} style={{ width: `${pct}%` }} />
      </div>
      <span className="min-w-0 max-w-full overflow-hidden text-ellipsis whitespace-nowrap text-right font-mono text-muted-foreground">
        {n} ({pct}%)
      </span>
    </div>
  )
}
