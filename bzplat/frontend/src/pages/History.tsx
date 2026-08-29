import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { ListFilter, Swords } from 'lucide-react'

import { apiGet, errMsg } from '@/api'
import { DataRegion, PageFrame, PageHeader, StickyToolbar } from '@/components/layout'
import { MatchOutcome } from '@/components/MatchOutcome'
import { MatchNatureBadge, MatchParticipants } from '@/components/MatchParticipants'
import Pagination from '@/components/Pagination'
import { EmptyState, ErrorMsg, Loading, StatusBadge } from '@/components/ui/status'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { fmtTime } from '@/lib/format'
import { GAMES, gameLabel } from '@/lib/games'
import type { MatchParticipantSource } from '@/lib/match-participants'
import type { MatchOutcomeSource } from '@/lib/match-outcome'
import { outcomeSeatLabels } from '@/lib/match-seats'

interface Match extends MatchParticipantSource, MatchOutcomeSource {
  id: string
  status: string
  bot_a_id: number | null
  bot_b_id: number | null
  created_at?: string
  result?: { rounds_played?: number; deltas?: number[]; normalized_delta?: number }
  match_type?: string
  game_id?: string
  contest_id?: number | null
}

const PAGE_SIZE = 20

export default function History() {
  const [matches, setMatches] = useState<Match[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [status, setStatus] = useState('')
  const [gameId, setGameId] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    const offset = (page - 1) * PAGE_SIZE
    const params = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(offset) })
    if (status) params.set('status', status)
    if (gameId) params.set('game_id', gameId)
    setLoading(true)
    setError('')
    apiGet<{ matches: Match[]; total: number }>(`/api/matches?${params}`)
      .then((d) => {
        setMatches(d.matches || [])
        setTotal(d.total ?? 0)
      })
      .catch((e) => setError(errMsg(e)))
      .finally(() => setLoading(false))
  }, [status, gameId, page])

  const onStatus = (value: string) => {
    setStatus(value)
    setPage(1)
  }
  const onGame = (value: string) => {
    setGameId(value)
    setPage(1)
  }

  return (
    <PageFrame layout="public-history">
      <PageHeader
        title="对局历史"
        description="查看双方用户、Bot 或真人身份以及对局性质，并按状态与游戏定位回放。"
      />

      <StickyToolbar label="对局历史筛选">
        <div className="flex min-w-0 items-center gap-2">
          <ListFilter aria-hidden="true" className="size-4 shrink-0 text-muted-foreground" />
          <span className="shrink-0 text-xs font-medium text-muted-foreground">状态</span>
          <Select value={status || 'all'} onValueChange={(value) => onStatus(value === 'all' ? '' : value)}>
            <SelectTrigger className="w-[7.75rem] max-w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">全部状态</SelectItem>
              <SelectItem value="pending">排队中</SelectItem>
              <SelectItem value="running">进行中</SelectItem>
              <SelectItem value="completed">已完成</SelectItem>
              <SelectItem value="aborted">已中止</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div className="flex min-w-0 items-center gap-2">
          <span className="shrink-0 text-xs font-medium text-muted-foreground">游戏</span>
          <Select value={gameId || 'all'} onValueChange={(value) => onGame(value === 'all' ? '' : value)}>
            <SelectTrigger className="w-[7.75rem] max-w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">全部游戏</SelectItem>
              {GAMES.map((game) => (
                <SelectItem key={game.id} value={game.id}>{game.label}</SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <span className="ml-auto shrink-0 text-xs text-muted-foreground">第 {page} 页</span>
      </StickyToolbar>

      <DataRegion
        title={`对局记录 · 共 ${total} 条`}
        description={gameId || status ? `已应用筛选条件 · 每页 ${PAGE_SIZE} 条` : `按创建时间查看 · 每页 ${PAGE_SIZE} 条`}
      >
        {error ? (
          <ErrorMsg msg={error} className="px-4 py-6" />
        ) : loading ? (
          <Loading text="正在加载对局…" />
        ) : matches.length === 0 ? (
          <EmptyState text="当前条件下暂无对局" icon={<Swords className="size-5 opacity-50" />} className="py-8" />
        ) : (
          <ul className="divide-y divide-border">
            {matches.map((match, index) => (
              <li
                key={match.id}
                data-testid="history-match-row"
                data-match-type={match.match_type || 'unknown'}
                className="grid min-w-0 gap-2 px-3 py-2.5 sm:grid-cols-[2rem_minmax(0,1fr)_auto] sm:items-center"
              >
                <span className="hidden font-mono text-xs tabular-nums text-muted-foreground sm:block">
                  {(page - 1) * PAGE_SIZE + index + 1}
                </span>
                <div className="min-w-0">
                  <MatchParticipants source={match} variant="panel" className="items-stretch gap-1.5 sm:gap-2" />
                  <MatchOutcome
                    source={match}
                    seatLabels={outcomeSeatLabels(match)}
                    normalizedUnit={match.game_id === 'holdem' ? 'BB' : undefined}
                    className="mt-1.5"
                  />
                  <div className="mt-1 flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
                    <span className="font-mono tabular-nums sm:hidden">序号 {(page - 1) * PAGE_SIZE + index + 1}</span>
                    <MatchNatureBadge matchType={match.match_type} source={match} />
                    <span>{gameLabel(match.game_id)}</span>
                    <StatusBadge status={match.status} />
                    {match.match_type === 'contest' && match.contest_id != null && (
                      <Link to={`/contests/${match.contest_id}`} className="font-medium text-primary hover:underline">查看锦标赛</Link>
                    )}
                    <time className="font-mono tabular-nums">{fmtTime(match.created_at)}</time>
                  </div>
                </div>
                <Link
                  className="inline-flex min-w-0 shrink-0 items-center justify-center whitespace-nowrap rounded-md px-2 py-1.5 text-sm font-medium text-primary hover:bg-accent"
                  to={`/match/${encodeURIComponent(match.id)}`}
                >
                  {match.status === 'running' || match.status === 'pending' ? '观赛' : '打开回放'}
                </Link>
              </li>
            ))}
          </ul>
        )}
      </DataRegion>

      <Pagination page={page} perPage={PAGE_SIZE} total={total} onPageChange={setPage} />
    </PageFrame>
  )
}
