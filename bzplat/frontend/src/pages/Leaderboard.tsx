import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { TrendingUp, TrendingDown, Minus, Trophy } from 'lucide-react'
import PageStub from '@/components/PageStub'
import { Card } from '@/components/ui/card'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { EmptyState, ErrorMsg, Loading } from '@/components/ui/status'
import { TierBadge } from '@/components/tier-badge'
import { apiGet, errMsg } from '@/api'
import { GAMES, gameLabel, gameIcon, type GameId } from '@/lib/games'
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
}

export default function Leaderboard() {
  const [rows, setRows] = useState<Row[]>([])
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [gameId, setGameId] = useState<GameId | ''>('holdem')

  useEffect(() => {
    setLoading(true)
    const q = gameId ? `?game_id=${encodeURIComponent(gameId)}` : ''
    apiGet<{ leaderboard: Row[] }>(`/api/leaderboard${q}`)
      .then((d) => setRows(d.leaderboard || []))
      .catch((e) => setError(errMsg(e)))
      .finally(() => setLoading(false))
  }, [gameId])

  return (
    <PageStub
      title="排行榜"
      subtitle="Glicko-2 评分，按游戏过滤"
      actions={
        <label className="flex items-center gap-2 text-sm text-muted-foreground">
          游戏
          <select
            value={gameId}
            onChange={(e) => setGameId(e.target.value as GameId | '')}
            className="h-9 rounded-lg border border-input bg-background px-3 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-ring"
          >
            <option value="">全部</option>
            {GAMES.map((g) => (
              <option key={g.id} value={g.id}>
                {g.label}
              </option>
            ))}
          </select>
        </label>
      }
    >
      {error && <ErrorMsg msg={error} className="mb-3" />}

      <Card>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="w-10">#</TableHead>
              <TableHead>Bot</TableHead>
              <TableHead className="hidden md:table-cell">游戏</TableHead>
              <TableHead className="hidden lg:table-cell">所有者</TableHead>
              <TableHead>段位</TableHead>
              <TableHead>Rating</TableHead>
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
                return (
                  <TableRow key={r.bot_id}>
                    <TableCell className="font-mono text-xs text-muted-foreground">
                      {i + 1}
                    </TableCell>
                    <TableCell>
                      <Link
                        to={`/bot/${r.bot_id}`}
                        className="font-medium text-foreground hover:text-primary"
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
                      <TierBadge rating={r.rating} label={r.tier_name} />
                    </TableCell>
                    <TableCell>
                      <span className="font-mono font-semibold text-primary">
                        {Number(r.rating).toFixed(1)}
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
      </Card>
    </PageStub>
  )
}
