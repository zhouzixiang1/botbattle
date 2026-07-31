import { useCallback, useEffect, useState } from 'react'
import { apiGet, apiJson, errMsg } from '../../api'
import { EmptyState, Loading, ErrorMsg, RefreshBtn, StatusBadge } from './ui'

interface Contest {
  id: number
  title: string
  organizer_id: number
  status: string
  hands_per_match: number
  created_at: string
  starts_at: string | null
  ends_at: string | null
  template_id?: string
  game_id?: string
}
interface Entry {
  id: number
  contest_id: number
  user_id: number
  bot_id: number
  registered_at: string
}

const NEXT_STATUS: Record<string, string> = {
  draft: 'open',
  open: 'running',
  running: 'finished',
  rest: 'finished',
  finished: 'cancelled',
}

export default function ContestsTab() {
  const [contests, setContests] = useState<Contest[]>([])
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [busyId, setBusyId] = useState<number | null>(null)
  const [expand, setExpand] = useState<number | null>(null)
  const [entries, setEntries] = useState<Entry[]>([])

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const d = await apiGet<{ contests: Contest[] }>('/api/admin/contests')
      setContests(d.contests || [])
    } catch (e) {
      setError(errMsg(e, '加载失败'))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const patch = async (id: number, fields: Record<string, unknown>) => {
    setBusyId(id)
    setError('')
    try {
      await apiJson(`/api/admin/contests/${id}`, 'PATCH', fields)
      await load()
    } catch (e) {
      setError(errMsg(e, '操作失败'))
    } finally {
      setBusyId(null)
    }
  }

  const del = async (id: number) => {
    if (!confirm(`确认删除比赛 #${id}？`)) return
    setBusyId(id)
    try {
      await apiJson(`/api/admin/contests/${id}`, 'DELETE')
      await load()
    } catch (e) {
      setError(errMsg(e, '删除失败'))
    } finally {
      setBusyId(null)
    }
  }

  const showEntries = async (c: Contest) => {
    if (expand === c.id) {
      setExpand(null)
      return
    }
    setExpand(c.id)
    setEntries([])
    try {
      const d = await apiGet<{ entries: Entry[] }>(`/api/admin/contests/${c.id}/entries`)
      setEntries(d.entries || [])
    } catch (e) {
      setError(errMsg(e, '加载报名失败'))
    }
  }

  const removeEntry = async (cid: number, uid: number) => {
    if (!confirm(`移除用户 #${uid} 的报名？`)) return
    try {
      await apiJson(`/api/admin/contests/${cid}/entries/${uid}`, 'DELETE')
      const d = await apiGet<{ entries: Entry[] }>(`/api/admin/contests/${cid}/entries`)
      setEntries(d.entries || [])
    } catch (e) {
      setError(errMsg(e, '移除失败'))
    }
  }

  if (loading && !contests.length) return <Loading />
  return (
    <div>
      <div className="mb-3 flex items-center justify-between">
        <span className="text-xs text-slate-400">
          共 {contests.length} 个比赛（→ running 会真正调用 start）
        </span>
        <RefreshBtn onClick={load} />
      </div>
      <ErrorMsg msg={error} />

      <div className="overflow-x-auto rounded-xl border border-slate-200 bg-white">
        <table className="w-full min-w-[46rem] text-left text-sm">
          <thead className="border-b border-slate-200 bg-slate-50 text-xs uppercase text-slate-400">
            <tr>
              <th className="px-3 py-2.5">ID</th>
              <th className="px-3 py-2.5">标题</th>
              <th className="px-3 py-2.5">模板/游戏</th>
              <th className="px-3 py-2.5">状态</th>
              <th className="px-3 py-2.5">手数</th>
              <th className="px-3 py-2.5">创建时间</th>
              <th className="px-3 py-2.5">操作</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {contests.map((c) => (
              <>
                <tr key={c.id} className="hover:bg-slate-50">
                  <td className="px-3 py-2 font-mono text-slate-400">{c.id}</td>
                  <td className="px-3 py-2 font-medium text-slate-700">{c.title}</td>
                  <td className="px-3 py-2 font-mono text-xs text-slate-500">
                    {c.template_id || '—'} / {c.game_id || 'holdem'}
                  </td>
                  <td className="px-3 py-2">
                    <StatusBadge status={c.status} />
                  </td>
                  <td className="px-3 py-2 font-mono text-xs text-slate-500">{c.hands_per_match}</td>
                  <td className="px-3 py-2 text-xs text-slate-400">{c.created_at}</td>
                  <td className="px-3 py-2">
                    <div className="flex flex-wrap gap-1">
                      {NEXT_STATUS[c.status] && (
                        <button
                          type="button"
                          disabled={busyId === c.id}
                          onClick={() => void patch(c.id, { status: NEXT_STATUS[c.status] })}
                          className="rounded border border-slate-300 bg-white px-2 py-0.5 text-xs text-slate-600 hover:bg-slate-100"
                        >
                          → {NEXT_STATUS[c.status]}
                        </button>
                      )}
                      {c.status === 'rest' && (
                        <button
                          type="button"
                          disabled={busyId === c.id}
                          onClick={() =>
                            void apiJson(`/api/contests/${c.id}/resume`, 'POST').then(load)
                          }
                          className="rounded border border-brand-300 bg-white px-2 py-0.5 text-xs text-brand-700 hover:bg-brand-50"
                        >
                          结束休息
                        </button>
                      )}
                      <button
                        type="button"
                        onClick={() => void showEntries(c)}
                        className="rounded border border-slate-300 bg-white px-2 py-0.5 text-xs text-slate-600 hover:bg-slate-100"
                      >
                        报名
                      </button>
                      <button
                        type="button"
                        disabled={busyId === c.id}
                        onClick={() => void del(c.id)}
                        className="rounded border border-slate-300 bg-white px-2 py-0.5 text-xs text-error-500 hover:bg-error-50"
                      >
                        删除
                      </button>
                    </div>
                  </td>
                </tr>
                {expand === c.id && (
                  <tr key={`${c.id}-e`} className="bg-slate-50/60">
                    <td colSpan={7} className="px-6 py-3">
                      {entries.length === 0 ? (
                        <EmptyState text="无报名" />
                      ) : (
                        <table className="w-full text-xs">
                          <thead className="text-slate-400">
                            <tr>
                              <th className="px-2 py-1 text-left">用户</th>
                              <th className="px-2 py-1 text-left">Bot</th>
                              <th className="px-2 py-1 text-left">报名时间</th>
                              <th className="px-2 py-1 text-left">操作</th>
                            </tr>
                          </thead>
                          <tbody className="divide-y divide-slate-200">
                            {entries.map((e) => (
                              <tr key={e.id} className="font-mono text-slate-600">
                                <td className="px-2 py-1">#{e.user_id}</td>
                                <td className="px-2 py-1">#{e.bot_id}</td>
                                <td className="px-2 py-1">{e.registered_at}</td>
                                <td className="px-2 py-1">
                                  <button
                                    type="button"
                                    onClick={() => void removeEntry(c.id, e.user_id)}
                                    className="rounded border border-slate-300 bg-white px-2 py-0.5 text-error-500 hover:bg-error-50"
                                  >
                                    移除
                                  </button>
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      )}
                    </td>
                  </tr>
                )}
              </>
            ))}
          </tbody>
        </table>
        {contests.length === 0 && <EmptyState text="无比赛" />}
      </div>
    </div>
  )
}
