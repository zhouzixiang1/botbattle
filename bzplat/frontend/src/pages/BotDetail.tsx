import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { Star, ArrowLeft, Trophy, Swords, Target, History as HistoryIcon } from 'lucide-react'
import { CartesianGrid, Line, LineChart, XAxis, YAxis } from 'recharts'
import PageStub from '@/components/PageStub'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
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
import { EmptyState, ErrorMsg, Loading } from '@/components/ui/status'
import { MetricCard } from '@/components/ui/metric-card'
import { TierBadge } from '@/components/tier-badge'
import Comments from '@/components/Comments'
import { apiGet, apiJson, errMsg } from '@/api'
import { useAuth } from '@/components/useAuth'
import { gameLabel, gameIcon } from '@/lib/games'

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
  is_public: number | boolean
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
  earnings_a?: number
  earnings_b?: number
  hands_played?: number
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
  const data = points.map((p) => ({
    idx: points.indexOf(p) + 1,
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
  const { user } = useAuth()
  const [profile, setProfile] = useState<BotProfile | null>(null)
  const [matches, setMatches] = useState<MatchRow[]>([])
  const [opponents, setOpponents] = useState<OpponentRow[]>([])
  const [history, setHistory] = useState<RatingPoint[]>([])
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [favorited, setFavorited] = useState(false)
  const [favCount, setFavCount] = useState(0)

  useEffect(() => {
    if (!botId) return
    setLoading(true)
    Promise.all([
      apiGet<{ profile: BotProfile }>(`/api/bots/${botId}/profile`),
      apiGet<{ matches: MatchRow[] }>(`/api/bots/${botId}/matches?limit=30`),
      apiGet<{ opponents: OpponentRow[] }>(`/api/bots/${botId}/opponents?limit=20`),
      apiGet<{ history: RatingPoint[] }>(`/api/bots/${botId}/rating-history?limit=100`),
    ])
      .then(([p, m, o, h]) => {
        setProfile(p.profile)
        setMatches(m.matches || [])
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

  function toggleFavorite() {
    if (!user) return
    const method = favorited ? 'DELETE' : 'POST'
    apiJson(`/api/bots/${botId}/favorite`, method)
      .then(() => {
        setFavorited(!favorited)
        setFavCount((c) => c + (favorited ? -1 : 1))
      })
      .catch((e) => setError(errMsg(e)))
  }

  if (loading) {
    return (
      <PageStub title="Bot 详情">
        <Loading />
      </PageStub>
    )
  }
  if (error || !profile) {
    return (
      <PageStub title="Bot 详情">
        <ErrorMsg msg={error || 'Bot 不存在'} className="mb-3" />
        <Button asChild variant="ghost" size="sm">
          <Link to="/leaderboard"><ArrowLeft className="size-4" /> 返回排行榜</Link>
        </Button>
      </PageStub>
    )
  }

  const wr = winRate(profile)
  const total = (profile.wins ?? 0) + (profile.losses ?? 0) + (profile.draws ?? 0)
  const GameIcon = gameIcon(profile.game_id)

  return (
    <PageStub title={profile.display_name || profile.name}>
      {/* 顶部信息卡 */}
      <Card className="mb-5">
        <CardContent className="flex flex-col gap-5 py-5 lg:flex-row lg:items-start">
          <div className="min-w-0 flex-1 space-y-2">
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="text-xl font-bold text-foreground">
                {profile.display_name || profile.name}
              </h2>
              {profile.tier_name && <TierBadge rating={profile.rating} label={profile.tier_name} />}
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
              <span className="font-mono">{profile.format}/{profile.os}-{profile.arch}</span>
              {profile.created_at && <span>创建于 {profile.created_at.slice(0, 10)}</span>}
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

          {/* 指标 */}
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 lg:w-[24rem] lg:shrink-0">
            <MetricCard label="Rating" value={profile.rating != null ? Number(profile.rating).toFixed(0) : '—'} hint={profile.rd != null ? `rd ${Number(profile.rd).toFixed(0)}` : undefined} />
            <MetricCard label="胜率" value={fmtPct(wr)} hint={`共 ${total} 场`} />
            <MetricCard label="胜" value={profile.wins ?? 0} danger={false} />
            <MetricCard label="负/平" value={profile.losses ?? 0} hint={`平 ${profile.draws ?? 0}`} danger />
          </div>
        </CardContent>
      </Card>

      <Tabs defaultValue="history" className="w-full">
        <TabsList>
          <TabsTrigger value="history" className="gap-1.5"><HistoryIcon className="size-3.5" />对局历史 ({matches.length})</TabsTrigger>
          <TabsTrigger value="opponents" className="gap-1.5"><Swords className="size-3.5" />对手战绩 ({opponents.length})</TabsTrigger>
          <TabsTrigger value="rating" className="gap-1.5"><Target className="size-3.5" />评分曲线</TabsTrigger>
        </TabsList>

        {/* 对局历史 */}
        <TabsContent value="history">
          <Card>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>时间</TableHead>
                  <TableHead>对手</TableHead>
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
                          {m.created_at?.slice(0, 16).replace('T', ' ') || '—'}
                        </TableCell>
                        <TableCell>
                          <Link to={`/bot/${oppId}`} className="font-medium text-foreground hover:text-primary">
                            {oppName || `#${oppId}`}
                          </Link>
                        </TableCell>
                        <TableCell>
                          {m.status === 'completed' ? (
                            <Badge variant={outcome === 'win' ? 'default' : outcome === 'loss' ? 'destructive' : 'secondary'}>
                              {outcome === 'win' ? '胜' : outcome === 'loss' ? '负' : '平'}
                            </Badge>
                          ) : (
                            <span className="text-xs text-muted-foreground">{m.status}</span>
                          )}
                        </TableCell>
                        <TableCell className="hidden text-xs text-muted-foreground sm:table-cell">{m.match_type}</TableCell>
                        <TableCell className="text-right">
                          <Link to={`/match/${encodeURIComponent(m.id)}`} className="text-xs font-medium text-primary hover:underline">回放</Link>
                        </TableCell>
                      </TableRow>
                    )
                  })
                )}
              </TableBody>
            </Table>
          </Card>
        </TabsContent>

        {/* 对手战绩 */}
        <TabsContent value="opponents">
          <Card>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>对手</TableHead>
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
                        <TableCell>
                          <Link to={`/bot/${o.opponent_id}`} className="font-medium text-foreground hover:text-primary">
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
