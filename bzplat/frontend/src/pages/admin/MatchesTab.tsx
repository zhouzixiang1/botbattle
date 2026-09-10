import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { apiGet, apiJson, errMsg } from '../../api'
import { MatchNatureBadge, MatchParticipants } from '@/components/MatchParticipants'
import { MatchOutcome } from '@/components/MatchOutcome'
import { Button, Table, TableHeader, TableBody, TableHead, TableRow, TableCell, Badge, EmptyState, Loading, ErrorMsg, RefreshBtn, StatusBadge, Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from './ui'
import { useConfirm } from '@/hooks/use-confirm'
import Pagination from '@/components/Pagination'
import { fmtTime } from '@/lib/format'
import { OverflowText } from '@/components/ui/overflow-text'
import { findGame, gameLabel, resolveTerminalReason } from '@/games'
import type { MatchParticipantSource } from '@/lib/match-participants'
import { isPublicMatchOutcome, type MatchOutcomeSource } from '@/lib/match-outcome'
import { outcomeSeatLabels } from '@/lib/match-seats'

interface Match extends MatchParticipantSource, MatchOutcomeSource {
  id: string
  bot_a_id: number | null
  bot_b_id: number | null
  game_id?: string
  status: string
  match_type: string
  result?: {
    rounds_played?: number
    deltas?: number[]
    normalized_delta?: number
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

function technicalIncidentCount(match: Match): number {
  return Object.values(match.result?.technical_incidents_by_seat || {})
    .reduce((sum, value) => sum + Number(value || 0), 0)
}

function technicalIncidentText(error: string): string {
  return error || 'Bot 技术故障'
}

function progressLabel(match: Match, unit: '步' | '手'): string {
  if (isPublicMatchOutcome(match.outcome) && match.outcome.kind === 'duplicate') {
    return `${match.outcome.completed_games}/${match.outcome.planned_games} 场计分 · ${match.outcome.rounds_played} ${unit}`
  }
  return `${match.result?.rounds_played ?? 0} ${unit}`
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
      await apiJson(`/api/admin/matches/${id}`, 'PATCH', { status: 'aborted' })
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
      <div className="mb-1.5 flex flex-wrap items-center gap-2">
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
        <span className="text-xs text-muted-foreground">共 {total || matches.length} 条对局记录</span>
        <div className="ml-auto">
          <RefreshBtn onClick={load} />
        </div>
      </div>
      <ErrorMsg msg={error} />

      <div className="overflow-x-auto rounded-xl border border-border bg-card">
        <Table className="min-w-[48rem]">
          <TableHeader>
            <TableRow>
              <TableHead className="h-9 px-2 py-1.5">对局 ID</TableHead>
              <TableHead className="h-9 px-2 py-1.5">对阵</TableHead>
              <TableHead className="h-9 px-2 py-1.5">游戏 / 类型</TableHead>
              <TableHead className="h-9 px-2 py-1.5">状态</TableHead>
              <TableHead className="h-9 px-2 py-1.5">进度</TableHead>
              <TableHead className="h-9 px-2 py-1.5">结果 / 异常</TableHead>
              <TableHead className="h-9 px-2 py-1.5">时间</TableHead>
              <TableHead className="h-9 px-2 py-1.5">操作</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {matches.map((m) => {
              const incidentCount = technicalIncidentCount(m)
              const sample = m.result?.technical_incident_samples?.[0]
              const gameSpec = findGame(m.game_id)
              const terminalReason = gameSpec
                ? gameSpec.terminalReason(m.reason, m.status)
                : resolveTerminalReason(m.reason, m.status)
              const hasTerminalStatus = m.status === 'completed' || m.status === 'aborted'
              return (
              <TableRow key={m.id} className={incidentCount > 0 ? 'bg-destructive/5 hover:bg-destructive/10' : 'hover:bg-accent'}>
                <TableCell className="w-[5.5rem] px-2 py-1 font-mono text-[11px] text-muted-foreground">
                  <OverflowText tooltip={m.id} tooltipFocusable={false}>{`${m.id.slice(0, 12)}…`}</OverflowText>
                </TableCell>
                {/* min-w-40 防止 auto 表格布局在窄视口把对阵列压缩到只剩省略号；
                    不足的宽度交给外层 overflow-x-auto 滚动区消化。 */}
                <TableCell className="w-72 min-w-40 whitespace-normal px-2 py-1 text-foreground">
                  <MatchParticipants
                    source={m}
                    className="[&_[data-match-participant]]:py-0 [&_[data-match-participant]>div+div]:mt-0"
                  />
                </TableCell>
                <TableCell className="w-24 whitespace-normal px-2 py-1 text-xs text-muted-foreground">
                  <div>{gameLabel(m.game_id)}</div>
                  <MatchNatureBadge matchType={m.match_type} source={m} className="mt-1" />
                </TableCell>
                <TableCell className="2xl:w-20 px-2 py-1">
                  <StatusBadge status={m.status} />
                </TableCell>
                <TableCell className="w-24 whitespace-normal px-2 py-1 font-mono text-xs text-muted-foreground">
                  {gameSpec
                    ? progressLabel(m, gameSpec.progressUnit === 'move' ? '步' : '手')
                    : '规则不可用'}
                </TableCell>
                {/* 宽屏去掉结果列固定宽度：让内容最长的赛果/异常列吸收余量，避免折行推高行高 */}
                <TableCell className="w-60 whitespace-normal 2xl:w-auto px-2 py-1 font-mono text-xs text-muted-foreground">
                  {/* 宽屏结果列很宽：primary/secondary/逐场计分横向排布，压平 4 行结构 */}
                  <MatchOutcome
                    source={m}
                    seatLabels={outcomeSeatLabels(m)}
                    normalizedUnit={m.game_id === 'holdem' ? 'BB' : undefined}
                    showGames
                    className="flex min-w-0 flex-wrap items-baseline gap-x-3 gap-y-0.5 font-sans leading-tight"
                  />
                  <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5">
                    <span className="whitespace-nowrap">原始分差 {m.result?.deltas?.[0] ?? 0} / {m.result?.deltas?.[1] ?? 0}</span>
                    {hasTerminalStatus && m.reason && (
                      <span
                        data-testid="terminal-reason"
                        data-tone={terminalReason.tone}
                        className={`text-[10px] ${terminalReason.tone === 'danger' ? 'text-destructive' : 'text-muted-foreground'}`}
                      >
                        {terminalReason.label}
                      </span>
                    )}
                  </div>
                  {incidentCount > 0 && (
                    <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[10px] text-destructive">
                      <Badge variant="destructive" className="text-[10px]">Bot 技术故障 {incidentCount} 次</Badge>
                      {sample && <span>座位 {sample.seat + 1} · {technicalIncidentText(sample.error)} · 回合 {sample.turn ?? '未知'}</span>}
                    </div>
                  )}
                </TableCell>
                <TableCell className="2xl:w-32 whitespace-nowrap px-2 py-1 text-[11px] text-muted-foreground">{fmtTime(m.created_at)}</TableCell>
                <TableCell className="w-16 whitespace-nowrap px-2 py-1">
                  <div className="flex items-center gap-1 whitespace-nowrap">
                    <Button asChild variant="outline" size="sm" className="max-lg:min-h-11 px-2.5"><Link to={`/match/${m.id}`}>查看</Link></Button>
                    {(m.status === 'running' || m.status === 'pending') && (
                      <Button
                        type="button"
                        variant="destructive"
                        size="sm"
                        className="max-lg:min-h-11 px-2.5"
                        disabled={busyId === m.id}
                        onClick={() => void abort(m.id)}
                      >
                        中止
                      </Button>
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
