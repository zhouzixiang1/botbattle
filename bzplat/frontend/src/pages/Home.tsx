import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Heart, Eye, Flame, ArrowRight, Swords, LogIn, UserPlus, Upload, Trophy as TrophyIcon } from 'lucide-react'
import PageStub from '@/components/PageStub'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { EmptyState, ErrorMsg, Loading, StatusBadge } from '@/components/ui/status'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { useAuth } from '@/components/useAuth'
import { apiGet, errMsg } from '@/api'
import { GAMES, gameLabel, gameIcon, matchTypeBadge } from '@/lib/games'
import { fmtTime } from '@/lib/format'

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
  game_id?: string
  match_type?: string
  owner_id?: number | null
}

function botDisplayName(display: string | undefined, name: string | undefined, id: number | null): string {
  return display || name || (id != null ? `Bot #${id}` : '已删除 Bot')
}

function MatchBotLink({ id, name, wrap = false }: { id: number | null; name: string; wrap?: boolean }) {
  const textClass = wrap ? 'break-words' : 'truncate'
  if (id == null) return <span className={`min-w-0 ${textClass} text-muted-foreground`}>{name}</span>
  return (
    <Link
      to={`/bot/${id}`}
      className={`min-w-0 ${textClass} font-medium text-foreground hover:text-primary`}
      aria-label={`Bot ${name}`}
    >
      {name}
    </Link>
  )
}

