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
import { GAMES, gameLabel, gameIcon, matchTypeBadge, isBoardGame } from '@/lib/games'
import { fmtTime } from '@/lib/format'

interface Match {
  id: string
  status: string
  bot_a_id: number
  bot_b_id: number
  bot_a_name?: string
  bot_b_name?: string
  bot_a_display?: string
  bot_b_display?: string
  earnings_a?: number  // 已废弃（result.deltas 取代），保留向后兼容旧 API 响应
  earnings_b?: number
  created_at?: string
  match_config?: Record<string, number>
  result?: { hands_played?: number; deltas?: number[]; net_bb?: number }
  game_id?: string
  match_type?: string
  owner_id?: number | null
}

export default function Home() {
  const { isLoggedIn } = useAuth()
  const [matches, setMatches] = useState<Match[]>([])
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [gameId, setGameId] = useState('')

  useEffect(() => {
    setLoading(true)
    const q = gameId ? `?limit=50&game_id=${encodeURIComponent(gameId)}` : '?limit=50'
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
        setMatches(deduped.slice(0, 30))
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
        <CardContent className="flex flex-col items-start gap-4 py-6 sm:flex-row sm:items-center sm:justify-between">
          <div className="space-y-1.5">
            <h2 className="font-display text-2xl font-bold tracking-tight text-foreground">
              多游戏 Bot 竞赛平台
            </h2>
            <p className="text-sm text-muted-foreground">
              上传你的 Bot，在沙箱中对战。支持德州扑克 · 五子棋 · 点格棋，提供观赛、回放与 Glicko-2 排行榜。
            </p>
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
              <div className="flex flex-col gap-3 pt-1 sm:flex-row">
                <Link
                  to="/my-bots"
                  className="group flex flex-1 items-center gap-2.5 rounded-lg border border-border bg-card/60 px-3 py-2 text-sm transition-colors hover:border-primary/40 hover:bg-accent"
                >
                  <span className="flex size-6 shrink-0 items-center justify-center rounded-full bg-primary/10 font-mono text-xs font-semibold text-primary">1</span>
                  <Upload className="size-4 shrink-0 text-muted-foreground group-hover:text-primary" />
                  <span className="min-w-0">
                    <span className="block font-medium text-foreground">上传 Bot</span>
                    <span className="block text-xs text-muted-foreground">提交二进制，选择游戏</span>
                  </span>
                </Link>
                <Link
                  to="/challenge"
                  className="group flex flex-1 items-center gap-2.5 rounded-lg border border-border bg-card/60 px-3 py-2 text-sm transition-colors hover:border-primary/40 hover:bg-accent"
                >
                  <span className="flex size-6 shrink-0 items-center justify-center rounded-full bg-primary/10 font-mono text-xs font-semibold text-primary">2</span>
                  <Swords className="size-4 shrink-0 text-muted-foreground group-hover:text-primary" />
                  <span className="min-w-0">
                    <span className="block font-medium text-foreground">发起挑战</span>
                    <span className="block text-xs text-muted-foreground">搜索对手或自博弈</span>
                  </span>
                </Link>
                <Link
                  to="/leaderboard"
                  className="group flex flex-1 items-center gap-2.5 rounded-lg border border-border bg-card/60 px-3 py-2 text-sm transition-colors hover:border-primary/40 hover:bg-accent"
                >
                  <span className="flex size-6 shrink-0 items-center justify-center rounded-full bg-primary/10 font-mono text-xs font-semibold text-primary">3</span>
                  <TrophyIcon className="size-4 shrink-0 text-muted-foreground group-hover:text-primary" />
                  <span className="min-w-0">
                    <span className="block font-medium text-foreground">查看排行</span>
                    <span className="block text-xs text-muted-foreground">Glicko-2 天梯榜</span>
                  </span>
                </Link>
              </div>
            )}
          </div>
          <div className="flex flex-wrap gap-2">
            {GAMES.map((g) => (
              <Badge key={g.id} variant="secondary" className="gap-1.5 px-2.5 py-1">
                <g.icon className="size-3.5" />
                {g.label}
              </Badge>
            ))}
          </div>
        </CardContent>
      </Card>



      {error && <ErrorMsg msg={error} className="mb-3" />}

      <Card className="overflow-hidden">
        <div className="overflow-x-auto">
          <Table className="min-w-[40rem]">
            <TableHeader>
              <TableRow>
                <TableHead className="whitespace-nowrap">时间</TableHead>
                <TableHead className="whitespace-nowrap">游戏</TableHead>
                <TableHead className="min-w-[10rem]">对阵</TableHead>
                <TableHead className="whitespace-nowrap">状态</TableHead>
                <TableHead className="whitespace-nowrap">进度</TableHead>
                <TableHead className="text-right whitespace-nowrap">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {loading ? (
                <TableRow>
                  <TableCell colSpan={6}>
                    <Loading />
                  </TableCell>
                </TableRow>
              ) : matches.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={6}>
                    <EmptyState
                      text="暂无对局"
                      icon={<Swords className="size-7 opacity-40" />}
                    />
                  </TableCell>
                </TableRow>
              ) : (
                matches.map((m) => {
                  const tb = matchTypeBadge(m.match_type)
                  const GameIcon = gameIcon(m.game_id)
                  const aName = m.bot_a_display || m.bot_a_name || `#${m.bot_a_id}`
                  const bName = m.bot_b_display || m.bot_b_name || `#${m.bot_b_id}`
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
                          <Link
                            to={`/bot/${m.bot_a_id}`}
                            className="min-w-0 truncate font-medium text-foreground hover:text-primary"
                            title={aName}
                          >
                            {aName}
                          </Link>
                          <span className="shrink-0 text-muted-foreground">vs</span>
                          <Link
                            to={`/bot/${m.bot_b_id}`}
                            className="min-w-0 truncate font-medium text-foreground hover:text-primary"
                            title={bName}
                          >
                            {bName}
                          </Link>
                        </div>
                      </TableCell>
                      <TableCell>
                        <StatusBadge status={m.status} />
                      </TableCell>
                      <TableCell className="font-mono text-xs whitespace-nowrap text-muted-foreground">
                        {isBoardGame(m.game_id)
                          ? `${m.result?.hands_played ?? 0} 步`
                          : `${m.result?.hands_played ?? 0} 手`}
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
                })
              )}
            </TableBody>
          </Table>
        </div>
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
            className="flex flex-wrap items-center gap-2 rounded-lg border border-border px-3 py-2 text-sm transition-colors hover:bg-accent"
          >
            <span className="font-medium text-foreground">
              {m.bot_a_display || m.bot_a_name} vs {m.bot_b_display || m.bot_b_name}
            </span>
            <Badge variant="outline" className="text-[10px]">
              {gameLabel(m.game_id)}
            </Badge>
            <span className="ml-auto flex items-center gap-3 text-xs text-muted-foreground">
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
