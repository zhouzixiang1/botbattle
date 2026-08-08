import { useEffect, useState } from 'react'
import { Link, useParams, useNavigate } from 'react-router-dom'
import { Star, ArrowLeft, Trophy, Swords, Target, History as HistoryIcon } from 'lucide-react'
import { CartesianGrid, Line, LineChart, XAxis, YAxis } from 'recharts'
import PageStub from '@/components/PageStub'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { ChartContainer, ChartTooltip, ChartTooltipContent, type ChartConfig } from '@/components/ui/chart'
import { EmptyState, ErrorMsg, StatusBadge } from '@/components/ui/status'
import { MetricCard } from '@/components/ui/metric-card'
import { TierBadge } from '@/components/tier-badge'
import Comments from '@/components/Comments'
import Pagination from '@/components/Pagination'
import { apiGet, apiJson, errMsg } from '@/api'
import { toast } from 'sonner'
import { useAuth } from '@/components/useAuth'
import { gameLabel, gameIcon, matchTypeBadge } from '@/lib/games'
import { fmtTime, fmtRating, fmtDate } from '@/lib/format'

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
  format?: string
  os?: string
  arch?: string
  current_version?: number
  created_at?: string
  rating?: number
  rd?: number
  vol?: number
  wins?: number
  losses?: number
  draws?: number
  net_chips?: number
  matches_played?: number
  rated_at?: string
  tier_level?: number
  tier_key?: string
  tier_name?: string
}

