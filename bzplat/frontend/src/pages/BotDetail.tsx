import { useEffect, useState } from 'react'
import { Link, useParams, useNavigate } from 'react-router-dom'
import { Star, ArrowLeft, Trophy, Swords, Target, History as HistoryIcon } from 'lucide-react'
import { CartesianGrid, Line, LineChart, XAxis, YAxis } from 'recharts'
import { DataRegion, PageFrame, PageHeader, StickyToolbar } from '@/components/layout'
import { MatchNatureBadge, MatchParticipantIdentity, MatchParticipants } from '@/components/MatchParticipants'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import {
  DataTable,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { ChartContainer, ChartTooltip, ChartTooltipContent, type ChartConfig } from '@/components/ui/chart'
import { EmptyState, ErrorMsg, Loading, StatusBadge } from '@/components/ui/status'
import { EntityName, OverflowText } from '@/components/ui/overflow-text'
import Comments from '@/components/Comments'
import Pagination from '@/components/Pagination'
import { apiGet, apiJson, errMsg } from '@/api'
import { toast } from 'sonner'
import { useAuth } from '@/components/useAuth'
import { gameLabel, gameIcon } from '@/lib/games'
import { isBotSelfPlay, resolveMatchParticipant, type MatchParticipantSource } from '@/lib/match-participants'
import { fmtTime, fmtRating, fmtDate } from '@/lib/format'
import { CopyIdentifier } from '@/pages/public-page-ui'

/* ── 类型 ─────────────────────────────────────────────── */
interface BotProfile {
  id: number
  name: string
  display_name: string
  description?: string
  game_id: string
  owner_id: number
  owner_name?: string
  owner_display?: string
  is_active: number | boolean
  current_version?: number
  created_at?: string
  rating?: number
  rd?: number
  vol?: number
  wins?: number
  losses?: number
  draws?: number
  rated_matches: number
  unique_opponents: number
  confidence_low: number | null
  confidence_high: number | null
  rank: number | null
  rank_total: number
  percentile: number | null
  ranking_min_matches: number
  ranking_progress: number
  ranking_eligible: boolean
  rating_delta: number | null
  recent_delta_30d: number | null
  normal_completion_rate: number | null
  technical_failures: number
}

interface MatchRow extends MatchParticipantSource {
  id: string
  game_id: string
  status: string
  winner: number | null
  match_type: string
  bot_a_id: number | null
  bot_b_id: number | null
  result?: { rounds_played?: number; deltas?: number[]; normalized_delta?: number }
  created_at?: string
}

interface OpponentRow {
  opponent_id: number
  opponent_name: string
  opponent_display?: string
  game_id?: string
  wins: number
  losses: number
  draws: number
  samples: number
  last_played_at?: string
}

interface RatingPoint {
  id: number
  rating: number
  rd: number
  vol: number
  matches_played: number
  reason?: string
  created_at: string
}

/* ── 辅助 ─────────────────────────────────────────────── */
function fmtPct(r: number): string {
  return `${(r * 100).toFixed(1)}%`
}

function fmtSigned(value: number | null | undefined): string {
  if (value == null) return '—'
  if (value === 0) return '0'
  return `${value > 0 ? '+' : ''}${value.toFixed(1)}`
}

function matchOutcome(m: MatchRow, botId: number): 'win' | 'loss' | 'draw' | 'selfplay' | '' {
  if (m.status !== 'completed') return ''
  if (isBotSelfPlay(m)) return 'selfplay'
  if (m.winner === null) return 'draw'
  const seatA = resolveMatchParticipant(m, 0)
  const seatB = resolveMatchParticipant(m, 1)
  // human 对局物理上会在两侧保存同一 Bot id；实际 Bot 座由 is_human 决定。
  const botSeat = !seatA.isHuman && seatA.botId === botId
    ? 0
    : !seatB.isHuman && seatB.botId === botId
      ? 1
      : null
  if (botSeat == null) return ''
  const won = m.winner === botSeat
  return won ? 'win' : 'loss'
}

function participantStates(m: MatchRow) {
  return m.status === 'completed' && m.winner === 0
    ? (['winner', 'loser'] as const)
    : m.status === 'completed' && m.winner === 1
      ? (['loser', 'winner'] as const)
      : (['neutral', 'neutral'] as const)
}

function MatchOutcome({ match, botId }: { match: MatchRow; botId: number }) {
  const outcome = matchOutcome(match, botId)
  if (match.status !== 'completed') return <StatusBadge status={match.status} />
  return (
    <Badge variant={outcome === 'win' ? 'default' : outcome === 'loss' ? 'destructive' : 'secondary'}>
      {outcome === 'win' ? '胜' : outcome === 'loss' ? '负' : outcome === 'selfplay' ? '自博弈' : '平'}
    </Badge>
  )
}

function MobileMatchCard({ match, botId }: { match: MatchRow; botId: number }) {
  const states = participantStates(match)
  return (
    <article
      data-testid="bot-match-mobile-card"
      data-match-type={match.match_type}
      className="space-y-2.5 px-3 py-3"
    >
      <header className="flex min-w-0 flex-wrap items-center gap-2">
        <span className="mr-auto font-mono text-xs text-muted-foreground">{fmtTime(match.created_at)}</span>
        <MatchOutcome match={match} botId={botId} />
        <MatchNatureBadge matchType={match.match_type} source={match} />
      </header>
      <div data-match-participants="true" className="grid min-w-0 gap-2">
        <MatchParticipantIdentity source={match} side={0} variant="panel" state={states[0]} textLines={2} />
        <MatchParticipantIdentity source={match} side={1} variant="panel" state={states[1]} textLines={2} />
      </div>
      <Button asChild variant="outline" size="sm" className="min-h-11 w-full text-primary">
        <Link to={`/match/${encodeURIComponent(match.id)}`}>查看对局回放</Link>
      </Button>
    </article>
  )
}

const chartConfig = {
  rating: { label: 'Rating', color: 'var(--chart-1)' },
} satisfies ChartConfig

/* ── 评分曲线（recharts，浅/暗双主题） ────────────────── */
function RatingChart({ points }: { points: RatingPoint[] }) {
  if (points.length < 2) {
    return <EmptyState text="评分数据不足，至少需 2 个数据点" icon={<Target className="size-5 opacity-50" />} className="py-8" />
  }
  const data = points.map((p, idx) => ({
    idx: idx + 1,
    rating: Number(p.rating.toFixed(1)),
    date: p.created_at?.slice(0, 10),
  }))
  return (
    <ChartContainer config={chartConfig} className="h-[200px] w-full">
      <LineChart data={data} margin={{ left: 8, right: 12, top: 8, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" vertical={false} />
        <XAxis dataKey="idx" tickLine={false} axisLine={false} tickMargin={8} className="text-[10px]" />
        <YAxis domain={['dataMin - 20', 'dataMax + 20']} tickLine={false} axisLine={false} width={40} className="text-[10px]" />
        <ChartTooltip content={<ChartTooltipContent labelKey="rating" />} />
        <Line
          dataKey="rating"
          type="monotone"
          stroke="var(--color-rating)"
          strokeWidth={2}
          dot={{ r: 3, fill: 'var(--color-rating)' }}
          activeDot={{ r: 5 }}
        />
      </LineChart>
    </ChartContainer>
  )
}

/* ── 主组件 ───────────────────────────────────────────── */
export default function BotDetail() {
  const { id } = useParams<{ id: string }>()
  const botId = Number(id)
  const navigate = useNavigate()
  const { user } = useAuth()
  const [profile, setProfile] = useState<BotProfile | null>(null)
  const [matches, setMatches] = useState<MatchRow[]>([])
  const [opponents, setOpponents] = useState<OpponentRow[]>([])
  const [history, setHistory] = useState<RatingPoint[]>([])
  const [profileError, setProfileError] = useState('')
  const [matchesError, setMatchesError] = useState('')
  const [actionError, setActionError] = useState('')
  const [loading, setLoading] = useState(true)
  const [matchesLoading, setMatchesLoading] = useState(true)
  const [favorited, setFavorited] = useState(false)
  const [favCount, setFavCount] = useState(0)
  // 对局历史分页
  const [matchesPage, setMatchesPage] = useState(1)
  const [matchesTotal, setMatchesTotal] = useState(0)
  const matchesPerPage = 30

  useEffect(() => {
    if (!Number.isInteger(botId) || botId <= 0) {
      setProfileError('无效的 Bot 标识')
      setLoading(false)
      return
    }
    setLoading(true)
    setProfileError('')
    Promise.all([
      apiGet<{ profile: BotProfile }>(`/api/bots/${botId}/profile`),
      apiGet<{ opponents: OpponentRow[] }>(`/api/bots/${botId}/opponents?limit=20`),
      apiGet<{ history: RatingPoint[] }>(`/api/bots/${botId}/rating-history?limit=100`),
    ])
      .then(([p, o, h]) => {
        setProfile(p.profile)
        setOpponents(o.opponents || [])
        setHistory(h.history || [])
      })
      .catch((e) => setProfileError(errMsg(e)))
      .finally(() => setLoading(false))
    if (user) {
      apiGet<{ favorited: boolean; favorite_count: number }>(
        `/api/bots/${botId}/favorite-status`,
      )
        .then((fs) => {
          setFavorited(fs.favorited)
          setFavCount(fs.favorite_count)
        })
        .catch(() => {})
    }
  }, [botId, user])

  // 对局历史单独分页（切页只重拉 matches，不重置 profile/opponents/history）
  useEffect(() => {
    if (!Number.isInteger(botId) || botId <= 0) {
      setMatchesLoading(false)
      return
    }
    setMatchesLoading(true)
    setMatchesError('')
    apiGet<{ matches: MatchRow[]; total?: number }>(
      `/api/bots/${botId}/matches?page=${matchesPage}&per_page=${matchesPerPage}`,
    )
      .then((m) => {
        setMatches(m.matches || [])
        if (m.total !== undefined) setMatchesTotal(m.total)
      })
      .catch((e) => setMatchesError(errMsg(e)))
      .finally(() => setMatchesLoading(false))
  }, [botId, matchesPage])

  function toggleFavorite() {
    if (!user) return
    const method = favorited ? 'DELETE' : 'POST'
    apiJson(`/api/bots/${botId}/favorite`, method)
      .then(() => {
        setActionError('')
        setFavorited(!favorited)
        setFavCount((c) => c + (favorited ? -1 : 1))
        toast.success(favorited ? '已取消收藏' : '收藏成功')
      })
      .catch((e) => setActionError(errMsg(e)))
  }

  if (loading) {
    return (
      <PageFrame layout="public-bot-detail-loading">
        <PageHeader title="Bot 详情" description="正在读取 Bot 资料、评分与对局记录。" />
        <DataRegion title="Bot 概览" contentClassName="space-y-2 p-4">
          <Skeleton className="h-6 w-48 max-w-full" />
          <Skeleton className="h-4 w-32 max-w-full" />
          <Skeleton className="h-4 w-72 max-w-full" />
        </DataRegion>
      </PageFrame>
    )
  }
  if (profileError || !profile) {
    return (
      <PageFrame width="narrow" layout="public-bot-detail-error">
        <PageHeader title="Bot 详情" description="无法显示该 Bot 的公开资料。" />
        <DataRegion title="加载失败" contentClassName="space-y-3 px-4 py-5">
          <ErrorMsg msg={profileError || 'Bot 不存在'} />
          <Button variant="outline" size="sm" onClick={() => (window.history.length > 1 ? navigate(-1) : navigate('/leaderboard'))}>
            <ArrowLeft className="size-4" />返回
          </Button>
        </DataRegion>
      </PageFrame>
    )
  }

  const ratedMatches = profile.rated_matches ?? ((profile.wins ?? 0) + (profile.losses ?? 0) + (profile.draws ?? 0))
  const GameIcon = gameIcon(profile.game_id)

  return (
    <PageFrame layout="public-bot-detail">
      <PageHeader
        eyebrow={`@${profile.name}`}
        title={<EntityName lines={2} tooltip={profile.display_name || profile.name} className="text-2xl font-bold sm:text-[1.75rem]">{profile.display_name || profile.name}</EntityName>}
        description={<OverflowText lines={2}>{profile.description || '该 Bot 暂未填写简介。'}</OverflowText>}
        actions={user ? (
          <Button type="button" variant={favorited ? 'outline' : 'default'} size="sm" onClick={toggleFavorite}>
            <Star className={`size-4 ${favorited ? 'fill-current' : ''}`} />{favorited ? '已收藏' : '收藏'} {favCount}
          </Button>
        ) : undefined}
      />

      {actionError && <ErrorMsg msg={actionError} />}

      <DataRegion title="Bot 资料" description="身份、版本与当前评分" contentClassName="p-4">
        <div className="grid min-w-0 gap-3 lg:grid-cols-[minmax(0,1fr)_auto] lg:items-start">
          <div className="min-w-0 space-y-2">
            <div className="flex min-w-0 flex-wrap items-center gap-2">
              <Badge variant="outline"><GameIcon className="size-3.5" />{gameLabel(profile.game_id)}</Badge>
              {profile.ranking_eligible && profile.rank != null ? (
                <Badge variant="outline" className="font-mono">公开排名 #{profile.rank} / {profile.rank_total}</Badge>
              ) : (
                <Badge variant="secondary" className="font-mono">排名资格 {ratedMatches}/{profile.ranking_min_matches}</Badge>
              )}
              {!profile.is_active && <Badge variant="secondary">已停用</Badge>}
            </div>
            <div className="flex min-w-0 flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
              <span className="inline-flex min-w-0 items-center gap-1">所有者
                {profile.owner_name ? (
                  <Link to={`/user/${encodeURIComponent(profile.owner_name)}`} className="min-w-0 font-medium text-primary">
                    <OverflowText lines={2} tooltip={false} tooltipFocusable={false}>{profile.owner_display || profile.owner_name}</OverflowText>
                  </Link>
                ) : '—'}
              </span>
              <span>当前版本 v{profile.current_version ?? 1}</span>
              <span>创建于 {fmtDate(profile.created_at)}</span>
            </div>
          </div>
          <CopyIdentifier value={profile.id} />
        </div>
        <dl className="mt-3 grid min-w-0 grid-cols-2 gap-x-5 gap-y-3 border-t pt-3 text-sm md:grid-cols-4 xl:grid-cols-6">
          <div className="min-w-0">
            <dt className="text-xs text-muted-foreground">Rating / 95% 区间</dt>
            <dd className="mt-0.5 font-mono font-semibold tabular-nums">
              {fmtRating(profile.rating)}
              <span className="ml-1.5 font-normal text-muted-foreground">
                {profile.rd != null && profile.confidence_low != null && profile.confidence_high != null
                  ? `RD ${Number(profile.rd).toFixed(0)} · ${profile.confidence_low.toFixed(0)}–${profile.confidence_high.toFixed(0)}`
                  : '暂无区间'}
              </span>
            </dd>
          </div>
          <div className="min-w-0">
            <dt className="text-xs text-muted-foreground">公开名次</dt>
            <dd className="mt-0.5 font-mono font-semibold tabular-nums">
              {profile.rank == null ? '暂未入榜' : `第 ${profile.rank} / ${profile.rank_total} 名`}
              <span className="ml-1.5 font-normal text-muted-foreground">
                {profile.rank == null
                  ? `${ratedMatches}/${profile.ranking_min_matches} 场`
                  : profile.percentile == null ? '' : `前 ${profile.percentile.toFixed(1)}%`}
              </span>
            </dd>
          </div>
          <div className="min-w-0">
            <dt className="text-xs text-muted-foreground">计分样本</dt>
            <dd className="mt-0.5 font-mono font-semibold tabular-nums">{ratedMatches} 场 <span className="font-normal text-muted-foreground">· {profile.unique_opponents ?? 0} 个对手</span></dd>
          </div>
          <div className="min-w-0">
            <dt className="text-xs text-muted-foreground">战绩</dt>
            <dd className="mt-0.5 font-mono font-semibold tabular-nums">{profile.wins ?? 0} 胜 · {profile.draws ?? 0} 平 · {profile.losses ?? 0} 负</dd>
          </div>
          <div className="min-w-0">
            <dt className="text-xs text-muted-foreground">评分变化</dt>
            <dd className="mt-0.5 font-mono font-semibold tabular-nums">上次 {fmtSigned(profile.rating_delta)} <span className="font-normal text-muted-foreground">· 30 日 {fmtSigned(profile.recent_delta_30d)}</span></dd>
          </div>
          <div className="min-w-0">
            <dt className="text-xs text-muted-foreground">正常完成率</dt>
            <dd className="mt-0.5 font-mono font-semibold tabular-nums">{profile.normal_completion_rate == null ? '—' : fmtPct(profile.normal_completion_rate)} <span className="font-normal text-muted-foreground">· 技术负 {profile.technical_failures ?? 0}</span></dd>
          </div>
        </dl>
      </DataRegion>

      <Tabs defaultValue="history" className="w-full">
        <StickyToolbar label="Bot 详情分区">
          <TabsList className="w-full sm:w-auto">
            <TabsTrigger value="history"><HistoryIcon className="size-3.5" />对局历史 <span className="text-xs text-muted-foreground">{matchesTotal || matches.length}</span></TabsTrigger>
            <TabsTrigger value="opponents"><Swords className="size-3.5" />对手战绩 <span className="text-xs text-muted-foreground">{opponents.length}</span></TabsTrigger>
            <TabsTrigger value="rating"><Target className="size-3.5" />评分曲线</TabsTrigger>
          </TabsList>
        </StickyToolbar>

        <TabsContent value="history">
          <DataRegion title="对局历史" description={`第 ${matchesPage} 页 · 每页 ${matchesPerPage} 场`}>
            {matchesError ? (
              <ErrorMsg msg={matchesError} className="px-4 py-6" />
            ) : matchesLoading ? (
              <Loading text="正在加载对局…" />
            ) : (
              <>
                <div className="divide-y md:hidden" aria-label="Bot 对局历史移动视图">
                  {matches.length === 0 ? (
                    <EmptyState text="暂无对局" icon={<Swords className="size-5 opacity-50" />} className="py-8" />
                  ) : matches.map((match) => <MobileMatchCard key={match.id} match={match} botId={botId} />)}
                </div>
                <div className="hidden md:block">
                  <DataTable className="rounded-none border-0" scrollLabel="Bot 对局历史">
                    <Table aria-label="Bot 对局历史" className="min-w-[46rem]">
              <TableHeader>
                <TableRow>
                  <TableHead>时间</TableHead>
                  <TableHead className="min-w-[18rem]">对阵与所属用户</TableHead>
                  <TableHead>结果</TableHead>
                  <TableHead>性质</TableHead>
                  <TableHead className="text-right">操作</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {matches.length === 0 ? (
                  <TableRow><TableCell colSpan={5}><EmptyState text="暂无对局" icon={<Swords className="size-5 opacity-50" />} className="py-8" /></TableCell></TableRow>
                ) : (
                  matches.map((m) => {
                    const states = participantStates(m)
                    return (
                      <TableRow key={m.id}>
                        <TableCell className="whitespace-nowrap font-mono text-xs text-muted-foreground">
                          {fmtTime(m.created_at)}
                        </TableCell>
                        <TableCell className="max-w-[22rem] whitespace-normal">
                          <MatchParticipants source={m} states={states} />
                        </TableCell>
                        <TableCell>
                          <MatchOutcome match={m} botId={botId} />
                        </TableCell>
                        <TableCell className="text-xs text-muted-foreground">
                          <MatchNatureBadge matchType={m.match_type} source={m} />
                        </TableCell>
                        <TableCell className="text-right">
                          <Link to={`/match/${encodeURIComponent(m.id)}`} className="text-xs font-medium text-primary hover:underline">回放</Link>
                        </TableCell>
                      </TableRow>
                    )
                  })
                )}
              </TableBody>
                    </Table>
                  </DataTable>
                </div>
              </>
            )}
            <Pagination
              page={matchesPage}
              perPage={matchesPerPage}
              total={matchesTotal}
              onPageChange={setMatchesPage}
            />
          </DataRegion>
        </TabsContent>

        <TabsContent value="opponents">
          <DataRegion title="对手战绩" description="近期交手累计结果">
            <DataTable className="rounded-none border-0" scrollLabel="Bot 对手战绩">
              <Table aria-label="Bot 对手战绩" className="min-w-[32rem]">
              <TableHeader>
                <TableRow>
                  <TableHead className="min-w-[6rem]">对手</TableHead>
                  <TableHead>交手</TableHead>
                  <TableHead>胜</TableHead>
                  <TableHead>负</TableHead>
                  <TableHead>平</TableHead>
                  <TableHead>胜率</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {opponents.length === 0 ? (
                  <TableRow><TableCell colSpan={6}><EmptyState text="暂无对手战绩" /></TableCell></TableRow>
                ) : (
                  opponents.map((o) => {
                    const t = o.wins + o.losses + o.draws
                    const r = t > 0 ? (o.wins + o.draws * 0.5) / t : 0
                    return (
                      <TableRow key={o.opponent_id}>
                        <TableCell className="max-w-[12rem] whitespace-normal">
                          <Link to={`/bot/${o.opponent_id}`} className="block min-w-0 hover:text-primary">
                            <EntityName lines={2} tooltip={false} tooltipFocusable={false} className="text-sm hover:text-primary">{o.opponent_display || o.opponent_name || '未命名 Bot'}</EntityName>
                          </Link>
                        </TableCell>
                        <TableCell className="text-muted-foreground">{o.samples}</TableCell>
                        <TableCell className="text-success">{o.wins}</TableCell>
                        <TableCell className="text-destructive">{o.losses}</TableCell>
                        <TableCell className="text-muted-foreground">{o.draws}</TableCell>
                        <TableCell className="font-mono text-sm">{fmtPct(r)}</TableCell>
                      </TableRow>
                    )
                  })
                )}
              </TableBody>
              </Table>
            </DataTable>
          </DataRegion>
        </TabsContent>

        <TabsContent value="rating">
          <DataRegion title="评分变化" description={`Glicko-2 · ${history.length} 个数据点`} actions={<Trophy className="size-4 text-primary" />} contentClassName="p-3">
            <RatingChart points={history} />
          </DataRegion>
        </TabsContent>
      </Tabs>

      <Comments targetType="bot" targetId={String(botId)} />
    </PageFrame>
  )
}