export default function Home() {
  const { isLoggedIn } = useAuth()
  const [matches, setMatches] = useState<Match[]>([])
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [gameId, setGameId] = useState('')

  useEffect(() => {
    setLoading(true)
    const q = gameId ? `?limit=20&game_id=${encodeURIComponent(gameId)}` : '?limit=20'
    apiGet<{ matches: Match[] }>(`/api/matches${q}`)
      .then((d) => {
        // 首页去重：同一场赛事(contest_id)的批量对阵只保留最新 1 条，
        // 避免首页被一场赛事的内部对阵刷屏（让首页展示更多样化的动态）。
        const seenContest = new Set<string | number | null>()
        const deduped = (d.matches || []).filter((m) => {
          const cid = (m as Match & { contest_id?: string | number | null }).contest_id
          if (!cid) return true // 非赛事对局保留
          if (seenContest.has(cid)) return false // 同赛事只留首条
          seenContest.add(cid)
          return true
        })
        setMatches(deduped.slice(0, 8))
      })
      .catch((e) => setError(errMsg(e)))
      .finally(() => setLoading(false))
  }, [gameId])

  return (
    <PageStub
      title="首页"
      subtitle="最新对局 · 进行中与已完成"
      actions={
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          游戏
          <Select value={gameId || 'all'} onValueChange={(v) => setGameId(v === 'all' ? '' : v)}>
            <SelectTrigger className="h-9 w-[8.5rem]">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">全部</SelectItem>
              {GAMES.map((g) => (
                <SelectItem key={g.id} value={g.id}>
                  {g.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      }
    >
      {/* Hero 区 */}
      <Card className="mb-6 overflow-hidden border-primary/20 bg-gradient-to-br from-primary/5 via-card to-card">
        <CardContent className="grid gap-4 py-6">
          <div className="min-w-0 space-y-1.5">
            <h2 className="font-display text-2xl font-bold tracking-tight text-foreground">
              多游戏 Bot 竞赛平台
            </h2>
            <p className="max-w-4xl break-words text-sm text-muted-foreground">
              上传你的 Bot，在沙箱中对战。支持德州扑克 · 五子棋 · 点格棋，提供观赛、回放与 Glicko-2 排行榜。
            </p>
            <div className="flex flex-wrap gap-2 pt-1">
              {GAMES.map((g) => (
                <Badge key={g.id} variant="secondary" className="gap-1.5 px-2.5 py-1">
                  <g.icon className="size-3.5" />
                  {g.label}
                </Badge>
              ))}
            </div>
            {!isLoggedIn && (
              <div className="flex flex-wrap gap-2 pt-1">
                <Button asChild size="sm" className="gap-1.5 shadow-soft">
                  <Link to="/register">
                    <UserPlus className="size-3.5" />
                    注册账号
                  </Link>
                </Button>
                <Button asChild variant="outline" size="sm" className="gap-1.5">
                  <Link to="/login">
                    <LogIn className="size-3.5" />
                    登录
                  </Link>
                </Button>
              </div>
            )}
            {/* 已登录新用户「快速开始」三步指引（loading 完成后才显示，避免闪烁） */}
            {isLoggedIn && !loading && (
              <div className="grid gap-2 pt-2 sm:grid-cols-3">
                <Link
                  to="/my-bots"
                  className="group flex min-w-0 items-center gap-2.5 rounded-lg border border-border bg-card/60 px-3 py-2 text-sm transition-colors hover:border-primary/40 hover:bg-accent"
                >
                  <span className="flex size-6 shrink-0 items-center justify-center rounded-full bg-primary/10 font-mono text-xs font-semibold text-primary">1</span>
                  <Upload className="size-4 shrink-0 text-muted-foreground group-hover:text-primary" />
                  <span className="min-w-0 break-words">
                    <span className="block font-medium text-foreground">上传 Bot</span>
                    <span className="block text-xs text-muted-foreground">提交二进制，选择游戏</span>
                  </span>
                </Link>
                <Link
                  to="/challenge"
                  className="group flex min-w-0 items-center gap-2.5 rounded-lg border border-border bg-card/60 px-3 py-2 text-sm transition-colors hover:border-primary/40 hover:bg-accent"
                >
                  <span className="flex size-6 shrink-0 items-center justify-center rounded-full bg-primary/10 font-mono text-xs font-semibold text-primary">2</span>
                  <Swords className="size-4 shrink-0 text-muted-foreground group-hover:text-primary" />
                  <span className="min-w-0 break-words">
                    <span className="block font-medium text-foreground">发起挑战</span>
                    <span className="block text-xs text-muted-foreground">搜索对手或自博弈</span>
                  </span>
                </Link>
                <Link
                  to="/leaderboard"
                  className="group flex min-w-0 items-center gap-2.5 rounded-lg border border-border bg-card/60 px-3 py-2 text-sm transition-colors hover:border-primary/40 hover:bg-accent"
                >
                  <span className="flex size-6 shrink-0 items-center justify-center rounded-full bg-primary/10 font-mono text-xs font-semibold text-primary">3</span>
                  <TrophyIcon className="size-4 shrink-0 text-muted-foreground group-hover:text-primary" />
                  <span className="min-w-0 break-words">
                    <span className="block font-medium text-foreground">查看排行</span>
                    <span className="block text-xs text-muted-foreground">Glicko-2 天梯榜</span>
                  </span>
                </Link>
              </div>
            )}
          </div>
        </CardContent>
      </Card>



      {error && <ErrorMsg msg={error} className="mb-3" />}

      <Card className="overflow-hidden">
        <div className="flex items-center justify-between border-b border-border px-4 py-2.5">
          <span className="text-sm font-semibold text-foreground">最新对局</span>
          <Link to="/history" className="text-xs text-primary hover:underline">查看全部 →</Link>
        </div>
        {loading ? (
          <Loading />
        ) : matches.length === 0 ? (
          <EmptyState text="暂无对局" icon={<Swords className="size-7 opacity-40" />} />
        ) : (
          <>
          <div className="hidden overflow-x-auto md:block">
          <Table className="min-w-[34rem]">
            <TableHeader>
              <TableRow>
                <TableHead className="whitespace-nowrap">时间</TableHead>
                <TableHead className="whitespace-nowrap">游戏</TableHead>
                <TableHead className="min-w-[10rem]">对阵</TableHead>
                <TableHead className="whitespace-nowrap">状态</TableHead>
                <TableHead className="text-right whitespace-nowrap">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {matches.map((m) => {
                  const tb = matchTypeBadge(m.match_type)
                  const GameIcon = gameIcon(m.game_id)
                  const aName = botDisplayName(m.bot_a_display, m.bot_a_name, m.bot_a_id)
                  const bName = botDisplayName(m.bot_b_display, m.bot_b_name, m.bot_b_id)
                  return (
                    <TableRow key={m.id}>
                      <TableCell className="whitespace-nowrap font-mono text-xs text-muted-foreground">
                        {fmtTime(m.created_at)}
                      </TableCell>
                      <TableCell>
                        <div className="flex items-center gap-1.5">
                          <GameIcon className="size-3.5 shrink-0 text-muted-foreground" />
                          <span className="text-sm whitespace-nowrap">{gameLabel(m.game_id)}</span>
                          {tb && (
                            <Badge variant="outline" className={`text-[10px] ${tb.cls}`}>
                              {tb.label}
                            </Badge>
                          )}
                        </div>
                      </TableCell>
                      <TableCell className="max-w-[14rem]">
                        <div className="flex min-w-0 items-center gap-1.5 text-sm">
                          <MatchBotLink id={m.bot_a_id} name={aName} />
                          <span className="shrink-0 text-muted-foreground">vs</span>
                          <MatchBotLink id={m.bot_b_id} name={bName} />
                        </div>
                      </TableCell>
                      <TableCell>
                        <StatusBadge status={m.status} />
                      </TableCell>
                      <TableCell className="text-right">
                        <div className="flex justify-end gap-2">
                          <Link
                            className="inline-flex items-center gap-0.5 text-xs font-medium text-primary hover:underline"
                            to={`/match/${m.id}`}
                          >
                            {(m.status === 'pending' || m.status === 'running') ? '观赛' : '详情'}
                            <ArrowRight className="size-3" />
                          </Link>
                        </div>
                      </TableCell>
                    </TableRow>
                  )
                })}
            </TableBody>
          </Table>
          </div>
          <div className="divide-y divide-border md:hidden">
            {matches.map((m) => {
              const tb = matchTypeBadge(m.match_type)
              const GameIcon = gameIcon(m.game_id)
              const aName = botDisplayName(m.bot_a_display, m.bot_a_name, m.bot_a_id)
              const bName = botDisplayName(m.bot_b_display, m.bot_b_name, m.bot_b_id)
              return (
                <article key={m.id} className="space-y-2.5 px-4 py-3">
                  <div className="flex min-w-0 flex-wrap items-center gap-1.5">
                    <GameIcon className="size-3.5 shrink-0 text-muted-foreground" />
                    <span className="text-sm font-medium">{gameLabel(m.game_id)}</span>
                    {tb && <Badge variant="outline" className={`text-[10px] ${tb.cls}`}>{tb.label}</Badge>}
                    <span className="ml-auto"><StatusBadge status={m.status} /></span>
                  </div>
                  <div className="grid min-w-0 grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)] items-start gap-1.5 text-sm">
                    <MatchBotLink id={m.bot_a_id} name={aName} wrap />
                    <span className="shrink-0 text-muted-foreground">vs</span>
                    <MatchBotLink id={m.bot_b_id} name={bName} wrap />
                  </div>
                  <div className="flex items-center justify-between gap-3 text-xs text-muted-foreground">
                    <time className="min-w-0 break-words font-mono">{fmtTime(m.created_at)}</time>
                    <Link className="inline-flex shrink-0 items-center gap-0.5 font-medium text-primary hover:underline" to={`/match/${m.id}`}>
                      {(m.status === 'pending' || m.status === 'running') ? '观赛' : '详情'}
                      <ArrowRight className="size-3" />
                    </Link>
                  </div>
                </article>
              )
            })}
          </div>
          </>
        )}
      </Card>

      <LikedTopMatches />
    </PageStub>
  )
}

interface LikedMatch {
  id: string
  game_id: string
  likes_count: number
  views_count: number
  bot_a_name?: string
  bot_a_display?: string
  bot_b_name?: string
  bot_b_display?: string
  created_at?: string
}

function LikedTopMatches() {
  const [matches, setMatches] = useState<LikedMatch[]>([])
  useEffect(() => {
    apiGet<{ matches: LikedMatch[] }>('/api/matches/liked-top?limit=5')
      .then((d) => setMatches(d.matches || []))
      .catch(() => {})
  }, [])
  if (matches.length === 0) return null
  return (
    <Card className="mt-6">
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <Flame className="size-4 text-warning" />
          热门对局（点赞榜）
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-2">
        {matches.map((m) => (
          <Link
            key={m.id}
            to={`/match/${encodeURIComponent(m.id)}`}
            className="grid min-w-0 gap-2 rounded-lg border border-border px-3 py-2 text-sm transition-colors hover:bg-accent sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center"
          >
            <span className="min-w-0 break-words font-medium text-foreground">
              {m.bot_a_display || m.bot_a_name || '已删除 Bot'} vs {m.bot_b_display || m.bot_b_name || '已删除 Bot'}
            </span>
            <Badge variant="outline" className="text-[10px]">
              {gameLabel(m.game_id)}
            </Badge>
            <span className="flex shrink-0 items-center gap-3 text-xs text-muted-foreground sm:justify-self-end">
              <span className="flex items-center gap-1">
                <Heart className="size-3 text-destructive" /> {m.likes_count}
              </span>
              <span className="flex items-center gap-1">
                <Eye className="size-3" /> {m.views_count}
              </span>
            </span>
          </Link>
        ))}
      </CardContent>
    </Card>
  )
}
