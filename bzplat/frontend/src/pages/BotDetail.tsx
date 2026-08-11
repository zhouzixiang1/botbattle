import { useEffect, useState } from 'react'
import { Link, useParams, useNavigate } from 'react-router-dom'
import { Star, ArrowLeft, Trophy, Swords, Target, History as HistoryIcon } from 'lucide-react'
import { CartesianGrid, Line, LineChart, XAxis, YAxis } from 'recharts'
import { DataRegion, PageFrame, PageHeader, StickyToolbar, SummaryStrip } from '@/components/layout'
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
import { gameLabel, gameIcon, matchTypeBadge } from '@/lib/games'
import { fmtTime, fmtRating, fmtDate } from '@/lib/format'
import { CopyIdentifier, SummaryMetric } from '@/pages/public-page-ui'

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

interface MatchRow {
  id: string
  game_id: string
  status: string
  winner: number | null
  match_type: string
  bot_a_id: number | null
  bot_b_id: number | null
  bot_a_name: string
  bot_b_name: string
  bot_a_display?: string
  bot_b_display?: string
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

function matchOutcome(m: MatchRow, botId: number): 'win' | 'loss' | 'draw' | '' {
  if (m.status !== 'completed') return ''
  if (m.winner === null) return 'draw'
  const isA = m.bot_a_id === botId
  const won = (isA && m.winner === 0) || (!isA && m.winner === 1)
  return won ? 'win' : 'loss'
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
        <SummaryStrip columns={4}>{[0, 1, 2, 3].map((item) => <Skeleton key={item} className="h-14" />)}</SummaryStrip>
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

      <DataRegion title="Bot 概览" description="公开身份、游戏维度与版本信息" contentClassName="p-4">
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
      </DataRegion>

      <SummaryStrip columns={4}>
        <SummaryMetric
          label="Rating"
          value={fmtRating(profile.rating)}
          detail={profile.rd != null && profile.confidence_low != null && profile.confidence_high != null
            ? `RD ${Number(profile.rd).toFixed(0)} · 95% ${profile.confidence_low.toFixed(0)}–${profile.confidence_high.toFixed(0)}`
            : 'RD / 95% 区间暂无数据'}
        />
        <SummaryMetric
          label="公开名次"
          value={profile.rank == null ? '—' : `#${profile.rank} / ${profile.rank_total}`}
          detail={profile.rank == null
            ? `资格进度 ${(profile.ranking_progress * 100).toFixed(0)}%（${ratedMatches}/${profile.ranking_min_matches}）`
            : `百分位 ${profile.percentile == null ? '—' : `${profile.percentile.toFixed(1)}%`}`}
        />
        <SummaryMetric label="计分样本" value={ratedMatches} detail={`${profile.unique_opponents ?? 0} 个不同对手`} />
        <SummaryMetric
          label="正常完成率"
          value={profile.normal_completion_rate == null ? '—' : fmtPct(profile.normal_completion_rate)}
          detail={`${profile.technical_failures ?? 0} 次本 Bot 技术负 / ${ratedMatches} 场计分对局`}
        />
      </SummaryStrip>

      <SummaryStrip columns={5} label="评分变化与赛果">
        <SummaryMetric label="上次变化" value={fmtSigned(profile.rating_delta)} detail="相邻评分快照" />
        <SummaryMetric label="30 日变化" value={fmtSigned(profile.recent_delta_30d)} detail="缺窗口起点时为 —" />
        <SummaryMetric label="胜" value={profile.wins ?? 0} detail="计分对局" />
        <SummaryMetric label="平" value={profile.draws ?? 0} detail="计分对局" />
        <SummaryMetric label="负" value={profile.losses ?? 0} detail="计分对局" />
      </SummaryStrip>

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
              <DataTable className="rounded-none border-0" scrollLabel="Bot 对局历史">
                <Table aria-label="Bot 对局历史" className="min-w-[38rem]">
              <TableHeader>
                <TableRow>
                  <TableHead>时间</TableHead>
                  <TableHead className="min-w-[6rem]">对手</TableHead>
                  <TableHead>结果</TableHead>
                  <TableHead className="hidden sm:table-cell">类型</TableHead>
                  <TableHead className="text-right">操作</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {matches.length === 0 ? (
                  <TableRow><TableCell colSpan={5}><EmptyState text="暂无对局" icon={<Swords className="size-5 opacity-50" />} className="py-8" /></TableCell></TableRow>
                ) : (
                  matches.map((m) => {
                    const isA = m.bot_a_id === botId
                    const oppName = isA ? m.bot_b_display || m.bot_b_name : m.bot_a_display || m.bot_a_name
                    const oppId = isA ? m.bot_b_id : m.bot_a_id
                    const outcome = matchOutcome(m, botId)
                    return (
                      <TableRow key={m.id}>
                        <TableCell className="whitespace-nowrap font-mono text-xs text-muted-foreground">
                          {fmtTime(m.created_at)}
                        </TableCell>
                        <TableCell className="max-w-[12rem] whitespace-normal">
                          {oppId != null ? (
                            <Link to={`/bot/${oppId}`} className="block min-w-0 hover:text-primary">
                              <EntityName lines={2} tooltip={false} tooltipFocusable={false} className="text-sm hover:text-primary">{oppName || '未命名 Bot'}</EntityName>
                            </Link>
                          ) : (
                            <span className="text-muted-foreground">已删除 Bot</span>
                          )}
                        </TableCell>
                        <TableCell>
                          {m.status === 'completed' ? (
                            <Badge variant={outcome === 'win' ? 'default' : outcome === 'loss' ? 'destructive' : 'secondary'}>
                              {outcome === 'win' ? '胜' : outcome === 'loss' ? '负' : '平'}
                            </Badge>
                          ) : (
                            <StatusBadge status={m.status} />
                          )}
                        </TableCell>
                        <TableCell className="hidden text-xs text-muted-foreground sm:table-cell">
                          {(() => {
                            const tb = matchTypeBadge(m.match_type)
                            return tb ? (
                              <Badge variant="outline" className={`text-[10px] ${tb.cls}`}>{tb.label}</Badge>
                            ) : (
                              m.match_type || '—'
                            )
                          })()}
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
