import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Clock3, ListFilter, Swords } from 'lucide-react'

import { apiGet, errMsg } from '@/api'
import { DataRegion, PageFrame, PageHeader, StickyToolbar, SummaryStrip } from '@/components/layout'
import Pagination from '@/components/Pagination'
import { EntityName } from '@/components/ui/overflow-text'
import { EmptyState, ErrorMsg, Loading, StatusBadge } from '@/components/ui/status'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { fmtTime } from '@/lib/format'
import { GAMES, gameLabel } from '@/lib/games'
import { SummaryMetric } from '@/pages/public-page-ui'

interface Match {
  id: string
  status: string
  bot_a_id: number | null
  bot_b_id: number | null
  bot_a_name?: string
  bot_b_name?: string
  bot_a_display?: string
  bot_b_display?: string
  created_at?: string
  result?: { rounds_played?: number; deltas?: number[]; normalized_delta?: number }
  match_type?: string
  game_id?: string
}

const PAGE_SIZE = 20

function BotName({ id, display, name }: { id: number | null; display?: string; name?: string }) {
  const label = display || name || '已删除 Bot'
  if (id == null) {
    return <EntityName lines={2} className="text-sm text-muted-foreground">{label}</EntityName>
  }
  return (
    <Link to={`/bot/${id}`} className="min-w-0 hover:text-primary">
      <EntityName lines={2} tooltip={false} tooltipFocusable={false} className="text-sm hover:text-primary">
        {label}
      </EntityName>
    </Link>
  )
}

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

  const activeCount = matches.filter((match) => match.status === 'pending' || match.status === 'running').length

  return (
    <PageFrame layout="public-history">
      <PageHeader
        title="对局历史"
        description="浏览全站对局记录，并按状态与游戏维度定位回放。"
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

      <SummaryStrip columns={3}>
        <SummaryMetric label="匹配记录" value={total} detail="当前筛选条件" icon={<Swords className="size-4" />} />
        <SummaryMetric label="本页记录" value={matches.length} detail={`每页最多 ${PAGE_SIZE} 场`} />
        <SummaryMetric label="本页活跃" value={activeCount} detail="排队中或进行中" icon={<Clock3 className="size-4" />} />
      </SummaryStrip>

      <DataRegion
        title="对局记录"
        description={gameId || status ? '已应用页面顶部筛选条件' : '按创建时间查看最新记录'}
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
              <li key={match.id} className="grid min-w-0 gap-2 px-3 py-2.5 sm:grid-cols-[2rem_minmax(0,1fr)_auto] sm:items-center">
                <span className="hidden font-mono text-xs tabular-nums text-muted-foreground sm:block">
                  {(page - 1) * PAGE_SIZE + index + 1}
                </span>
                <div className="min-w-0">
                  <div className="grid min-w-0 grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)] items-start gap-2">
                    <BotName id={match.bot_a_id} display={match.bot_a_display} name={match.bot_a_name} />
                    <span className="shrink-0 text-xs text-muted-foreground">vs</span>
                    <BotName id={match.bot_b_id} display={match.bot_b_display} name={match.bot_b_name} />
                  </div>
                  <div className="mt-1 flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
                    <span>{gameLabel(match.game_id)}</span>
                    <StatusBadge status={match.status} />
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
