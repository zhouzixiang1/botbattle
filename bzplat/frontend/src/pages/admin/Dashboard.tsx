import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { toast } from 'sonner'
import { apiGet, apiJson, errMsg } from '@/api'
import { MetricCard, Card, CardHeader, CardTitle, EmptyState, Loading, ErrorMsg, RefreshBtn } from './ui'
import {
  AutoMatchQueuePanel,
  type AutoMatchQueueSnapshot,
} from '@/components/auto-match-queue'
import { Switch } from '@/components/ui/switch'
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

const ROLE_LABEL: Record<string, string> = {
  user: '普通用户',
  organizer: '组织者',
  admin: '管理员',
}

export default function Dashboard() {
  const [stats, setStats] = useState<Stats | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [queue, setQueue] = useState<AutoMatchQueueSnapshot | null>(null)
  const [savingAutoMatch, setSavingAutoMatch] = useState(false)
  const requestRevision = useRef(0)
  const toggleInFlight = useRef(false)

  const load = useCallback(async (showLoading = true) => {
    if (toggleInFlight.current) return
    const revision = ++requestRevision.current
    if (showLoading) setLoading(true)
    try {
      const [d, q] = await Promise.all([
        apiGet<Stats>('/api/admin/stats'),
        apiGet<AutoMatchQueueSnapshot>('/api/auto-match/queue'),
      ])
      if (revision === requestRevision.current) {
        setStats(d)
        setQueue(q)
        setError('')
      }
    } catch (e) {
      if (revision === requestRevision.current) setError(errMsg(e, '加载失败'))
    } finally {
      if (showLoading && revision === requestRevision.current) setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load(true)
    const timer = window.setInterval(() => { void load(false) }, 5_000)
    return () => window.clearInterval(timer)
  }, [load])

  const toggleAutoMatch = async (enabled: boolean) => {
    if (toggleInFlight.current || (!queue?.capability_enabled && enabled)) return
    const revision = ++requestRevision.current
    toggleInFlight.current = true
    setSavingAutoMatch(true)
    try {
      const updated = await apiJson<AutoMatchQueueSnapshot>(
        '/api/admin/auto-match',
        'PUT',
        { enabled },
      )
      if (revision !== requestRevision.current) return
      setQueue(updated)
      toast.success(enabled ? '自动排位已开启，队列将继续运行' : '自动排位已暂停，当前对局会正常结束')
    } catch (e) {
      if (revision === requestRevision.current) {
        toast.error(errMsg(e, '更新自动排位总开关失败'))
      }
    } finally {
      toggleInFlight.current = false
      setSavingAutoMatch(false)
    }
  }

  if (loading && !stats) return <Loading />
  if (error && !stats) return <ErrorMsg msg={error} />
  if (!stats) return <EmptyState text="无数据" />

  // running 是健康的活跃状态，不能计入异常；历史 Bot 响应异常在对局记录中单独展示。
  const abnormal = stats.matches_aborted

  return (
    <div>
      <div className="mb-3 flex items-center justify-between">
        <p className="text-sm text-muted-foreground">平台总览统计</p>
        <RefreshBtn onClick={() => { void load(true) }} />
      </div>
      <ErrorMsg msg={error} />

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
        <MetricCard label="用户" value={stats.users} hint={`活跃 ${stats.users_active}`} />
        <MetricCard label="Bot" value={stats.bots} hint={`活跃 ${stats.bots_active}`} />
        <MetricCard label="对局" value={stats.matches} hint={`完成 ${stats.matches_completed}`} />
        <MetricCard label="异常对局" value={abnormal} danger={abnormal > 0}
          hint={`已中止 ${stats.matches_aborted} · 运行中 ${stats.matches_running}`} />
        <MetricCard label="比赛" value={stats.contests} hint={`进行中 ${stats.contests_running}`} />
        <MetricCard label="在线会话" value={stats.active_sessions} />
      </div>

      <AutoMatchQueuePanel
        snapshot={queue}
        maxUpcoming={6}
        className="mt-5"
        action={queue ? (
          <div className="flex shrink-0 items-center gap-2 rounded-lg border border-border bg-background px-2.5 py-1.5">
            <div className="text-right">
              <div className="text-xs font-medium">总开关</div>
              <div className="text-[10px] text-muted-foreground">
                {queue.capability_enabled ? '生产调度能力可用' : 'QA 安全门已锁定'}
              </div>
            </div>
            <Switch
              checked={queue.enabled}
              disabled={savingAutoMatch || (!queue.capability_enabled && !queue.enabled)}
              onCheckedChange={(checked) => { void toggleAutoMatch(checked) }}
              aria-label="自动排位总开关"
            />
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
