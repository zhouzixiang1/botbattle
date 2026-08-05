import { Fragment, useCallback, useEffect, useState } from 'react'
import { apiGet, apiJson, errMsg } from '../../api'
import { EmptyState, Loading, ErrorMsg, RefreshBtn, StatusBadge } from './ui'
import { useConfirm } from '@/hooks/use-confirm'
import Pagination from '@/components/Pagination'
import { fmtTime } from '@/lib/format'

interface Contest {
  id: number
  title: string
  organizer_id: number
  status: string
  hands_per_match?: number  // 已钉死固定值，不再展示；保留字段兼容 API 响应
  created_at: string
  starts_at: string | null
  ends_at: string | null
  registration_opens_at?: string | null
  registration_closes_at?: string | null
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
  open: 'published',
  published: 'running',
  running: 'finished',
  rest: 'finished',
  finished: 'cancelled',
}

export default function ContestsTab() {
  const [confirm, confirmDialog] = useConfirm()
  const [contests, setContests] = useState<Contest[]>([])
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [busyId, setBusyId] = useState<number | null>(null)
  const [expand, setExpand] = useState<number | null>(null)
  const [entries, setEntries] = useState<Entry[]>([])
  // 分页
  const [page, setPage] = useState(1)
  const [total, setTotal] = useState(0)
  const perPage = 50

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const d = await apiGet<{ contests: Contest[]; total?: number }>(
        `/api/admin/contests?page=${page}&per_page=${perPage}`,
      )
      setContests(d.contests || [])
      if (d.total !== undefined) setTotal(d.total)
    } catch (e) {
      setError(errMsg(e, '加载失败'))
    } finally {
      setLoading(false)
    }
  }, [page])

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
    if (!await confirm({
      title: '删除比赛',
      desc: `确认删除比赛 #${id}？`,
      confirmText: '删除',
      danger: true,
    })) return
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

  const loadEntries = async (cid: number) => {
    try {
      const d = await apiGet<{ entries: Entry[] }>(`/api/admin/contests/${cid}/entries`)
      setEntries(d.entries || [])
    } catch (e) {
      setError(errMsg(e, '加载报名失败'))
    }
  }

  const showEntries = async (c: Contest) => {
    if (expand === c.id) {
      setExpand(null)
      return
    }
    setExpand(c.id)
    setEntries([])
    await loadEntries(c.id)
  }

  const removeEntry = async (cid: number, uid: number) => {
    if (!await confirm({
      title: '移除报名',
      desc: `移除用户 #${uid} 的报名？`,
      confirmText: '移除',
      danger: true,
    })) return
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
        <span className="text-xs text-muted-foreground">
          共 {total || contests.length} 个比赛（切到 running 会真正调用 start）
        </span>
        <RefreshBtn onClick={load} />
      </div>
      <ErrorMsg msg={error} />

      <div className="overflow-x-auto rounded-xl border border-border bg-card">
        <table className="w-full min-w-[46rem] text-left text-sm">
          <thead className="border-b border-border bg-muted text-xs uppercase text-muted-foreground">
            <tr>
              <th className="px-3 py-2.5">ID</th>
              <th className="px-3 py-2.5">标题</th>
              <th className="px-3 py-2.5">模板/游戏</th>
              <th className="px-3 py-2.5">状态</th>
              <th className="px-3 py-2.5">时间编排</th>
              <th className="px-3 py-2.5">创建时间</th>
              <th className="px-3 py-2.5">操作</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {contests.map((c) => (
              <Fragment key={c.id}>
                <tr className="hover:bg-accent">
                  <td className="px-3 py-2 font-mono text-muted-foreground">{c.id}</td>
                  <td className="px-3 py-2 font-medium text-foreground">{c.title}</td>
                  <td className="px-3 py-2 font-mono text-xs text-muted-foreground">
                    {c.template_id || '—'} / {c.game_id || 'holdem'}
                  </td>
                  <td className="px-3 py-2">
                    <StatusBadge status={c.status} />
                  </td>
                  <td className="px-3 py-2 text-xs text-muted-foreground">
                    {c.registration_opens_at && <div>报名: {fmtTime(c.registration_opens_at)}</div>}
                    {c.registration_closes_at && <div>截止: {fmtTime(c.registration_closes_at)}</div>}
                    {c.starts_at && <div className="font-medium text-foreground">开赛: {fmtTime(c.starts_at)}</div>}
                    {!c.registration_opens_at && !c.registration_closes_at && !c.starts_at && <span>—</span>}
                  </td>
                  <td className="px-3 py-2 text-xs text-muted-foreground">{c.created_at}</td>
                  <td className="px-3 py-2">
                    <div className="flex flex-wrap gap-1">
                      {NEXT_STATUS[c.status] && (
                        <button
                          type="button"
                          disabled={busyId === c.id}
                          onClick={() => void patch(c.id, { status: NEXT_STATUS[c.status] })}
                          className="rounded border border-input bg-card px-2 py-0.5 text-xs text-muted-foreground hover:bg-accent"
                        >
                          推进到 {NEXT_STATUS[c.status]}
                        </button>
                      )}
                      {c.status === 'rest' && (
                        <button
                          type="button"
                          disabled={busyId === c.id}
                          onClick={() =>
                            void apiJson(`/api/contests/${c.id}/resume`, 'POST')
                              .then(load)
                              .catch((e) => setError(errMsg(e, '结束休息失败')))
                          }
                          className="rounded border border-primary/30 bg-card px-2 py-0.5 text-xs text-primary hover:bg-primary/10"
                        >
                          结束休息
                        </button>
                      )}
                      <button
                        type="button"
                        onClick={() => void showEntries(c)}
                        className="rounded border border-input bg-card px-2 py-0.5 text-xs text-muted-foreground hover:bg-accent"
                      >
                        报名
                      </button>
                      <button
                        type="button"
                        disabled={busyId === c.id}
                        onClick={() => void del(c.id)}
                        className="rounded border border-input bg-card px-2 py-0.5 text-xs text-destructive hover:bg-destructive/10"
                      >
                        删除
                      </button>
                    </div>
                  </td>
                </tr>
                {expand === c.id && (
                  <tr key={`${c.id}-e`} className="bg-muted/60">
                    <td colSpan={7} className="px-6 py-3">
                      {/* 批量指派（测试期 admin 派遣参赛者+Bot；正式版用户自己报名） */}
                      <AssignPanel contestId={c.id} gameId={c.game_id} onDone={() => void loadEntries(c.id)} />
                      {entries.length === 0 ? (
                        <EmptyState text="无报名" />
                      ) : (
                        <table className="w-full text-xs">
                          <thead className="text-muted-foreground">
                            <tr>
                              <th className="px-2 py-1 text-left">用户</th>
                              <th className="px-2 py-1 text-left">Bot</th>
                              <th className="px-2 py-1 text-left">报名时间</th>
                              <th className="px-2 py-1 text-left">操作</th>
                            </tr>
                          </thead>
                          <tbody className="divide-y divide-border">
                            {entries.map((e) => (
                              <tr key={e.id} className="font-mono text-muted-foreground">
                                <td className="px-2 py-1">#{e.user_id}</td>
                                <td className="px-2 py-1">#{e.bot_id}</td>
                                <td className="px-2 py-1">{e.registered_at}</td>
                                <td className="px-2 py-1">
                                  <button
                                    type="button"
                                    onClick={() => void removeEntry(c.id, e.user_id)}
                                    className="rounded border border-input bg-card px-2 py-0.5 text-destructive hover:bg-destructive/10"
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
              </Fragment>
            ))}
          </tbody>
        </table>
        {contests.length === 0 && <EmptyState text="无比赛" />}
      </div>
      <Pagination page={page} perPage={perPage} total={total} onPageChange={setPage} />
      {confirmDialog}
    </div>
  )
}

/** 批量指派面板（admin 派遣参赛者+Bot）。assign_all 模式按 game_id 全选，
 * 可选 name_prefix 过滤（如 "load_"/"cs_" 前缀的测试用户）。 */
function AssignPanel({ contestId, gameId, onDone }: { contestId: number; gameId?: string; onDone: () => void }) {
  const [prefix, setPrefix] = useState('')
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState('')

  const assignAll = async () => {
    setBusy(true); setMsg('')
    try {
      const d = await apiJson<{ added: number; skipped: string[]; total_entries: number }>(
        `/api/admin/contests/${contestId}/entries/bulk`, 'POST',
        { assign_all: true, game_id: gameId || 'holdem', name_prefix: prefix || undefined },
      )
      setMsg(`已指派 ${d.added} 人（共 ${d.total_entries} 报名${d.skipped.length ? `，跳过 ${d.skipped.length}` : ''}）`)
      onDone()
    } catch (e) {
      setMsg(errMsg(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="mb-3 flex flex-wrap items-center gap-2 rounded border border-border bg-card p-2 text-xs">
      <span className="font-medium text-foreground">批量指派：</span>
      <input
        type="text"
        placeholder="用户/Bot 名前缀（可选，如 load_）"
        value={prefix}
        onChange={(e) => setPrefix(e.target.value)}
        className="h-7 w-56 rounded border border-input bg-background px-2 text-foreground focus:outline-none focus:ring-2 focus:ring-ring"
      />
      <button
        type="button"
        onClick={() => void assignAll()}
        disabled={busy}
        className="rounded bg-primary px-3 py-1 font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
      >
        {busy ? '指派中…' : `指派全部 ${gameId || 'holdem'} 用户`}
      </button>
      {msg && <span className="text-muted-foreground">{msg}</span>}
    </div>
  )
}
