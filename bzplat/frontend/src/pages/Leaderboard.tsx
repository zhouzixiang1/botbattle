import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  Bot,
  ChevronRight,
  Gauge,
  Minus,
  TrendingDown,
  TrendingUp,
  Trophy,
} from 'lucide-react'

import { apiFetch, apiGet, errMsg } from '@/api'
import Pagination from '@/components/Pagination'
import {
  ExecutionQueuePanel,
  type ExecutionQueueSnapshot,
} from '@/components/execution-queue'
import { DataRegion, PageFrame, PageHeader, StickyToolbar } from '@/components/layout'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { EntityName, OverflowText } from '@/components/ui/overflow-text'
import { EmptyState, ErrorMsg, Loading } from '@/components/ui/status'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import {
  DataTable,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { fmtRating, fmtTime } from '@/lib/format'
import { GAMES, type GameId } from '@/lib/games'
import { cn } from '@/lib/utils'
import { useSingleFlightPolling } from '@/hooks/use-single-flight-polling'

interface RankingRow {
  rank: number | null
  rank_total: number
  percentile: number | null
  bot_id: number
  bot_name?: string
  bot_display?: string
  owner_name?: string
  rating: number
  rd: number
  confidence_low: number | null
  confidence_high: number | null
  wins: number
  losses: number
  draws: number
  rated_matches: number
  unique_opponents: number
  rating_delta: number | null
  recent_delta_30d: number | null
  ranking_min_matches: number
  ranking_progress: number
  ranking_eligible: boolean
  last_match_id?: string | null
  last_match_at?: string | null
}

interface RankingSummary {
  total: number
  eligible: number
  sample: number
  last_rated_at?: string | null
}

interface LeaderboardResponse {
  leaderboard: RankingRow[]
  game_id: string
  ranking_min_matches: number
  summary: RankingSummary
  page?: number
  per_page?: number
  total: number
}

const EMPTY_SUMMARY: RankingSummary = {
  total: 0,
  eligible: 0,
  sample: 0,
  last_rated_at: null,
}

function signed(value: number | null | undefined) {
  if (value == null) return '—'
  if (value === 0) return '0'
  return `${value > 0 ? '+' : ''}${value.toFixed(0)}`
}

function ChangeValue({ value, label }: { value: number | null; label: string }) {
  if (value == null) return <span className="text-muted-foreground">{label} —</span>
  if (value === 0) {
    return <span className="inline-flex items-center gap-1 text-muted-foreground"><Minus className="size-3" />{label} 0</span>
  }
  return (
    <span className={cn('inline-flex items-center gap-1 font-medium', value > 0 ? 'text-success' : 'text-destructive')}>
      {value > 0 ? <TrendingUp className="size-3" /> : <TrendingDown className="size-3" />}
      {label} {signed(value)}
    </span>
  )
}

function BotIdentity({ row, inline = false }: { row: RankingRow; inline?: boolean }) {
  const name = row.bot_display || row.bot_name || '未命名 Bot'
  const nameNode = (
    <Link to={`/bot/${row.bot_id}`} className="block min-w-0 hover:text-primary">
      <EntityName lines={inline ? 1 : 2} tooltip={false} tooltipFocusable={false} className="text-sm hover:text-primary">
        {name}
      </EntityName>
    </Link>
  )
  const ownerNode = (
    <OverflowText
      tooltip={row.owner_name ? `@${row.owner_name}` : '所有者未知'}
      className={cn('min-w-0 text-xs text-muted-foreground', inline ? 'shrink-[3] basis-24' : 'mt-0.5')}
    >
      {row.owner_name ? (
        <Link to={`/user/${encodeURIComponent(row.owner_name)}`} className="hover:text-primary hover:underline">
          @{row.owner_name}
        </Link>
      ) : '所有者未知'}
    </OverflowText>
  )
  if (inline) {
    return (
      <div className="flex min-w-0 max-w-full items-baseline gap-1.5">
        <div className="min-w-0 shrink-[2] basis-28">{nameNode}</div>
        {ownerNode}
      </div>
    )
  }
  return (
    <div className="min-w-0">
      {nameNode}
      {ownerNode}
    </div>
  )
}

function RatingFacts({ row }: { row: RankingRow }) {
  const hasConfidence = row.confidence_low != null && row.confidence_high != null
  return (
    <div className="min-w-0 tabular-nums">
      {hasConfidence ? (
        <Tooltip>
          <TooltipTrigger asChild>
            <span
              tabIndex={0}
              className="inline-block min-w-0 cursor-help font-mono text-sm font-semibold text-foreground underline decoration-dotted underline-offset-4 outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50"
            >
              {fmtRating(row.rating)}
            </span>
          </TooltipTrigger>
          <TooltipContent>
            实力波动范围 {row.confidence_low!.toFixed(0)}–{row.confidence_high!.toFixed(0)}
          </TooltipContent>
        </Tooltip>
      ) : (
        <span className="font-mono text-sm font-semibold text-foreground">{fmtRating(row.rating)}</span>
      )}
    </div>
  )
}

function RankingFacts({ row }: { row: RankingRow }) {
  if (!row.ranking_eligible || row.rank == null) {
    return (
      <div className="tabular-nums">
        <div className="min-w-0 font-mono text-xs font-semibold text-foreground">
          {row.rated_matches}/{row.ranking_min_matches} 场
        </div>
        <div className="mt-0.5 min-w-0 text-xs text-muted-foreground">
          资格进度 {(row.ranking_progress * 100).toFixed(0)}%
        </div>
      </div>
    )
  }
  return (
    <div className="tabular-nums">
      <div className="min-w-0 font-mono text-xs font-semibold text-foreground">#{row.rank} / {row.rank_total}</div>
      <div className="mt-0.5 min-w-0 text-xs text-muted-foreground">
        百分位 {row.percentile == null ? '—' : `${row.percentile.toFixed(1)}%`}
      </div>
    </div>
  )
}

function SampleFacts({ row }: { row: RankingRow }) {
  return (
    <div className="tabular-nums">
      <div className="min-w-0 font-mono text-xs text-foreground">{row.wins} 胜 · {row.draws} 平 · {row.losses} 负</div>
      <div className="mt-0.5 min-w-0 text-xs text-muted-foreground">
        {row.rated_matches} 场计分 · {row.unique_opponents} 个对手
      </div>
    </div>
  )
}

function ChangeFacts({ row }: { row: RankingRow }) {
  return (
    <div className="flex min-w-0 flex-col gap-0.5 text-xs tabular-nums">
      <ChangeValue value={row.rating_delta} label="上次" />
      <ChangeValue value={row.recent_delta_30d} label="30 日" />
    </div>
  )
}

function RecentMatch({ row }: { row: RankingRow }) {
  if (!row.last_match_id || !row.last_match_at) {
    return <span className="text-xs text-muted-foreground">暂无已验证对局</span>
  }
  return (
    <div className="min-w-0">
      <Link to={`/match/${encodeURIComponent(row.last_match_id)}`} className="text-xs font-medium text-primary hover:underline">
        查看对局
      </Link>
      <div className="mt-0.5 text-xs text-muted-foreground">{fmtTime(row.last_match_at)}</div>
    </div>
  )
}

/** 排名门槛一句话 + 可展开的评分规则说明（默认收起，避免说明墙）。 */
function RankingRuleNote({ minMatches }: { minMatches: number }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="min-w-0 text-xs text-muted-foreground">
      <p>
        {minMatches
          ? `打满 ${minMatches} 场计分对局后进入公开排名；评分随胜负自动更新。`
          : '打满计分对局后进入公开排名；评分随胜负自动更新。'}
        {!open && (
          <button
            type="button"
            onClick={() => setOpen(true)}
            className="ml-1 inline-flex min-h-11 items-center font-medium text-primary hover:underline sm:min-h-0"
          >
            了解评分规则
          </button>
        )}
      </p>
      {open && (
        <p className="mt-1 max-w-3xl leading-relaxed">
          排行榜只统计当前派遣参榜的 Bot；每个账号每款游戏最多派遣一个。评分会结合对手强弱与对局结果自动更新，并不是简单的胜场计数；赛事积分不进入平台评分。评分旁的虚线区间表示实力可能波动的范围，对局越多越稳定。百分位按公开名次映射；30 日变化缺少窗口起点时显示「—」。
          <button
            type="button"
            onClick={() => setOpen(false)}
            className="ml-1 inline-flex min-h-11 items-center font-medium text-primary hover:underline sm:min-h-0"
          >
            收起
          </button>
        </p>
      )}
    </div>
  )
}

