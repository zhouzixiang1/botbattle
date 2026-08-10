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
  Trophy,
  Upload,
  UserPlus,
} from 'lucide-react'

import { apiGet, errMsg } from '@/api'
import { DataRegion, PageFrame, PageHeader, StickyToolbar, SummaryStrip } from '@/components/layout'
import { useAuth } from '@/components/useAuth'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { EntityName } from '@/components/ui/overflow-text'
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
import { GAMES, gameIcon, gameLabel, matchTypeBadge } from '@/lib/games'
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
  game_id?: string
  match_type?: string
  contest_id?: string | number | null
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

function displayBot(display?: string, name?: string): string {
  return display || name || '已删除 Bot'
}

function BotLink({ id, label }: { id: number | null; label: string }) {
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

const QUICK_LINKS = [
  { to: '/my-bots', label: '上传 Bot', detail: '提交 ELF 并选择游戏', icon: Upload },
  { to: '/challenge', label: '发起挑战', detail: '指定对手、版本或亲自上场', icon: Swords },
  { to: '/leaderboard', label: '查看排行', detail: '按游戏查看 Glicko-2 天梯', icon: Trophy },
]

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

  const activeMatches = matches.filter((match) => match.status === 'pending' || match.status === 'running').length
  const completedMatches = matches.filter((match) => match.status === 'completed').length

  return (
    <PageFrame layout="public-home">
      <PageHeader
        eyebrow="Botbattle"
        title="Bot 对战中心"
        description="上传 Linux x86_64 ELF Bot，在隔离沙箱中完成德州扑克、五子棋与点格棋对局。"
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

      <SummaryStrip columns={3}>
        <SummaryMetric label="支持游戏" value={GAMES.length} detail={GAMES.map((game) => game.label).join(' · ')} icon={<Bot className="size-4" />} />
        <SummaryMetric label="最新活跃" value={activeMatches} detail={`当前展示 ${matches.length} 场`} icon={<Swords className="size-4" />} />
        <SummaryMetric label="本批完成" value={completedMatches} detail={gameId ? gameLabel(gameId) : '全部游戏'} icon={<Trophy className="size-4" />} />
      </SummaryStrip>

      <div className="grid min-w-0 gap-2 sm:grid-cols-3" aria-label="快速开始">
        {QUICK_LINKS.map((item, index) => (
          <Link key={item.to} to={item.to} className="group min-w-0">
            <Card density="compact" className="h-full transition-colors hover:border-primary/40 hover:bg-accent/30">
              <CardContent className="flex min-w-0 items-center gap-3">
                <span className="flex min-w-0 size-7 shrink-0 items-center justify-center rounded-lg bg-primary/10 font-mono text-xs font-semibold text-primary">
                  {index + 1}
                </span>
                <item.icon aria-hidden="true" className="size-4 shrink-0 text-muted-foreground group-hover:text-primary" />
                <span className="min-w-0 flex-1">
                  <EntityName tooltip={false} tooltipFocusable={false} className="text-sm group-hover:text-primary">{item.label}</EntityName>
                  <span className="block text-xs text-muted-foreground">{item.detail}</span>
                </span>
                <ArrowRight aria-hidden="true" className="size-4 shrink-0 text-muted-foreground" />
              </CardContent>
            </Card>
          </Link>
        ))}
      </div>

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

      <DataRegion title="最新对局" description="同一赛事批次仅展示最近一场，避免首页动态被单一赛事占满。">
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
                      const typeBadge = matchTypeBadge(match.match_type)
                      const GameIcon = gameIcon(match.game_id)
                      return (
                        <TableRow key={match.id}>
                          <TableCell className="font-mono text-xs tabular-nums text-muted-foreground">{fmtTime(match.created_at)}</TableCell>
                          <TableCell>
                            <span className="flex min-w-0 items-center gap-1.5">
                              <GameIcon className="size-3.5 shrink-0 text-muted-foreground" />
                              <span>{gameLabel(match.game_id)}</span>
                              {typeBadge && <Badge variant="outline" className={typeBadge.cls}>{typeBadge.label}</Badge>}
                            </span>
                          </TableCell>
                          <TableCell className="whitespace-normal">
                            <div className="grid min-w-0 grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)] items-start gap-2">
                              <BotLink id={match.bot_a_id} label={displayBot(match.bot_a_display, match.bot_a_name)} />
                              <span className="text-xs text-muted-foreground">vs</span>
                              <BotLink id={match.bot_b_id} label={displayBot(match.bot_b_display, match.bot_b_name)} />
                            </div>
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
                const typeBadge = matchTypeBadge(match.match_type)
                const GameIcon = gameIcon(match.game_id)
                return (
                  <li key={match.id} className="min-w-0 px-3 py-2.5">
                    <div className="flex min-w-0 flex-wrap items-center gap-1.5">
                      <GameIcon className="size-3.5 shrink-0 text-muted-foreground" />
                      <span className="text-xs font-medium">{gameLabel(match.game_id)}</span>
                      {typeBadge && <Badge variant="outline" className={typeBadge.cls}>{typeBadge.label}</Badge>}
                      <span className="ml-auto"><StatusBadge status={match.status} /></span>
                    </div>
                    <div className="mt-2 grid min-w-0 grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)] items-start gap-2">
                      <BotLink id={match.bot_a_id} label={displayBot(match.bot_a_display, match.bot_a_name)} />
                      <span className="text-xs text-muted-foreground">vs</span>
                      <BotLink id={match.bot_b_id} label={displayBot(match.bot_b_display, match.bot_b_name)} />
                    </div>
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
              <Link to={`/match/${encodeURIComponent(match.id)}`} className="min-w-0 hover:text-primary">
                <EntityName lines={2} tooltip={false} tooltipFocusable={false} className="text-sm hover:text-primary">
                  {displayBot(match.bot_a_display, match.bot_a_name)} vs {displayBot(match.bot_b_display, match.bot_b_name)}
                </EntityName>
                <span className="mt-0.5 block text-xs text-muted-foreground">{gameLabel(match.game_id)} · {fmtTime(match.created_at)}</span>
              </Link>
              <span className="flex min-w-0 shrink-0 items-center gap-3 text-xs tabular-nums text-muted-foreground">
                <span className="inline-flex min-w-0 items-center gap-1"><Heart className="size-3 text-destructive" />{match.likes_count}</span>
                <span className="inline-flex min-w-0 items-center gap-1"><Eye className="size-3" />{match.views_count}</span>
              </span>
            </li>
          ))}
        </ul>
      )}
    </DataRegion>
  )
}
