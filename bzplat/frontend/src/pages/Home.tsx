import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  ArrowRight,
  Bot,
  Eye,
  Flame,
  Heart,
  LogIn,
  Swords,
  UserPlus,
} from 'lucide-react'

import { apiGet, errMsg } from '@/api'
import { DataRegion, PageFrame, PageHeader, StickyToolbar } from '@/components/layout'
import { MatchNatureBadge, MatchParticipants } from '@/components/MatchParticipants'
import { useAuth } from '@/components/useAuth'
import { Button } from '@/components/ui/button'
import {
  DataTable,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { EmptyState, ErrorMsg, Loading, StatusBadge } from '@/components/ui/status'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { fmtTime } from '@/lib/format'
import { GAMES, gameIcon, gameLabel } from '@/lib/games'
import type { MatchParticipantSource } from '@/lib/match-participants'

interface Match extends MatchParticipantSource {
  id: string
  status: string
  bot_a_id: number | null
  bot_b_id: number | null
  created_at?: string
  game_id?: string
  match_type?: string
  contest_id?: string | number | null
}

interface LikedMatch extends MatchParticipantSource {
  id: string
  game_id: string
  likes_count: number
  views_count: number
  match_type?: string
  created_at?: string
}

export default function Home() {
  const { isLoggedIn } = useAuth()
  const [matches, setMatches] = useState<Match[]>([])
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [gameId, setGameId] = useState('')

  useEffect(() => {
    setLoading(true)
    setError('')
    const query = gameId ? `?limit=20&game_id=${encodeURIComponent(gameId)}` : '?limit=20'
    apiGet<{ matches: Match[] }>(`/api/matches${query}`)
      .then((data) => {
        const seenContests = new Set<string | number>()
        const deduped = (data.matches || []).filter((match) => {
          if (!match.contest_id) return true
          if (seenContests.has(match.contest_id)) return false
          seenContests.add(match.contest_id)
          return true
        })
        setMatches(deduped.slice(0, 8))
      })
      .catch((cause) => setError(errMsg(cause)))
      .finally(() => setLoading(false))
  }, [gameId])

  return (
    <PageFrame layout="public-home">
      <PageHeader
        title="Bot 对战"
        description="上传 Bot，选择游戏和对手，开一场。"
        actions={
          isLoggedIn ? (
            <>
              <Button asChild variant="outline" size="sm"><Link to="/my-bots"><Bot className="size-4" />我的 Bot</Link></Button>
              <Button asChild size="sm"><Link to="/challenge"><Swords className="size-4" />发起挑战</Link></Button>
            </>
          ) : (
            <>
              <Button asChild variant="outline" size="sm"><Link to="/login"><LogIn className="size-4" />登录</Link></Button>
              <Button asChild size="sm"><Link to="/register"><UserPlus className="size-4" />注册账号</Link></Button>
            </>
          )
        }
      />

      <StickyToolbar label="首页对局筛选">
        <span className="shrink-0 text-xs font-medium text-muted-foreground">最新对局</span>
        <Select value={gameId || 'all'} onValueChange={(value) => setGameId(value === 'all' ? '' : value)}>
          <SelectTrigger className="w-[8.5rem] max-w-full">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">全部游戏</SelectItem>
            {GAMES.map((game) => (
              <SelectItem key={game.id} value={game.id}>{game.label}</SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Button asChild variant="ghost" size="sm" className="ml-auto">
          <Link to="/history">查看全部<ArrowRight className="size-3.5" /></Link>
        </Button>
      </StickyToolbar>

      <DataRegion title="最新对局" description="同一赛事连续开赛时只保留最近一场，方便浏览不同来源的对局。">
        {error ? (
          <ErrorMsg msg={error} className="px-4 py-6" />
        ) : loading ? (
          <Loading text="正在加载最新对局…" />
        ) : matches.length === 0 ? (
          <EmptyState text="当前游戏暂无对局" icon={<Swords className="size-5 opacity-50" />} className="py-8" />
        ) : (
          <>
            <div className="hidden md:block">
              <DataTable className="rounded-none border-0" scrollLabel="最新对局">
                <Table aria-label="最新对局" className="min-w-[42rem]">
                  <TableHeader>
                    <TableRow>
                      <TableHead>时间</TableHead>
                      <TableHead>游戏</TableHead>
                      <TableHead className="w-full min-w-[16rem]">对阵</TableHead>
                      <TableHead>状态</TableHead>
                      <TableHead className="text-right">操作</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {matches.map((match) => {
                      const GameIcon = gameIcon(match.game_id)
                      return (
                        <TableRow key={match.id}>
                          <TableCell className="font-mono text-xs tabular-nums text-muted-foreground">{fmtTime(match.created_at)}</TableCell>
                          <TableCell>
                            <span className="flex min-w-0 items-center gap-1.5">
                              <GameIcon className="size-3.5 shrink-0 text-muted-foreground" />
                              <span>{gameLabel(match.game_id)}</span>
                              <MatchNatureBadge matchType={match.match_type} source={match} />
                            </span>
                          </TableCell>
                          <TableCell className="whitespace-normal">
                            <MatchParticipants source={match} />
                          </TableCell>
                          <TableCell><StatusBadge status={match.status} /></TableCell>
                          <TableCell className="text-right">
                            <Button asChild variant="ghost" size="xs">
                              <Link to={`/match/${encodeURIComponent(match.id)}`}>
                                {match.status === 'pending' || match.status === 'running' ? '观赛' : '详情'}
                              </Link>
                            </Button>
                          </TableCell>
                        </TableRow>
                      )
                    })}
                  </TableBody>
                </Table>
              </DataTable>
            </div>
            <ul className="divide-y divide-border md:hidden">
              {matches.map((match) => {
                const GameIcon = gameIcon(match.game_id)
                return (
                  <li key={match.id} className="min-w-0 px-3 py-2.5">
                    <div className="flex min-w-0 flex-wrap items-center gap-1.5">
                      <GameIcon className="size-3.5 shrink-0 text-muted-foreground" />
                      <span className="text-xs font-medium">{gameLabel(match.game_id)}</span>
                      <MatchNatureBadge matchType={match.match_type} source={match} />
                      <span className="ml-auto"><StatusBadge status={match.status} /></span>
                    </div>
                    <MatchParticipants source={match} className="mt-2" />
                    <div className="mt-2 flex min-w-0 items-center justify-between gap-3 text-xs text-muted-foreground">
                      <time className="min-w-0 font-mono tabular-nums">{fmtTime(match.created_at)}</time>
                      <Link className="shrink-0 whitespace-nowrap font-medium text-primary" to={`/match/${encodeURIComponent(match.id)}`}>
                        {match.status === 'pending' || match.status === 'running' ? '观赛' : '详情'}
                      </Link>
                    </div>
                  </li>
                )
              })}
            </ul>
          </>
        )}
      </DataRegion>

      <LikedTopMatches />
    </PageFrame>
  )
}

function LikedTopMatches() {
  const [matches, setMatches] = useState<LikedMatch[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    apiGet<{ matches: LikedMatch[] }>('/api/matches/liked-top?limit=5')
      .then((data) => setMatches(data.matches || []))
      .catch((cause) => setError(errMsg(cause, '热门对局加载失败')))
      .finally(() => setLoading(false))
  }, [])

  return (
    <DataRegion title="热门对局" description="按点赞数展示近期公开回放。" actions={<Flame className="size-4 text-warning" />}>
      {error ? (
        <ErrorMsg msg={error} className="px-4 py-6" />
      ) : loading ? (
        <Loading text="正在加载热门对局…" className="py-7" />
      ) : matches.length === 0 ? (
        <EmptyState text="暂无热门对局" icon={<Heart className="size-5 opacity-50" />} className="py-7" />
      ) : (
        <ul className="divide-y divide-border">
          {matches.map((match, index) => (
            <li key={match.id} className="grid min-w-0 gap-2 px-3 py-2.5 sm:grid-cols-[2rem_minmax(0,1fr)_auto] sm:items-center">
              <span className="hidden font-mono text-xs tabular-nums text-muted-foreground sm:block">{index + 1}</span>
              <div className="min-w-0">
                <MatchParticipants source={match} />
                <div className="mt-1 flex min-w-0 flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
                  <span>{gameLabel(match.game_id)}</span>
                  <MatchNatureBadge matchType={match.match_type} source={match} />
                  <time className="font-mono tabular-nums">{fmtTime(match.created_at)}</time>
                </div>
              </div>
              <div className="flex min-w-0 shrink-0 items-center gap-2">
                <span className="flex min-w-0 items-center gap-3 text-xs tabular-nums text-muted-foreground">
                  <span className="inline-flex min-w-0 items-center gap-1"><Heart className="size-3 text-destructive" />{match.likes_count}</span>
                  <span className="inline-flex min-w-0 items-center gap-1"><Eye className="size-3" />{match.views_count}</span>
                </span>
                <Button asChild variant="ghost" size="xs">
                  <Link to={`/match/${encodeURIComponent(match.id)}`}>回放</Link>
                </Button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </DataRegion>
  )
}