function DesktopRows({
  rows,
  title,
  globalCount,
  testId,
}: {
  rows: RankingRow[]
  title: string
  globalCount: number
  testId: string
}) {
  if (rows.length === 0) return null
  return (
    <>
      <TableRow className="bg-muted/30 hover:bg-muted/30" data-testid={testId}>
        <TableCell colSpan={5} className="h-7 px-3 py-1 text-xs font-semibold text-muted-foreground">
          {title} · 本页 {rows.length} / 共 {globalCount}
        </TableCell>
      </TableRow>
      {rows.map((row) => (
        <TableRow key={row.bot_id}>
          <TableCell className="w-16 whitespace-nowrap font-mono text-xs font-semibold text-muted-foreground">
            {row.rank == null ? `${row.rated_matches}/${row.ranking_min_matches} 场` : `#${row.rank}`}
          </TableCell>
          {/* 堆叠事实（与移动卡一致）：inline 单行版会让表格按 max-content
              求和撑到 ~1555px（1440 视口必横向滚动），堆叠后各列 ≤ ~150px。 */}
          <TableCell className="max-w-xs"><BotIdentity row={row} inline /></TableCell>
          <TableCell><RatingFacts row={row} /></TableCell>
          <TableCell><SampleFacts row={row} /></TableCell>
          <TableCell><ChangeFacts row={row} /></TableCell>
        </TableRow>
      ))}
    </>
  )
}

