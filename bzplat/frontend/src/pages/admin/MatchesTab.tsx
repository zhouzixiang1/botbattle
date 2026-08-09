import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { apiGet, apiJson, errMsg } from '../../api'
import { Table, TableHeader, TableBody, TableHead, TableRow, TableCell, Badge, EmptyState, Loading, ErrorMsg, RefreshBtn, StatusBadge, Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from './ui'
import { useConfirm } from '@/hooks/use-confirm'
import Pagination from '@/components/Pagination'
import { fmtTime } from '@/lib/format'
import { gameLabel } from '@/lib/games'

interface Match {
  id: string
  bot_a_id: number | null
  bot_b_id: number | null
  bot_a_name?: string
  bot_b_name?: string
  game_id?: string
  status: string
  match_type: string
  result?: {
    hands_played?: number
    deltas?: number[]
    net_bb?: number
    technical_incidents_by_seat?: Record<string, number>
    technical_incident_samples?: Array<{ seat: number; error: string; turn?: number | null }>
  }
  reason: string
  created_at: string
  contest_id: number | null
}

const STATUSES = [
  { value: '', label: '全部状态' },
  { value: 'running', label: '进行中' },
  { value: 'pending', label: '排队中' },
  { value: 'completed', label: '已完成' },
  { value: 'aborted', label: '已中止' },
]

const QUALITY_FILTERS = [
  { value: '', label: '全部诊断结果' },
  { value: 'true', label: '含 Bot 技术故障' },
  { value: 'false', label: '不含 Bot 技术故障' },
]

const MATCH_TYPE_LABEL: Record<string, string> = {
  challenge: '挑战',
  ladder: '天梯',
  contest: '锦标赛',
  human: '人机',
  table: '房间',
}

const REASON_LABEL: Record<string, string> = {
  crash: 'Bot 运行中崩溃',
  technical_loss: 'Bot 技术判负',
  platform_error: '平台运行时故障',
  'admin-abort': '管理员中止',
  bot_deleted: 'Bot 已删除',
  protocol_error: 'Bot 响应协议错误',
}

function technicalIncidentCount(match: Match): number {
  return Object.values(match.result?.technical_incidents_by_seat || {})
    .reduce((sum, value) => sum + Number(value || 0), 0)
}

function technicalIncidentText(error: string): string {
  return error || 'Bot 技术故障'
}

export default function MatchesTab() {
  const [confirm, confirmDialog] = useConfirm()
  const [matches, setMatches] = useState<Match[]>([])
  const [status, setStatus] = useState('')
  const [hasTechnicalIncidents, setHasTechnicalIncidents] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [busyId, setBusyId] = useState<string | null>(null)
  // 分页（/api/matches 为公开端点，支持 limit/offset + 返回 total）
  const [page, setPage] = useState(1)
  const [total, setTotal] = useState(0)
  const perPage = 20

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const offset = (page - 1) * perPage
      const params = new URLSearchParams()
      if (status) params.set('status', status)
      if (hasTechnicalIncidents) params.set('has_technical_incidents', hasTechnicalIncidents)
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
  }, [status, hasTechnicalIncidents, page])

  // 状态筛选切换 → 回到第 1 页
  const onStatusChange = (v: string) => {
    setStatus(v === 'all' ? '' : v)
    setPage(1)
  }

  const onQualityChange = (v: string) => {
    setHasTechnicalIncidents(v === 'all' ? '' : v)
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
              <SelectItem key={s.value || 'all'} value={s.value || 'all'}>
                {s.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select value={hasTechnicalIncidents || 'all'} onValueChange={onQualityChange}>
          <SelectTrigger size="sm" className="h-9 w-[11rem]">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {QUALITY_FILTERS.map((item) => (
              <SelectItem key={item.value || 'all'} value={item.value || 'all'}>
                {item.label}
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
        <Table className="min-w-[48rem]">
          <TableHeader>
            <TableRow>
              <TableHead className="px-3 py-2.5">对局 ID</TableHead>
              <TableHead className="px-3 py-2.5">对阵</TableHead>
              <TableHead className="px-3 py-2.5">游戏 / 类型</TableHead>
              <TableHead className="px-3 py-2.5">状态</TableHead>
              <TableHead className="px-3 py-2.5">进度</TableHead>
              <TableHead className="px-3 py-2.5">结果 / 异常</TableHead>
              <TableHead className="px-3 py-2.5">时间</TableHead>
              <TableHead className="px-3 py-2.5">操作</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {matches.map((m) => {
              const incidentCount = technicalIncidentCount(m)
              const sample = m.result?.technical_incident_samples?.[0]
              return (
              <TableRow key={m.id} className={incidentCount > 0 ? 'bg-destructive/5 hover:bg-destructive/10' : 'hover:bg-accent'}>
                <TableCell className="px-3 py-2 font-mono text-xs text-muted-foreground">{m.id.slice(0, 16)}…</TableCell>
                <TableCell className="max-w-[16rem] px-3 py-2 text-foreground">
                  <div className="flex min-w-0 items-center gap-1 truncate">
                    {m.bot_a_id != null ? <Link to={`/bot/${m.bot_a_id}`} className="min-w-0 truncate text-primary hover:underline">{m.bot_a_name || `#${m.bot_a_id}`}</Link> : <span>已删除 Bot</span>}
                    <span className="shrink-0 text-muted-foreground">vs</span>
                    {m.bot_b_id != null ? <Link to={`/bot/${m.bot_b_id}`} className="min-w-0 truncate text-primary hover:underline">{m.bot_b_name || `#${m.bot_b_id}`}</Link> : <span>已删除 Bot</span>}
                  </div>
                </TableCell>
                <TableCell className="px-3 py-2 text-xs text-muted-foreground">
                  <div>{gameLabel(m.game_id || 'holdem')}</div>
                  <div>{MATCH_TYPE_LABEL[m.match_type] || m.match_type}</div>
                </TableCell>
                <TableCell className="px-3 py-2">
                  <StatusBadge status={m.status} />
                </TableCell>
                <TableCell className="px-3 py-2 font-mono text-xs text-muted-foreground">
                  {m.game_id === 'holdem' || !m.game_id
                    ? `${m.result?.hands_played ?? 0} / 70 手牌`
                    : `${m.result?.hands_played ?? 0} 步`}
                </TableCell>
                <TableCell className="max-w-[18rem] px-3 py-2 font-mono text-xs text-muted-foreground">
                  <div>{m.result?.deltas?.[0] ?? 0} / {m.result?.deltas?.[1] ?? 0}</div>
                  {m.reason && m.reason !== 'completed' && (
                    <div className="mt-1 text-[10px] text-destructive">
                      {REASON_LABEL[m.reason] || m.reason}
                    </div>
                  )}
                  {incidentCount > 0 && (
                    <div className="mt-1 space-y-0.5 text-[10px] text-destructive">
                      <Badge variant="destructive" className="text-[10px]">Bot 技术故障 {incidentCount} 次</Badge>
                      {sample && <div>座位 {sample.seat + 1} · {technicalIncidentText(sample.error)} · 回合 {sample.turn ?? '未知'}</div>}
                    </div>
                  )}
                </TableCell>
                <TableCell className="px-3 py-2 text-xs text-muted-foreground">{fmtTime(m.created_at)}</TableCell>
                <TableCell className="px-3 py-2">
                  <div className="flex gap-1">
                    <Link
                      to={`/match/${m.id}`}
                      className="inline-flex h-8 items-center rounded-md border border-input bg-background px-3 text-xs text-primary hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring"
                    >
                      查看
                    </Link>
                    {(m.status === 'running' || m.status === 'pending') && (
                      <button
                        type="button"
                        disabled={busyId === m.id}
                        onClick={() => void abort(m.id)}
                        className="inline-flex h-8 items-center rounded-md border border-destructive/30 bg-destructive/10 px-3 text-xs text-destructive hover:bg-destructive/20 focus-visible:ring-2 focus-visible:ring-ring"
                      >
                        中止
                      </button>
                    )}
                  </div>
                </TableCell>
              </TableRow>
              )
            })}
          </TableBody>
        </Table>
        {matches.length === 0 && <EmptyState text="暂无对局" />}
      </div>
      <Pagination page={page} perPage={perPage} total={total} onPageChange={setPage} />
      {confirmDialog}
    </div>
  )
}
