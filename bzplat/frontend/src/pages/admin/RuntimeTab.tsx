import { useCallback, useEffect, useState } from 'react'
import { apiGet, apiJson, errMsg } from '../../api'
import { ErrorMsg, Loading, RefreshBtn } from './ui'

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
        <p className="text-xs text-slate-400">
          机器 {rt?.cpu_count ?? '—'} 核 · 半负载最多 {rt?.ceiling ?? '—'} 场并发
        </p>
        <RefreshBtn onClick={load} />
      </div>
      <ErrorMsg msg={error} />
      {ok && <p className="text-sm text-emerald-600">{ok}</p>}

      <div className="rounded-xl border border-slate-200 bg-white p-4">
        <h3 className="text-sm font-medium text-slate-800">运行时</h3>
        <div className="mt-3 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          <label className="text-sm text-slate-600">
            决策超时（秒）
            <input
              type="number"
              min={1}
              max={300}
              className="mt-1 block w-full rounded-lg border border-slate-300 px-3 py-2"
              value={timeoutSec}
              onChange={(e) => setTimeoutSec(Number(e.target.value))}
            />
          </label>
          <label className="text-sm text-slate-600">
            最大并发对局（≤ {rt?.ceiling ?? 1}）
            <input
              type="number"
              min={1}
              max={rt?.ceiling ?? 1}
              className="mt-1 block w-full rounded-lg border border-slate-300 px-3 py-2"
              value={conc}
              onChange={(e) => setConc(Number(e.target.value))}
            />
          </label>
          <label className="text-sm text-slate-600">
            赛事默认休息（分钟）
            <input
              type="number"
              min={0}
              max={120}
              className="mt-1 block w-full rounded-lg border border-slate-300 px-3 py-2"
              value={restMin}
              onChange={(e) => setRestMin(Number(e.target.value))}
            />
          </label>
        </div>
        <div className="mt-4 flex flex-wrap gap-4 text-xs text-slate-500">
          <span>
            Bot CPU：<strong className="text-slate-700">{rt?.bot_cpus}</strong>（只读）
          </span>
          <span>
            Bot 内存：<strong className="text-slate-700">{rt?.bot_memory_mb} MB</strong>（只读）
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
          className="mt-4 rounded-lg bg-brand-600 px-4 py-2 text-sm text-white hover:bg-brand-500 disabled:opacity-50"
        >
          保存
        </button>
      </div>

      <div className="rounded-xl border border-slate-200 bg-white p-4">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-medium text-slate-800">闲时自动对局</h3>
          <label className="flex items-center gap-2 text-sm text-slate-600">
            <input
              type="checkbox"
              className="h-4 w-4 rounded border-slate-300"
              checked={amEnabled}
              onChange={(e) => setAmEnabled(e.target.checked)}
            />
            启用
          </label>
        </div>
        <p className="mt-1 text-xs text-slate-400">
          系统空闲时自动安排 bot 对战以维护天梯榜（陈旧度优先 + rating 就近配对）。
        </p>
        <div className="mt-3 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          <label className="text-sm text-slate-600">
            轮询间隔（秒）
            <input
              type="number" min={1} max={3600}
              className="mt-1 block w-full rounded-lg border border-slate-300 px-3 py-2"
              value={amInterval} onChange={(e) => setAmInterval(Number(e.target.value))}
            />
          </label>
          <label className="text-sm text-slate-600">
            连续空闲触发（秒）
            <input
              type="number" min={0} max={600}
              className="mt-1 block w-full rounded-lg border border-slate-300 px-3 py-2"
              value={amMinIdle} onChange={(e) => setAmMinIdle(Number(e.target.value))}
            />
          </label>
          <label className="text-sm text-slate-600">
            同 Bot 冷却（秒）
            <input
              type="number" min={0} max={86400}
              className="mt-1 block w-full rounded-lg border border-slate-300 px-3 py-2"
              value={amCooldown} onChange={(e) => setAmCooldown(Number(e.target.value))}
            />
          </label>
          <label className="text-sm text-slate-600">
            陈旧阈值（秒）
            <input
              type="number" min={60} max={604800}
              className="mt-1 block w-full rounded-lg border border-slate-300 px-3 py-2"
              value={amStale} onChange={(e) => setAmStale(Number(e.target.value))}
            />
          </label>
          <label className="text-sm text-slate-600">
            预留挑战槽（≤ {rt?.ceiling ?? 1}）
            <input
              type="number" min={0} max={rt?.ceiling ?? 1}
              className="mt-1 block w-full rounded-lg border border-slate-300 px-3 py-2"
              value={amReserve} onChange={(e) => setAmReserve(Number(e.target.value))}
            />
          </label>
        </div>
        <p className="mt-3 text-xs text-slate-500">
          状态：进行中 {rt?.queue.running ?? 0} · 生效并发 {rt?.effective_concurrent ?? '—'}
        </p>
      </div>

      <div className="rounded-xl border border-slate-200 bg-white p-4">
        <h3 className="text-sm font-medium text-slate-800">赛事模板</h3>
        <ul className="mt-3 divide-y divide-slate-100 text-sm">
          {templates.map((t) => (
            <li key={t.id} className="flex flex-wrap items-baseline justify-between gap-2 py-2">
              <span className="font-medium text-slate-700">{t.name}</span>
              <span className="font-mono text-xs text-slate-400">
                {t.id} · {t.game_id} · {(t.stages || []).length} 阶段
              </span>
            </li>
          ))}
          {templates.length === 0 && (
            <li className="py-4 text-slate-400">无模板</li>
          )}
        </ul>
      </div>
    </div>
  )
}