function MobileSection({
  rows,
  title,
  globalCount,
  testId,
}: {
  rows: RankingRow[]
  title: string
  globalCount: number
  testId: string
}) {
  if (rows.length === 0) return null
  return (
    <section data-testid={testId}>
      <h2 className="border-b border-border bg-muted/30 px-3 py-2 text-xs font-semibold text-muted-foreground">
        {title} · 本页 {rows.length} / 共 {globalCount}
      </h2>
      <ul className="divide-y divide-border">
        {rows.map((row) => (
          <li key={row.bot_id} className="min-w-0 px-3 py-3">
            <div className="flex min-w-0 items-start justify-between gap-3">
              <BotIdentity row={row} />
              {row.rank == null ? (
                <Badge variant="secondary" className="shrink-0 font-mono">{row.rated_matches}/{row.ranking_min_matches} 场</Badge>
              ) : (
                <Badge variant="outline" className="shrink-0 font-mono">#{row.rank}</Badge>
              )}
            </div>
            <div className="mt-2 grid min-w-0 grid-cols-2 gap-x-3 gap-y-2 rounded-lg bg-muted/30 p-2.5">
              <RatingFacts row={row} />
              <RankingFacts row={row} />
              <SampleFacts row={row} />
              <ChangeFacts row={row} />
            </div>
            <div className="mt-2"><RecentMatch row={row} /></div>
          </li>
        ))}
      </ul>
    </section>
  )
}

