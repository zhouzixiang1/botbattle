import { useEffect, useRef, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import {
  Activity,
  Bot as BotIcon,
  Clock3,
  Minus,
  ShieldCheck,
  Target,
  TrendingDown,
  TrendingUp,
  Trophy,
} from 'lucide-react'
import PageStub from '@/components/PageStub'
import {
  AutoMatchQueuePanel,
  type AutoMatchQueueSnapshot,
} from '@/components/auto-match-queue'
import { Card } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { EmptyState, ErrorMsg, Loading } from '@/components/ui/status'
import { TierBadge } from '@/components/tier-badge'
import Pagination from '@/components/Pagination'
import { apiGet, errMsg } from '@/api'
import { GAMES, type GameId } from '@/lib/games'
import { fmtRating, fmtTime } from '@/lib/format'
import { cn } from '@/lib/utils'

interface Row {
  rank: number | null
  bot_id: number
  bot_name?: string
  bot_display?: string
  owner_name?: string
  rating: number
  rd?: number
  wins?: number
  losses?: number
  draws?: number
  matches_played?: number
  rating_delta?: number | null
  tier_name?: string
  tier_key?: string
  is_placement: boolean
  placement_required: number
  placement_remaining: number
  last_match_id?: string | null
  last_match_at?: string | null
}

interface Summary {
  total: number
  ranked: number
  placement: number
  last_rated_at?: string | null
}

interface LeaderboardResponse {
  leaderboard: Row[]
  game_id: string
  placement_required: number
  summary: Summary
  page?: number
  per_page?: number
  total: number
}

const EMPTY_SUMMARY: Summary = { total: 0, ranked: 0, placement: 0, last_rated_at: null }

function recordFor(row: Row) {
  const wins = row.wins ?? 0
  const draws = row.draws ?? 0
  const losses = row.losses ?? 0
  const played = row.matches_played ?? wins + draws + losses
  const winRate = played > 0 ? (wins / played) * 100 : 0
  return { wins, draws, losses, played, winRate }
}

function SummaryItem({ icon, label, value }: { icon: ReactNode; label: string; value: ReactNode }) {
  return (
    <div className="flex min-w-0 items-center gap-2.5 px-3 py-2.5 sm:px-4">
      <span className="flex size-8 shrink-0 items-center justify-center rounded-md bg-primary/10 text-primary">
        {icon}
      </span>
      <div className="min-w-0">
        <dt className="text-[11px] font-medium text-muted-foreground">{label}</dt>
        <dd className="break-words text-sm font-semibold leading-tight tabular-nums text-foreground [overflow-wrap:anywhere]">
          {value}
        </dd>
      </div>
    </div>
  )
}

function BotIdentity({ row }: { row: Row }) {
  const name = row.bot_display || row.bot_name || `Bot #${row.bot_id}`
  return (
    <div className="min-w-0">
      <Link
        to={`/bot/${row.bot_id}`}
        className="block break-words font-medium leading-snug text-foreground hover:text-primary [overflow-wrap:anywhere]"
      >
        {name}
      </Link>
      <div className="mt-0.5 min-w-0 break-words text-xs text-muted-foreground [overflow-wrap:anywhere]">
        {row.owner_name ? (
          <Link to={`/user/${encodeURIComponent(row.owner_name)}`} className="hover:text-primary hover:underline">
            @{row.owner_name}
          </Link>
        ) : '所有者未知'}
      </div>
    </div>
  )
}

function PlacementOrTier({ row, gameId }: { row: Row; gameId: GameId }) {
  const record = recordFor(row)
  if (row.is_placement) {
    return (
      <div className="flex flex-col items-start gap-0.5">
        <Badge variant="secondary" className="whitespace-nowrap text-muted-foreground">
          定级 {record.played}/{row.placement_required}
        </Badge>
        <span className="text-[11px] text-muted-foreground">还差 {row.placement_remaining} 场</span>
      </div>
    )
  }
  return (
    <TierBadge
      rating={row.rating}
      label={row.tier_name}
      gameId={gameId}
      tierKey={row.tier_key}
    />
  )
}

function RatingInfo({ row }: { row: Row }) {
  const delta = row.rating_delta
  return (
    <div className="min-w-0 tabular-nums">
      <div className="flex flex-wrap items-center gap-x-1.5 gap-y-0.5">
        <span className="font-mono font-semibold text-primary">{fmtRating(row.rating)}</span>
        {delta == null ? (
          <span className="text-[11px] text-muted-foreground">上次 —</span>
        ) : delta === 0 ? (
          <span className="inline-flex items-center gap-0.5 text-[11px] text-muted-foreground">
            <Minus className="size-3" />上次 0
          </span>
        ) : (
          <span className={cn(
            'inline-flex items-center gap-0.5 text-[11px] font-medium',
            delta > 0 ? 'text-success' : 'text-destructive',
          )}>
            {delta > 0 ? <TrendingUp className="size-3" /> : <TrendingDown className="size-3" />}
            上次 {delta > 0 ? '+' : ''}{delta.toFixed(0)}
          </span>
        )}
      </div>
      <div className="mt-0.5 text-[11px] text-muted-foreground">
        RD {row.rd == null ? '—' : Number(row.rd).toFixed(0)}
      </div>
    </div>
  )
}

function RecordInfo({ row }: { row: Row }) {
  const record = recordFor(row)
  return (
    <div className="tabular-nums">
      <div className="font-mono text-xs">
        <span className="text-success">{record.wins}</span>
        <span className="text-muted-foreground">-{record.draws}-</span>
        <span className="text-destructive">{record.losses}</span>
      </div>
      <div className="mt-0.5 text-[11px] text-muted-foreground">
        胜率 {record.winRate.toFixed(1)}%
      </div>
    </div>
  )
}

function RecentMatch({ row }: { row: Row }) {
  if (!row.last_match_id || !row.last_match_at) {
    return <span className="text-xs text-muted-foreground">暂无已验证对局</span>
  }
  return (
    <div className="min-w-0">
      <Link to={`/match/${encodeURIComponent(row.last_match_id)}`} className="text-xs font-medium text-primary hover:underline">
        查看对局
      </Link>
      <div className="mt-0.5 text-[11px] text-muted-foreground">{fmtTime(row.last_match_at)}</div>
    </div>
  )
}

function DesktopRows({
  rows,
  label,
  count,
  gameId,
  testId,
}: {
  rows: Row[]
  label: string
  count: number
  gameId: GameId
  testId: string
}) {
  if (rows.length === 0) return null
  return (
    <>
      <TableRow className="bg-muted/30 hover:bg-muted/30" data-testid={testId}>
        <TableCell colSpan={6} className="h-8 px-3 py-1.5 text-xs font-semibold text-muted-foreground">
          {label} · {count}
        </TableCell>
      </TableRow>
      {rows.map((row) => (
        <TableRow key={row.bot_id}>
          <TableCell className="w-10 px-2 font-mono text-xs font-semibold text-muted-foreground">
            {row.rank ?? '—'}
          </TableCell>
          <TableCell className="whitespace-normal">
            <BotIdentity row={row} />
          </TableCell>
          <TableCell><PlacementOrTier row={row} gameId={gameId} /></TableCell>
          <TableCell><RatingInfo row={row} /></TableCell>
          <TableCell><RecordInfo row={row} /></TableCell>
          <TableCell className="w-36 whitespace-normal"><RecentMatch row={row} /></TableCell>
        </TableRow>
      ))}
    </>
  )
}

function MobileSection({
  rows,
  label,
  count,
  gameId,
  testId,
}: {
  rows: Row[]
  label: string
  count: number
  gameId: GameId
  testId: string
}) {
  if (rows.length === 0) return null
  return (
    <section data-testid={testId}>
      <h2 className="border-b border-border bg-muted/30 px-3 py-2 text-xs font-semibold text-muted-foreground">
        {label} · {count}
      </h2>
      <ol className="divide-y divide-border">
        {rows.map((row) => (
          <li key={row.bot_id} className="min-w-0 px-3 py-3">
            <div className="flex min-w-0 items-start gap-3">
              <span className="w-6 shrink-0 pt-0.5 text-center font-mono text-xs font-semibold text-muted-foreground">
                {row.rank ?? '—'}
              </span>
              <div className="min-w-0 flex-1">
                <div className="flex min-w-0 items-start justify-between gap-2">
                  <BotIdentity row={row} />
                  <div className="shrink-0"><PlacementOrTier row={row} gameId={gameId} /></div>
                </div>
                <div className="mt-2 grid min-w-0 grid-cols-2 gap-2 rounded-md bg-muted/30 px-2.5 py-2">
                  <RatingInfo row={row} />
                  <RecordInfo row={row} />
                </div>
                <div className="mt-2"><RecentMatch row={row} /></div>
              </div>
            </div>
          </li>
        ))}
      </ol>
    </section>
  )
}

export default function Leaderboard() {
  const [rows, setRows] = useState<Row[]>([])
  const [summary, setSummary] = useState<Summary>(EMPTY_SUMMARY)
  const [placementRequired, setPlacementRequired] = useState(0)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [gameId, setGameId] = useState<GameId>('holdem')
  const [page, setPage] = useState(1)
  const [total, setTotal] = useState(0)
  const [queue, setQueue] = useState<AutoMatchQueueSnapshot | null>(null)
  const [queueLoading, setQueueLoading] = useState(true)
  const [queueError, setQueueError] = useState('')
  const queueRequest = useRef(0)
  const perPage = 50

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError('')
    const params = new URLSearchParams({
      game_id: gameId,
      page: String(page),
      per_page: String(perPage),
    })
    apiGet<LeaderboardResponse>(`/api/leaderboard?${params.toString()}`)
      .then((data) => {
        if (cancelled) return
        if (data.game_id !== gameId) throw new Error('排行榜游戏维度不一致')
        setRows(data.leaderboard || [])
        setSummary(data.summary || EMPTY_SUMMARY)
        setPlacementRequired(data.placement_required ?? 0)
        setTotal(data.total ?? 0)
      })
      .catch((reason) => {
        if (cancelled) return
        setRows([])
        setSummary(EMPTY_SUMMARY)
        setTotal(0)
        setError(errMsg(reason, '排行榜加载失败'))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [gameId, page])

  useEffect(() => {
    let disposed = false
    const loadQueue = async (initial = false) => {
      const requestId = ++queueRequest.current
      if (initial) setQueueLoading(true)
      try {
        const data = await apiGet<AutoMatchQueueSnapshot>(
          `/api/auto-match/queue?game_id=${encodeURIComponent(gameId)}`,
        )
        if (disposed || requestId !== queueRequest.current) return
        if (data.game_id !== gameId) throw new Error('自动排位队列游戏维度不一致')
        setQueue(data)
        setQueueError('')
      } catch (reason) {
        if (disposed || requestId !== queueRequest.current) return
        setQueueError(errMsg(reason, '自动排位队列加载失败'))
      } finally {
        if (!disposed && requestId === queueRequest.current) setQueueLoading(false)
      }
    }
    void loadQueue(true)
    const timer = window.setInterval(() => { void loadQueue(false) }, 3_000)
    return () => {
      disposed = true
      queueRequest.current += 1
      window.clearInterval(timer)
    }
  }, [gameId])

  const changeGame = (next: GameId) => {
    if (next === gameId) return
    // 游戏维度切换时旧概览不再具有任何语义。先同步清空再发新请求，避免
    // 慢网下新 tab 短暂展示上一游戏的总数、定级数和列表。
    setRows([])
    setSummary(EMPTY_SUMMARY)
    setPlacementRequired(0)
    setTotal(0)
    setQueue(null)
    setQueueError('')
    setQueueLoading(true)
    queueRequest.current += 1
    setError('')
    setLoading(true)
    setGameId(next)
    setPage(1)
  }
  const formalRows = rows.filter((row) => !row.is_placement)
  const placementRows = rows.filter((row) => row.is_placement)

  return (
    <PageStub
      title="排行榜"
      subtitle={`每款游戏独立使用 Glicko-2 评级；完成 ${placementRequired || 10} 场后进入正式榜。`}
    >
      <Tabs value={gameId} onValueChange={(value) => changeGame(value as GameId)} className="contents">
        <TabsList
          aria-label="游戏排行榜"
          data-testid="leaderboard-game-tabs"
          className={cn(
            'sticky z-30 mb-3 grid h-auto w-full grid-cols-3 gap-1 rounded-lg border border-border bg-background/95 p-1 shadow-sm backdrop-blur',
            'top-14 lg:top-0',
          )}
        >
          {GAMES.map((game) => {
            const GameIcon = game.icon
            return (
              <TabsTrigger
                key={game.id}
                value={game.id}
                className={cn(
                  'h-auto min-w-0 rounded-md px-2 py-2 text-sm transition-colors',
                  'data-[state=active]:bg-primary data-[state=active]:text-primary-foreground data-[state=active]:shadow-sm',
                  'data-[state=inactive]:text-muted-foreground data-[state=inactive]:hover:bg-muted data-[state=inactive]:hover:text-foreground',
                )}
              >
                <GameIcon className="size-3.5 shrink-0" />
                <span className="truncate">{game.label}</span>
              </TabsTrigger>
            )
          })}
        </TabsList>
      </Tabs>

      {error && <ErrorMsg msg={error} className="mb-3" />}

      <Card className="mb-3 gap-0 overflow-hidden py-0">
        <dl className="grid grid-cols-2 divide-x divide-y divide-border sm:grid-cols-4 sm:divide-y-0">
          <SummaryItem icon={<BotIcon className="size-4" />} label="Bot 总数" value={summary.total} />
          <SummaryItem icon={<ShieldCheck className="size-4" />} label="正式榜" value={summary.ranked} />
          <SummaryItem icon={<Target className="size-4" />} label="定级中" value={summary.placement} />
          <SummaryItem
            icon={<Clock3 className="size-4" />}
            label="最近更新"
            value={summary.last_rated_at ? fmtTime(summary.last_rated_at) : '暂无'}
          />
        </dl>
      </Card>

      <AutoMatchQueuePanel
        snapshot={queue}
        loading={queueLoading}
        error={queueError}
        maxUpcoming={4}
        className="mb-3"
      />

      {loading ? (
        <Card className="py-4"><Loading /></Card>
      ) : rows.length === 0 ? (
        <Card className="py-2">
          <EmptyState text="该游戏暂无可排名 Bot" icon={<Trophy className="size-7 opacity-40" />} />
        </Card>
      ) : (
        <Card className="gap-0 py-0">
          <div className="hidden md:block" data-testid="leaderboard-desktop">
            <Table className="table-fixed" containerClassName="overflow-visible">
              <TableHeader className="sticky top-[6.75rem] z-20 bg-background/95 backdrop-blur lg:top-[3.25rem]">
                <TableRow>
                  <TableHead className="w-10 px-2">名次</TableHead>
                  <TableHead className="w-[30%]">Bot / 所有者</TableHead>
                  <TableHead className="w-[14%]">段位</TableHead>
                  <TableHead className="w-[20%] whitespace-normal">
                    <span className="block">Rating / 上次变化</span>
                    <span className="normal-case font-normal">RD 越低越稳定</span>
                  </TableHead>
                  <TableHead className="w-[18%] whitespace-normal">胜-平-负 / 胜率</TableHead>
                  <TableHead className="w-[16%] whitespace-normal">最近对局</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                <DesktopRows
                  rows={formalRows}
                  label="正式排名"
                  count={summary.ranked}
                  gameId={gameId}
                  testId="leaderboard-section-formal"
                />
                <DesktopRows
                  rows={placementRows}
                  label="定级中（暂无正式名次）"
                  count={summary.placement}
                  gameId={gameId}
                  testId="leaderboard-section-placement"
                />
              </TableBody>
            </Table>
          </div>

          <div className="md:hidden" data-testid="leaderboard-mobile">
            <MobileSection
              rows={formalRows}
              label="正式排名"
              count={summary.ranked}
              gameId={gameId}
              testId="leaderboard-section-formal"
            />
            <MobileSection
              rows={placementRows}
              label="定级中（暂无正式名次）"
              count={summary.placement}
              gameId={gameId}
              testId="leaderboard-section-placement"
            />
          </div>

          <Pagination page={page} perPage={perPage} total={total} onPageChange={setPage} />
        </Card>
      )}

      <p className="mt-3 flex items-start gap-1.5 text-xs leading-relaxed text-muted-foreground">
        <Activity className="mt-0.5 size-3.5 shrink-0" />
        Rating 变化只对比该 Bot 在当前游戏的上一条评分记录；胜率按胜场 ÷ 总场次计算。
      </p>
    </PageStub>
  )
}
