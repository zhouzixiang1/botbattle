import { useCallback, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { toast } from 'sonner'
import { apiFetch, apiJson, errMsg } from '@/api'
import { MetricCard, Card, CardHeader, CardTitle, EmptyState, Loading, ErrorMsg, RefreshBtn, Button } from './ui'
import {
  ExecutionQueuePanel,
  type ExecutionQueueSnapshot,
} from '@/components/execution-queue'
import { Switch } from '@/components/ui/switch'
import { useConfirm } from '@/hooks/use-confirm'
import { useSingleFlightPolling } from '@/hooks/use-single-flight-polling'
import { fmtTime } from '@/lib/format'

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

export default function Dashboard() {
  const [stats, setStats] = useState<Stats | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [queue, setQueue] = useState<ExecutionQueueSnapshot | null>(null)
  const [lastUpdatedAt, setLastUpdatedAt] = useState<number | null>(null)
  const [savingAutoMatch, setSavingAutoMatch] = useState(false)
  const requestRevision = useRef(0)
  const toggleInFlight = useRef(false)
  const [confirm, confirmDialog] = useConfirm()

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
    enabled: !savingAutoMatch,
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

  const toggleAutoMatch = async (enabled: boolean) => {
    if (toggleInFlight.current) return
    const revision = ++requestRevision.current
    toggleInFlight.current = true
    setSavingAutoMatch(true)
    try {
      const updated = await apiJson<ExecutionQueueSnapshot>(
        '/api/admin/auto-match',
        'PUT',
        { enabled },
      )
      if (revision !== requestRevision.current) return
      setQueue(updated)
      setLastUpdatedAt(Date.now())
      toast.success(enabled ? '自动排位生产已开启' : '自动排位生产已关闭，人工与赛事任务不受影响')
    } catch (e) {
      if (revision === requestRevision.current) {
        toast.error(errMsg(e, '更新自动排位总开关失败'))
      }
    } finally {
      toggleInFlight.current = false
      setSavingAutoMatch(false)
    }
  }

  const resumeQueue = async () => {
    if (toggleInFlight.current) return
    if (!await confirm({
      title: '清场并恢复执行队列',
      desc: '将删除当前平台实例标签下的所有容器，确认清零后补偿中断任务并恢复派发。',
      confirmText: '清场并恢复',
      danger: true,
    })) return
    const revision = ++requestRevision.current
    toggleInFlight.current = true
    setSavingAutoMatch(true)
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
      } else {
        toast.success('全局执行队列已恢复')
      }
    } catch (e) {
      toast.error(errMsg(e, '恢复全局执行队列失败'))
    } finally {
      toggleInFlight.current = false
      setSavingAutoMatch(false)
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
        <RefreshBtn onClick={refresh} className="min-h-11" />
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
        onRetry={refresh}
        maxQueued={6}
        compactOnMobile
        className="mt-5"
        action={queue ? (
          <div className="flex shrink-0 flex-wrap items-center justify-end gap-2">
            {queue.dispatcher.state === 'paused' && (
              <Button size="sm" className="min-h-11" variant="outline" disabled={savingAutoMatch} onClick={() => { void resumeQueue() }}>
                清场并恢复
              </Button>
            )}
            <div className="flex min-h-11 items-center gap-3 rounded-lg border border-border bg-background px-2.5 py-1.5">
              <div className="text-right">
                <div className="text-xs font-medium">自动排位</div>
                <div className="text-xs text-muted-foreground">仅控制自动任务生产</div>
              </div>
              <Switch
                checked={queue.dispatcher.auto_enabled}
                disabled={savingAutoMatch}
                onCheckedChange={(checked) => { void toggleAutoMatch(checked) }}
                aria-label="自动排位生产开关"
                className="relative before:absolute before:-inset-3 before:content-['']"
              />
            </div>
          </div>
        ) : null}
      />

      <div className="mt-5 grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader><CardTitle>最近注册用户</CardTitle></CardHeader>
          {stats.recent_users.length === 0 ? (
            <EmptyState text="暂无用户" />
          ) : (
            <ul className="divide-y divide-border">
              {stats.recent_users.map((u) => (
                <li key={u.id} className="flex items-center justify-between py-2 text-sm">
                  <Link
                    to={`/user/${encodeURIComponent(u.username)}`}
                    className="min-w-0 max-w-[10rem] truncate font-medium text-primary hover:underline"
                  >
                    {u.username}
                  </Link>
                  <span className="text-xs text-muted-foreground">
                    <span className="mr-2">{ROLE_LABEL[u.role] || u.role}</span>
                    {fmtTime(u.created_at)}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card>
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

function DistRow({ label, n, total, color }: { label: string; n: number; total: number; color: string }) {
  const pct = total > 0 ? Math.round((n / total) * 100) : 0
  return (
    <div className="flex items-center gap-2 text-xs">
      <span className="w-14 text-muted-foreground">{label}</span>
      <div className="h-2 flex-1 overflow-hidden rounded-full bg-muted">
        <div className={`h-full ${color}`} style={{ width: `${pct}%` }} />
      </div>
      <span className="w-20 text-right font-mono text-muted-foreground">
        {n} ({pct}%)
      </span>
    </div>
  )
}
