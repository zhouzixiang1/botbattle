import { useCallback, useEffect, useState } from 'react'
import { apiGet, apiJson, errMsg } from '../../api'
import { ErrorMsg, Loading, RefreshBtn, Switch, inp } from './ui'

interface Runtime {
  cpu_count: number
  ceiling: number
  action_timeout_sec: number
  max_concurrent_matches: number
  effective_concurrent: number
  bot_cpus: number
  bot_memory_mb: number
  contest_default_rest_minutes: number
  queue: { pending: number; running: number }
  auto_match: {
    enabled: boolean
    interval_sec: number
    min_idle_sec: number
    bot_cooldown: number
    stale_sec: number
    reserve_slots: number
    placement_games: number
    max_per_round: number
    daily_cap: number
    daily_count: number
  }
}

interface Template {
  id: string
  name: string
  game_id: string
  stages: unknown[]
}

export default function RuntimeTab() {
  const [rt, setRt] = useState<Runtime | null>(null)
  const [templates, setTemplates] = useState<Template[]>([])
  const [timeoutSec, setTimeoutSec] = useState(60)
  const [conc, setConc] = useState(1)
  const [restMin, setRestMin] = useState(10)
  const [amEnabled, setAmEnabled] = useState(true)
  const [amInterval, setAmInterval] = useState(30)
  const [amMinIdle, setAmMinIdle] = useState(5)
  const [amCooldown, setAmCooldown] = useState(600)
  const [amStale, setAmStale] = useState(3600)
  const [amReserve, setAmReserve] = useState(1)
  const [amPlacement, setAmPlacement] = useState(10)
  const [amMaxPerRound, setAmMaxPerRound] = useState(2)
  const [amDailyCap, setAmDailyCap] = useState(200)
  const [error, setError] = useState('')
  const [ok, setOk] = useState('')
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const [r, t] = await Promise.all([
        apiGet<Runtime>('/api/admin/settings/runtime'),
        apiGet<{ templates: Template[] }>('/api/admin/settings/templates'),
      ])
      setRt(r)
      setTimeoutSec(r.action_timeout_sec)
      setConc(r.max_concurrent_matches)
      setRestMin(r.contest_default_rest_minutes)
      const am = r.auto_match
      setAmEnabled(am.enabled)
      setAmInterval(am.interval_sec)
      setAmMinIdle(am.min_idle_sec)
      setAmCooldown(am.bot_cooldown)
      setAmStale(am.stale_sec)
      setAmReserve(am.reserve_slots)
      setAmPlacement(am.placement_games)
      setAmMaxPerRound(am.max_per_round)
      setAmDailyCap(am.daily_cap)
      setTemplates(t.templates || [])
    } catch (e) {
      setError(errMsg(e, '加载失败'))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const save = async () => {
    setBusy(true)
    setError('')
    setOk('')
    try {
      await apiJson('/api/admin/settings/runtime', 'PATCH', {
        action_timeout_sec: timeoutSec,
        max_concurrent_matches: conc,
        contest_default_rest_minutes: restMin,
        auto_match_enabled: amEnabled,
        auto_match_interval_sec: amInterval,
        auto_match_min_idle_sec: amMinIdle,
        auto_match_bot_cooldown: amCooldown,
        auto_match_stale_sec: amStale,
        auto_match_reserve_slots: amReserve,
        auto_match_placement_games: amPlacement,
        auto_match_max_per_round: amMaxPerRound,
        auto_match_daily_cap: amDailyCap,
      })
      setOk('已保存并热更新')
      await load()
    } catch (e) {
      setError(errMsg(e, '保存失败'))
    } finally {
      setBusy(false)
    }
  }

  if (loading && !rt) return <Loading />
  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <p className="text-xs text-muted-foreground">
          机器 {rt?.cpu_count ?? '—'} 核 · 半负载最多 {rt?.ceiling ?? '—'} 场并发
        </p>
        <RefreshBtn onClick={load} />
      </div>
      <ErrorMsg msg={error} />
      {ok && <p className="text-sm text-success">{ok}</p>}

      <div className="rounded-xl border border-border bg-card p-4">
        <h3 className="text-sm font-medium text-foreground">运行时</h3>
        <div className="mt-3 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          <label className="text-sm text-muted-foreground">
            决策超时（秒）
            <input
              type="number"
              min={1}
              max={300}
              className={inp}
              value={timeoutSec}
              onChange={(e) => setTimeoutSec(Number(e.target.value))}
            />
          </label>
          <label className="text-sm text-muted-foreground">
            最大并发对局（≤ {rt?.ceiling ?? 1}）
            <input
              type="number"
              min={1}
              max={rt?.ceiling ?? 1}
              className={inp}
              value={conc}
              onChange={(e) => setConc(Number(e.target.value))}
            />
          </label>
          <label className="text-sm text-muted-foreground">
            赛事默认休息（分钟）
            <input
              type="number"
              min={0}
              max={120}
              className={inp}
              value={restMin}
              onChange={(e) => setRestMin(Number(e.target.value))}
            />
          </label>
        </div>
        <div className="mt-4 flex flex-wrap gap-4 text-xs text-muted-foreground">
          <span>
            Bot CPU：<strong className="text-foreground">{rt?.bot_cpus}</strong>（只读）
          </span>
          <span>
            Bot 内存：<strong className="text-foreground">{rt?.bot_memory_mb} MB</strong>（只读）
          </span>
          <span>
            排队中：{rt?.queue.pending ?? 0} · 进行中：{rt?.queue.running ?? 0}
          </span>
          <span>生效并发：{rt?.effective_concurrent}</span>
        </div>
        <button
          type="button"
          disabled={busy}
          onClick={() => void save()}
          className="mt-4 rounded-lg bg-primary px-4 py-2 text-sm text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
        >
          保存
        </button>
      </div>

      <div className="rounded-xl border border-border bg-card p-4">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-medium text-foreground">闲时自动对局</h3>
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <Switch checked={amEnabled} onCheckedChange={setAmEnabled} />
            启用
          </div>
        </div>
        <p className="mt-1 text-xs text-muted-foreground">
          系统空闲时自动安排 bot 对战以维护天梯榜（陈旧度优先 + rating 就近配对）。
        </p>
        <div className="mt-3 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          <label className="text-sm text-muted-foreground">
            轮询间隔（秒）
            <input
              type="number" min={1} max={3600}
              className={inp}
              value={amInterval} onChange={(e) => setAmInterval(Number(e.target.value))}
            />
          </label>
          <label className="text-sm text-muted-foreground">
            连续空闲触发（秒）
            <input
              type="number" min={0} max={600}
              className={inp}
              value={amMinIdle} onChange={(e) => setAmMinIdle(Number(e.target.value))}
            />
          </label>
          <label className="text-sm text-muted-foreground">
            同 Bot 冷却（秒）
            <input
              type="number" min={0} max={86400}
              className={inp}
              value={amCooldown} onChange={(e) => setAmCooldown(Number(e.target.value))}
            />
          </label>
          <label className="text-sm text-muted-foreground">
            陈旧阈值（秒，0=不限）
            <input
              type="number" min={0} max={604800}
              className={inp}
              value={amStale} onChange={(e) => setAmStale(Number(e.target.value))}
            />
          </label>
          <label className="text-sm text-muted-foreground">
            预留挑战槽（≤ {rt?.ceiling ?? 1}）
            <input
              type="number" min={0} max={rt?.ceiling ?? 1}
              className={inp}
              value={amReserve} onChange={(e) => setAmReserve(Number(e.target.value))}
            />
          </label>
          <label className="text-sm text-muted-foreground">
            新 Bot 定级赛场次（前N场优先，0=禁用）
            <input
              type="number" min={0} max={100}
              className={inp}
              value={amPlacement} onChange={(e) => setAmPlacement(Number(e.target.value))}
            />
          </label>
          <label className="text-sm text-muted-foreground">
            每轮最多补几场
            <input
              type="number" min={1} max={50}
              className={inp}
              value={amMaxPerRound} onChange={(e) => setAmMaxPerRound(Number(e.target.value))}
            />
          </label>
          <label className="text-sm text-muted-foreground">
            每日总量上限（0=不限）
            <input
              type="number" min={0} max={100000}
              className={inp}
              value={amDailyCap} onChange={(e) => setAmDailyCap(Number(e.target.value))}
            />
          </label>
        </div>
        <p className="mt-3 text-xs text-muted-foreground">
          状态：进行中 {rt?.queue.running ?? 0} · 生效并发 {rt?.effective_concurrent ?? '—'} ·
          今日后台对局 {rt?.auto_match.daily_count ?? 0}/{rt?.auto_match.daily_cap ?? '—'}
        </p>
      </div>

      <div className="rounded-xl border border-border bg-card p-4">
        <h3 className="text-sm font-medium text-foreground">赛事模板</h3>
        <ul className="mt-3 divide-y divide-border text-sm">
          {templates.map((t) => (
            <li key={t.id} className="flex flex-wrap items-baseline justify-between gap-2 py-2">
              <span className="font-medium text-foreground">{t.name}</span>
              <span className="font-mono text-xs text-muted-foreground">
                {t.id} · {t.game_id} · {(t.stages || []).length} 阶段
              </span>
            </li>
          ))}
          {templates.length === 0 && (
            <li className="py-4 text-muted-foreground">无模板</li>
          )}
        </ul>
      </div>
    </div>
  )
}
