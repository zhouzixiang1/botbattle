import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { TrendingUp, TrendingDown, Minus, Trophy } from 'lucide-react'
import PageStub from '@/components/PageStub'
import { Card } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { EmptyState, ErrorMsg, Loading } from '@/components/ui/status'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { TierBadge } from '@/components/tier-badge'
import Pagination from '@/components/Pagination'
import { apiGet, errMsg } from '@/api'
import { GAMES, gameLabel, gameIcon, type GameId } from '@/lib/games'
import { fmtRating } from '@/lib/format'
import { trendDelta } from '@/lib/tiers'

interface Row {
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
  net_chips?: number
  format?: string
  os?: string
  arch?: string
  game_id?: string
  rating_delta?: number | null
  tier_name?: string
  tier_key?: string
  is_placement?: boolean
  placement_required?: number
  placement_remaining?: number
}

export default function Leaderboard() {
  const [rows, setRows] = useState<Row[]>([])
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [gameId, setGameId] = useState<GameId | ''>('holdem')
  // 分页
  const [page, setPage] = useState(1)
  const [total, setTotal] = useState(0)
  const perPage = 50

  useEffect(() => {
    setLoading(true)
    const params = new URLSearchParams()
    if (gameId) params.set('game_id', gameId)
    params.set('page', String(page))
    params.set('per_page', String(perPage))
    apiGet<{ leaderboard: Row[]; total?: number }>(`/api/leaderboard?${params.toString()}`)
      .then((d) => {
        setRows(d.leaderboard || [])
        if (d.total !== undefined) setTotal(d.total)
      })
      .catch((e) => setError(errMsg(e)))
      .finally(() => setLoading(false))
  }, [gameId, page])

  // 切换游戏 tab → 回到第 1 页
  const onGameChange = (v: string) => {
    setGameId(v === 'all' ? '' : (v as GameId))
    setPage(1)
  }

  return (
    <PageStub
      title="排行榜"
      subtitle="Glicko-2 评分，按游戏过滤"
      actions={
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          游戏
          <Select value={gameId || 'all'} onValueChange={onGameChange}>
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
      {error && <ErrorMsg msg={error} className="mb-3" />}

      <Card className="overflow-hidden">
        <div className="overflow-x-auto">
        <Table className="min-w-[28rem]">
          <TableHeader>
            <TableRow>
              <TableHead className="w-10">#</TableHead>
              <TableHead className="min-w-[7rem]">Bot</TableHead>
              <TableHead className="hidden md:table-cell">游戏</TableHead>
              <TableHead className="hidden lg:table-cell">所有者</TableHead>
              <TableHead className="whitespace-nowrap">段位</TableHead>
              <TableHead className="whitespace-nowrap">Rating</TableHead>
              <TableHead className="hidden sm:table-cell">战绩</TableHead>
              <TableHead className="hidden xl:table-cell">平台</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {loading ? (
              <TableRow>
                <TableCell colSpan={8}>
                  <Loading />
                </TableCell>
              </TableRow>
            ) : rows.length === 0 ? (
              <TableRow>
                <TableCell colSpan={8}>
                  <EmptyState text="暂无数据" icon={<Trophy className="size-7 opacity-40" />} />
                </TableCell>
              </TableRow>
            ) : (
              rows.map((r, i) => {
                const td = trendDelta(r.rating_delta)
                const GameIcon = gameIcon(r.game_id)
                const played = r.matches_played ?? ((r.wins ?? 0) + (r.losses ?? 0) + (r.draws ?? 0))
                const placementRequired = r.placement_required ?? 0
                return (
                  <TableRow key={r.bot_id}>
                    <TableCell className="font-mono text-xs text-muted-foreground">
                      {r.is_placement ? '—' : (page - 1) * perPage + i + 1}
                    </TableCell>
                    <TableCell className="max-w-[10rem]">
                      <Link
                        to={`/bot/${r.bot_id}`}
                        className="block truncate font-medium text-foreground hover:text-primary"
                        title={r.bot_display || r.bot_name || `#${r.bot_id}`}
                      >
                        {r.bot_display || r.bot_name || `#${r.bot_id}`}
                      </Link>
                    </TableCell>
                    <TableCell className="hidden md:table-cell">
                      <span className="flex items-center gap-1.5 text-sm text-muted-foreground">
                        <GameIcon className="size-3.5" />
                        {gameLabel(r.game_id)}
                      </span>
                    </TableCell>
                    <TableCell className="hidden lg:table-cell">
                      {r.owner_name ? (
                        <Link
                          to={`/user/${encodeURIComponent(r.owner_name)}`}
                          className="text-primary hover:underline"
                        >
                          {r.owner_name}
                        </Link>
                      ) : (
                        <span className="text-muted-foreground">—</span>
                      )}
                    </TableCell>
                    <TableCell>
                      {r.is_placement ? (
                        <Badge variant="secondary" className="whitespace-nowrap text-muted-foreground">
                          定级中 {played}/{placementRequired}
                        </Badge>
                      ) : (
                        <TierBadge rating={r.rating} label={r.tier_name} gameId={r.game_id} tierKey={r.tier_key} />
                      )}
                    </TableCell>
                    <TableCell className="whitespace-nowrap">
                      <span className="font-mono font-semibold text-primary tabular-nums">
                        {fmtRating(r.rating)}
                      </span>
                      {td && (
                        <span
                          className={`ml-1.5 inline-flex items-center text-xs font-medium ${
                            td.up ? 'text-success' : 'text-destructive'
                          }`}
                        >
                          {td.up ? (
                            <TrendingUp className="size-3" />
                          ) : (
                            <TrendingDown className="size-3" />
                          )}
                          {td.abs.toFixed(0)}
                        </span>
                      )}
                      {r.rating_delta === 0 && (
                        <Minus className="ml-1.5 inline size-3 text-muted-foreground" />
                      )}
                    </TableCell>
                    <TableCell className="hidden font-mono text-xs text-muted-foreground sm:table-cell">
                      <span className="text-success">{r.wins ?? 0}</span>W{' '}
                      <span className="text-destructive">{r.losses ?? 0}</span>L{' '}
                      {r.draws ?? 0}D
                    </TableCell>
                    <TableCell className="hidden font-mono text-xs text-muted-foreground xl:table-cell">
                      {r.format}/{r.os}-{r.arch}
                    </TableCell>
                  </TableRow>
                )
              })
            )}
          </TableBody>
        </Table>
        </div>
        <Pagination page={page} perPage={perPage} total={total} onPageChange={setPage} />
      </Card>
    </PageStub>
  )
}
