import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { apiGet, apiJson, errMsg } from '../../api'
import { EmptyState, Loading, ErrorMsg, RefreshBtn, StatusBadge, Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from './ui'
import { useConfirm } from '@/hooks/use-confirm'
import Pagination from '@/components/Pagination'

interface Match {
  id: string
  bot_a_id: number
  bot_b_id: number
  bot_a_name?: string
  bot_b_name?: string
  status: string
  match_type: string
  match_config?: Record<string, number>
  result?: { hands_played?: number; deltas?: number[]; net_bb?: number }
  reason: string
  created_at: string
  contest_id: number | null
}

const STATUSES = ['', 'running', 'pending', 'completed', 'aborted']

export default function MatchesTab() {
  const [confirm, confirmDialog] = useConfirm()
  const [matches, setMatches] = useState<Match[]>([])
  const [status, setStatus] = useState('aborted')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [busyId, setBusyId] = useState<string | null>(null)
  // 分页（/api/matches 为公开端点，支持 limit/offset + 返回 total）
  const [page, setPage] = useState(1)
  const [total, setTotal] = useState(0)
  const perPage = 50

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const offset = (page - 1) * perPage
      const params = new URLSearchParams()
      if (status) params.set('status', status)
      params.set('limit', String(perPage))
      params.set('offset', String(offset))
      const d = await apiGet<{ matches: Match[]; total?: number }>(`/api/matches?${params.toString()}`)
      setMatches(d.matches || [])
      if (d.total !== undefined) setTotal(d.total)
    } catch (e) {
      setError(errMsg(e, '加载失败'))
    } finally {
      setLoading(false)
    }
  }, [status, page])

  // 状态筛选切换 → 回到第 1 页
  const onStatusChange = (v: string) => {
    setStatus(v === 'all' ? '' : v)
    setPage(1)
  }

  useEffect(() => {
    void load()
  }, [load])

  const abort = async (id: string) => {
    if (!await confirm({
      title: '中止对局',
      desc: `确认将对局 ${id} 标记为中止？`,
      confirmText: '中止',
      danger: true,
    })) return
    setBusyId(id)
    try {
      await apiJson(`/api/admin/matches/${id}`, 'PATCH', { status: 'aborted', reason: 'admin-abort' })
      await load()
    } catch (e) {
      setError(errMsg(e, '中止失败'))
    } finally {
      setBusyId(null)
    }
  }

  if (loading && !matches.length) return <Loading />
  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <Select value={status || 'all'} onValueChange={onStatusChange}>
          <SelectTrigger size="sm" className="h-9 w-[8.5rem]">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {STATUSES.map((s) => (
              <SelectItem key={s || 'all'} value={s || 'all'}>
                {s === '' ? '全部状态' : s}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <span className="text-xs text-muted-foreground">共 {total || matches.length} 局</span>
        <div className="ml-auto">
          <RefreshBtn onClick={load} />
        </div>
      </div>
      <ErrorMsg msg={error} />

      <div className="overflow-x-auto rounded-xl border border-border bg-card">
        <table className="w-full min-w-[52rem] text-left text-sm">
          <thead className="border-b border-border bg-muted text-xs uppercase text-muted-foreground">
            <tr>
              <th className="px-3 py-2.5">对局 ID</th>
              <th className="px-3 py-2.5">对阵</th>
              <th className="px-3 py-2.5">类型</th>
              <th className="px-3 py-2.5">状态</th>
              <th className="px-3 py-2.5">手数</th>
              <th className="px-3 py-2.5">盈亏</th>
              <th className="px-3 py-2.5">时间</th>
              <th className="px-3 py-2.5">操作</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {matches.map((m) => (
              <tr key={m.id} className="hover:bg-accent">
                <td className="px-3 py-2 font-mono text-xs text-muted-foreground">{m.id.slice(0, 16)}…</td>
                <td className="max-w-[16rem] px-3 py-2 text-foreground">
                  <div className="flex min-w-0 items-center gap-1 truncate">
                    <span className="min-w-0 truncate" title={m.bot_a_name || `#${m.bot_a_id}`}>
                      {m.bot_a_name || `#${m.bot_a_id}`}
                    </span>
                    <span className="shrink-0 text-muted-foreground">vs</span>
                    <span className="min-w-0 truncate" title={m.bot_b_name || `#${m.bot_b_id}`}>
                      {m.bot_b_name || `#${m.bot_b_id}`}
                    </span>
                  </div>
                </td>
                <td className="px-3 py-2 text-xs text-muted-foreground">{m.match_type}</td>
                <td className="px-3 py-2">
                  <StatusBadge status={m.status} />
                </td>
                <td className="px-3 py-2 font-mono text-xs text-muted-foreground">
                  {m.result?.hands_played ?? 0}/{m.match_config?.hands ?? '-'}
                </td>
                <td className="px-3 py-2 font-mono text-xs text-muted-foreground">
                  {m.result?.deltas?.[0] ?? 0}/{m.result?.deltas?.[1] ?? 0}
                  {m.reason && m.reason !== 'completed' && (
                    <div className="text-[10px] text-destructive">{m.reason}</div>
                  )}
                </td>
                <td className="px-3 py-2 text-xs text-muted-foreground">{m.created_at}</td>
                <td className="px-3 py-2">
                  <div className="flex gap-1">
                    <Link
                      to={`/match/${m.id}`}
                      className="rounded border border-input bg-card px-2 py-0.5 text-xs text-primary hover:bg-accent"
                    >
                      查看
                    </Link>
                    {(m.status === 'running' || m.status === 'pending') && (
                      <button
                        type="button"
                        disabled={busyId === m.id}
                        onClick={() => void abort(m.id)}
                        className="rounded border border-input bg-card px-2 py-0.5 text-xs text-destructive hover:bg-destructive/10"
                      >
                        中止
                      </button>
                    )}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {matches.length === 0 && <EmptyState text="无对局" />}
      </div>
      <Pagination page={page} perPage={perPage} total={total} onPageChange={setPage} />
      {confirmDialog}
    </div>
  )
}