interface MatchRow {
  id: string
  game_id: string
  status: string
  winner: number | null
  match_type: string
  bot_a_id: number
  bot_b_id: number
  bot_a_name: string
  bot_b_name: string
  bot_a_display?: string
  bot_b_display?: string
  match_config?: Record<string, number>
  result?: { hands_played?: number; deltas?: number[]; net_bb?: number }
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
function winRate(p?: BotProfile): number {
  const w = p?.wins ?? 0
  const l = p?.losses ?? 0
  const d = p?.draws ?? 0
  const total = w + l + d
  if (total === 0) return 0
  return (w + d * 0.5) / total
}

function fmtPct(r: number): string {
  return `${(r * 100).toFixed(1)}%`
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
    return <EmptyState text="评分数据不足，至少需 2 个数据点" icon={<Target className="size-7 opacity-40" />} />
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
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [favorited, setFavorited] = useState(false)
  const [favCount, setFavCount] = useState(0)
  // 对局历史分页
  const [matchesPage, setMatchesPage] = useState(1)
  const [matchesTotal, setMatchesTotal] = useState(0)
  const matchesPerPage = 30

  useEffect(() => {
    if (!botId) return
    setLoading(true)
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
      .catch((e) => setError(errMsg(e)))
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
    if (!botId) return
    apiGet<{ matches: MatchRow[]; total?: number }>(
      `/api/bots/${botId}/matches?page=${matchesPage}&per_page=${matchesPerPage}`,
    )
      .then((m) => {
        setMatches(m.matches || [])
        if (m.total !== undefined) setMatchesTotal(m.total)
      })
      .catch((e) => setError(errMsg(e)))
  }, [botId, matchesPage])

  function toggleFavorite() {
    if (!user) return
    const method = favorited ? 'DELETE' : 'POST'
    apiJson(`/api/bots/${botId}/favorite`, method)
      .then(() => {
        setFavorited(!favorited)
        setFavCount((c) => c + (favorited ? -1 : 1))
        toast.success(favorited ? '已取消收藏' : '收藏成功')
      })
      .catch((e) => setError(errMsg(e)))
  }

  if (loading) {
    return (
      <PageStub title="Bot 详情">
        {/* 骨架屏：信息卡 + 指标卡轮廓，避免裸 spinner 布局抖动 */}
        <Card className="mb-4">
          <CardContent className="flex flex-col gap-5 py-5 lg:flex-row lg:items-start">
            <div className="flex-1 space-y-2">
              <Skeleton className="h-7 w-40" />
              <Skeleton className="h-4 w-32" />
              <Skeleton className="h-4 w-48" />
            </div>
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 lg:w-[32rem]">
              {[0, 1, 2, 3].map((i) => <Skeleton key={i} className="h-20" />)}
            </div>
          </CardContent>
        </Card>
        <Card><CardContent className="py-4"><Skeleton className="h-32" /></CardContent></Card>
      </PageStub>
    )
  }
  if (error || !profile) {
    return (
      <PageStub title="Bot 详情">
        <ErrorMsg msg={error || 'Bot 不存在'} className="mb-3" />
        {/* 后退策略：有历史则返回上一页，直接 URL 进入（无历史）fallback 到排行榜 */}
        <Button
          variant="ghost"
          size="sm"
          className="gap-1"
          onClick={() => (window.history.length > 1 ? navigate(-1) : navigate('/leaderboard'))}
        >
          <ArrowLeft className="size-4" /> 返回
        </Button>
      </PageStub>
    )
  }

  const wr = winRate(profile)
  // 总场次优先用后端 matches_played（store 聚合，含未评分类对局），否则本地兜底。
  const total = profile.matches_played ?? ((profile.wins ?? 0) + (profile.losses ?? 0) + (profile.draws ?? 0))
  const GameIcon = gameIcon(profile.game_id)

  return (
    <PageStub title={profile.display_name || profile.name}>
      {/* 顶部信息卡 */}
      <Card className="mb-5">
        <CardContent className="flex flex-col gap-5 py-5 lg:flex-row lg:items-start">
          <div className="min-w-0 flex-1 space-y-2">
            <div className="flex min-w-0 flex-wrap items-center gap-2">
              <h2 className="max-w-full break-words text-xl font-bold text-foreground [overflow-wrap:anywhere]">
                {profile.display_name || profile.name}
              </h2>
              {profile.tier_name && (
                <TierBadge rating={profile.rating} label={profile.tier_name} gameId={profile.game_id} tierKey={profile.tier_key} />
              )}
              {!profile.is_active && <Badge variant="secondary">已停用</Badge>}
            </div>
            <p className="text-sm text-muted-foreground">@{profile.name}</p>
            <Badge variant="outline" className="gap-1.5">
              <GameIcon className="size-3.5" />
              {gameLabel(profile.game_id)}
            </Badge>
            {profile.description && (
              <p className="text-sm text-muted-foreground">{profile.description}</p>
            )}
            <div className="flex flex-wrap gap-x-4 gap-y-1 pt-1 text-xs text-muted-foreground">
              <span>
                所有者：
                {profile.owner_name ? (
                  <Link to={`/user/${encodeURIComponent(profile.owner_name)}`} className="text-primary hover:underline">
                    {profile.owner_display || profile.owner_name}
                  </Link>
                ) : (
                  '—'
                )}
              </span>
              <span>版本 v{profile.current_version ?? 1}</span>
              <span className="max-w-full break-all font-mono">{profile.format}/{profile.os}-{profile.arch}</span>
              {profile.created_at && <span>创建于 {fmtDate(profile.created_at)}</span>}
            </div>
            {user && (
              <Button
                type="button"
                variant={favorited ? 'outline' : 'default'}
                size="sm"
                onClick={toggleFavorite}
                className="mt-1 gap-1.5"
              >
                <Star className={`size-3.5 ${favorited ? 'fill-current' : ''}`} />
                {favorited ? '已收藏' : '收藏'}（{favCount}）
              </Button>
            )}
          </div>

          {/* 指标（plain：嵌套在本 Card 内，无边框避免 Card 套 Card） */}
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 lg:w-[32rem] lg:shrink-0">
            <MetricCard plain label="Rating" value={fmtRating(profile.rating)} hint={profile.rd != null ? `rd ${Number(profile.rd).toFixed(0)}` : undefined} />
            <MetricCard plain label="胜率" value={fmtPct(wr)} hint={`共 ${total} 场`} />
            <MetricCard plain label="胜" value={profile.wins ?? 0} danger={false} />
            <MetricCard plain label="负/平" value={profile.losses ?? 0} hint={`平 ${profile.draws ?? 0}`} danger />
          </div>
        </CardContent>
      </Card>

      <Tabs defaultValue="history" className="w-full">
        <TabsList className="w-full">
          <TabsTrigger value="history" className="min-w-0 gap-1.5"><HistoryIcon className="size-3.5 shrink-0" /><span className="truncate">对局历史</span> <span className="hidden text-xs text-muted-foreground sm:inline">({matchesTotal || matches.length})</span></TabsTrigger>
          <TabsTrigger value="opponents" className="min-w-0 gap-1.5"><Swords className="size-3.5 shrink-0" /><span className="truncate">对手战绩</span> <span className="hidden text-xs text-muted-foreground sm:inline">({opponents.length})</span></TabsTrigger>
          <TabsTrigger value="rating" className="min-w-0 gap-1.5"><Target className="size-3.5 shrink-0" /><span className="truncate">评分曲线</span></TabsTrigger>
        </TabsList>

        {/* 对局历史 */}
        <TabsContent value="history">
          <Card className="overflow-hidden">
            <div className="overflow-x-auto">
            <Table className="min-w-[36rem]">
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
                  <TableRow><TableCell colSpan={5}><EmptyState text="暂无对局" icon={<Swords className="size-7 opacity-40" />} /></TableCell></TableRow>
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
                        <TableCell className="max-w-[10rem]">
                          <Link to={`/bot/${oppId}`} className="block truncate font-medium text-foreground hover:text-primary" title={oppName || `#${oppId}`}>
                            {oppName || `#${oppId}`}
                          </Link>
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
            </div>
            <Pagination
              page={matchesPage}
              perPage={matchesPerPage}
              total={matchesTotal}
              onPageChange={setMatchesPage}
            />
          </Card>
        </TabsContent>

        {/* 对手战绩 */}
        <TabsContent value="opponents">
          <Card className="overflow-hidden">
            <div className="overflow-x-auto">
            <Table className="min-w-[32rem]">
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
                        <TableCell className="max-w-[10rem]">
                          <Link to={`/bot/${o.opponent_id}`} className="block truncate font-medium text-foreground hover:text-primary" title={o.opponent_display || o.opponent_name || `#${o.opponent_id}`}>
                            {o.opponent_display || o.opponent_name || `#${o.opponent_id}`}
                          </Link>
                        </TableCell>
                        <TableCell className="text-muted-foreground">{o.samples || t}</TableCell>
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
            </div>
          </Card>
        </TabsContent>

        {/* 评分曲线 */}
        <TabsContent value="rating">
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2 text-base">
                <Trophy className="size-4 text-primary" />
                评分变化（Glicko-2，{history.length} 个数据点）
              </CardTitle>
            </CardHeader>
            <CardContent>
              <RatingChart points={history} />
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>

      <Comments targetType="bot" targetId={String(botId)} />
    </PageStub>
  )
}