export default function Leaderboard() {
  const [rows, setRows] = useState<RankingRow[]>([])
  const [summary, setSummary] = useState<RankingSummary>(EMPTY_SUMMARY)
  const [rankingMinMatches, setRankingMinMatches] = useState(0)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [gameId, setGameId] = useState<GameId>('holdem')
  const [page, setPage] = useState(1)
  const [total, setTotal] = useState(0)
  const [queue, setQueue] = useState<ExecutionQueueSnapshot | null>(null)
  const [queueLoading, setQueueLoading] = useState(true)
  const [queueError, setQueueError] = useState('')
  const [queueLastUpdatedAt, setQueueLastUpdatedAt] = useState<number | null>(null)
  // 队列详情默认折叠为一行摘要；展开后才挂载面板并继续轮询（面板组件本身不变）。
  const [queueOpen, setQueueOpen] = useState(false)
  // 每页 14 行配合堆叠事实行高与固定队列面板，控制首屏总高；翻页语义不变。
  const perPage = 14

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
        setRankingMinMatches(data.ranking_min_matches ?? 0)
        setTotal(data.total ?? 0)
      })
      .catch((reason) => {
        if (cancelled) return
        setRows([])
        setSummary(EMPTY_SUMMARY)
        setRankingMinMatches(0)
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

  const pollQueue = useCallback(async (signal: AbortSignal) => {
    const data = await apiFetch<ExecutionQueueSnapshot>('/api/execution-queue', {
      method: 'GET',
      signal,
    })
    if (signal.aborted) return
    setQueue(data)
    setQueueLastUpdatedAt(Date.now())
  }, [])

  const {
    refresh: refreshQueue,
    polling: queuePolling,
    offline: queueOffline,
  } = useSingleFlightPolling({
    task: pollQueue,
    intervalMs: 3_000,
    maxIntervalMs: 24_000,
    onSuccess: () => {
      setQueueError('')
      setQueueLoading(false)
    },
    onError: (reason) => {
      setQueueError(errMsg(reason, '全局执行队列加载失败'))
      setQueueLoading(false)
    },
  })

  const changeGame = (next: GameId) => {
    if (next === gameId) return
    setRows([])
    setSummary(EMPTY_SUMMARY)
    setRankingMinMatches(0)
    setTotal(0)
    setError('')
    setLoading(true)
    setGameId(next)
    setPage(1)
  }

  const rankedRows = rows.filter((row) => row.ranking_eligible)
  const sampleRows = rows.filter((row) => !row.ranking_eligible)

  return (
    <PageFrame layout="public-leaderboard" width="full">
      <PageHeader
        title="排行榜"
        description="每款游戏独立使用 Glicko-2 数值评分；每个账号每款游戏最多派遣一个 Bot，公开名次与计分样本分区展示。"
      />

      <StickyToolbar label="排行榜游戏选择">
        <Tabs value={gameId} onValueChange={(value) => changeGame(value as GameId)} className="w-full">
          <TabsList aria-label="游戏排行榜" data-testid="leaderboard-game-tabs" className="grid h-auto w-full grid-cols-3 gap-1">
            {GAMES.map((game) => {
              const GameIcon = game.icon
              return (
                <TabsTrigger key={game.id} value={game.id} className="h-auto min-w-0 px-2 py-1.5">
                  <GameIcon className="size-3.5 shrink-0" />
                  <span className="truncate">{game.label}</span>
                </TabsTrigger>
              )
            })}
          </TabsList>
        </Tabs>
      </StickyToolbar>

      {error && <ErrorMsg msg={error} />}

      {queueOpen ? (
        <ExecutionQueuePanel
          snapshot={queue}
          loading={queueLoading || (!queue && queuePolling)}
          error={queueOffline ? '当前离线；联网后会自动刷新全局队列。' : queueError}
          stale={!!queue && (queueOffline || !!queueError)}
          lastUpdatedAt={queueLastUpdatedAt}
          onRetry={refreshQueue}
          // 排队展示上限收紧到 2：等待列表只保留前 2 条 + 条数溢出提示，
          // 正在执行列表不受影响；压掉长队列下首屏的队列高度。
          maxQueued={2}
          compactOnMobile
          compactCapacity
          dense
          action={(
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="min-h-11 sm:min-h-0"
              onClick={() => setQueueOpen(false)}
            >
              收起队列
            </Button>
          )}
        />
      ) : (
        <button
          type="button"
          data-testid="execution-queue-summary"
          aria-expanded={false}
          onClick={() => setQueueOpen(true)}
          className="flex min-h-11 w-full items-center justify-between gap-2 rounded-xl border bg-card px-3 py-2 text-left text-sm transition-colors hover:bg-muted/40 focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50"
        >
          <span className="inline-flex min-w-0 items-center gap-2 font-medium text-foreground">
            <Bot className="size-4 shrink-0 text-primary" aria-hidden="true" />
            对局执行
            <span className="min-w-0 truncate text-xs font-normal text-muted-foreground">
              {queue ? `当前 ${queue.capacity.running_matches} 场进行中` : '正在获取对局执行情况…'}
            </span>
          </span>
          <ChevronRight className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
        </button>
      )}

      <DataRegion
        title="数值评分明细"
        description={<RankingRuleNote minMatches={rankingMinMatches} />}
        actions={<Gauge className="size-4 text-primary" />}
        density="compact"
      >
        {loading ? (
          <Loading text="正在加载排行榜…" />
        ) : rows.length === 0 ? (
          <EmptyState text="该游戏暂无已派遣参榜 Bot" icon={<Trophy className="size-6 opacity-40" />} className="py-10" />
        ) : (
          <>
            <div className="hidden md:block" data-testid="leaderboard-desktop">
              <DataTable className="rounded-none border-0" scrollLabel="排行榜数值明细">
                <Table aria-label="排行榜数值明细" className="min-w-[48rem] [--table-row-height:3rem]">
                  <TableHeader>
                    <TableRow>
                      <TableHead>名次</TableHead>
                      <TableHead className="min-w-48">Bot / 所有者</TableHead>
                      <TableHead>评分</TableHead>
                      <TableHead>战绩</TableHead>
                      <TableHead>评分变化</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    <DesktopRows rows={rankedRows} title="公开排名" globalCount={summary.eligible} testId="leaderboard-section-ranked" />
                    <DesktopRows rows={sampleRows} title="计分样本（无公开名次）" globalCount={summary.sample} testId="leaderboard-section-sample" />
                  </TableBody>
                </Table>
              </DataTable>
            </div>

            <div className="md:hidden" data-testid="leaderboard-mobile">
              <MobileSection rows={rankedRows} title="公开排名" globalCount={summary.eligible} testId="leaderboard-section-ranked" />
              <MobileSection rows={sampleRows} title="计分样本（无公开名次）" globalCount={summary.sample} testId="leaderboard-section-sample" />
            </div>

            <Pagination page={page} perPage={perPage} total={total} onPageChange={setPage} />
          </>
        )}
      </DataRegion>
    </PageFrame>
  )
}
