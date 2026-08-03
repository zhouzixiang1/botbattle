import { useEffect, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { Search as SearchIcon, User as UserIcon, Bot as BotIcon, Swords } from 'lucide-react'
import PageStub from '@/components/PageStub'
import { Card, CardContent } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
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
import { apiGet, errMsg } from '@/api'
import { gameLabel, gameIcon, GAMES } from '@/lib/games'

type SearchType = 'users' | 'bots' | 'matches'

interface UserRow {
  id: number
  username: string
  display_name: string
}
interface BotRow {
  id: number
  name: string
  display_name: string
  game_id: string
  owner_name?: string
  owner_display?: string
  rating?: number
}
interface MatchRow {
  id: string
  game_id: string
  winner: number | null
  bot_a_id: number
  bot_b_id: number
  bot_a_name: string
  bot_b_name: string
  bot_a_display?: string
  bot_b_display?: string
  created_at?: string
}

export default function Search() {
  const [params, setParams] = useSearchParams()
  const navigate = useNavigate()
  const q = params.get('q') || ''
  const type = (params.get('type') as SearchType) || 'users'
  const gameId = params.get('game_id') || ''
  const [input, setInput] = useState(q)
  const [users, setUsers] = useState<UserRow[]>([])
  const [bots, setBots] = useState<BotRow[]>([])
  const [matches, setMatches] = useState<MatchRow[]>([])
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    setInput(q)
  }, [q])

  useEffect(() => {
    if (!q.trim()) return
    setLoading(true)
    const t = type
    const gidParam = gameId ? `&game_id=${encodeURIComponent(gameId)}` : ''
    apiGet<Record<string, unknown[]>>(
      `/api/search?q=${encodeURIComponent(q)}&type=${t}&limit=30${gidParam}`,
    )
      .then((d) => {
        setUsers(t === 'users' ? (d.users as UserRow[]) : [])
        setBots(t === 'bots' ? (d.bots as BotRow[]) : [])
        setMatches(t === 'matches' ? (d.matches as MatchRow[]) : [])
      })
      .catch((e) => setError(errMsg(e)))
      .finally(() => setLoading(false))
  }, [q, type])

  function submit(e: React.FormEvent) {
    e.preventDefault()
    const next = new URLSearchParams(params)
    next.set('q', input.trim())
    next.set('type', type)
    setParams(next)
  }

  function switchType(t: SearchType) {
    const next = new URLSearchParams(params)
    next.set('type', t)
    if (q) next.set('q', q)
    navigate(`/search?${next.toString()}`)
  }

  return (
    <PageStub title="搜索" subtitle="查找用户、Bot 或对局">
      {/* 搜索框 */}
      <form onSubmit={submit} className="mb-4 flex gap-2">
        <div className="relative min-w-0 flex-1">
          <SearchIcon className="absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="搜索用户 / Bot / 对局…"
            className="pl-9"
            autoFocus
          />
        </div>
        <Button type="submit">搜索</Button>
      </form>

      {/* 类型 tab + 游戏筛选 */}
      <div className="mb-4 flex items-center justify-between gap-2">
        <Tabs value={type} onValueChange={(v) => switchType(v as SearchType)}>
          <TabsList>
            <TabsTrigger value="users" className="gap-1.5"><UserIcon className="size-3.5" />用户</TabsTrigger>
            <TabsTrigger value="bots" className="gap-1.5"><BotIcon className="size-3.5" />Bot</TabsTrigger>
            <TabsTrigger value="matches" className="gap-1.5"><Swords className="size-3.5" />对局</TabsTrigger>
          </TabsList>
        </Tabs>
        {type !== 'users' && (
          <Select
            value={gameId || 'all'}
            onValueChange={(v) => {
              const next = new URLSearchParams(params)
              if (v === 'all') next.delete('game_id')
              else next.set('game_id', v)
              navigate(`/search?${next.toString()}`)
            }}
          >
            <SelectTrigger className="h-9 w-[8.5rem]">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">全部游戏</SelectItem>
              {GAMES.map((g) => (
                <SelectItem key={g.id} value={g.id}>{g.label}</SelectItem>
              ))}
            </SelectContent>
          </Select>
        )}
      </div>

      {error && <ErrorMsg msg={error} className="mb-3" />}

      {!q.trim() && <EmptyState text="输入关键词开始搜索" icon={<SearchIcon className="size-7 opacity-40" />} />}
      {loading && <Loading text="搜索中…" />}

      {/* 用户结果 */}
      {type === 'users' && q.trim() && !loading && (
        <div className="space-y-2">
          {users.length === 0 ? (
            <EmptyState text="无匹配用户" icon={<UserIcon className="size-7 opacity-40" />} />
          ) : (
            users.map((u) => (
              <Link key={u.id} to={`/user/${encodeURIComponent(u.username)}`}>
                <Card className="transition-colors hover:border-primary/40">
                  <CardContent className="flex items-center gap-3 py-3">
                    <div className="flex size-9 items-center justify-center rounded-full bg-muted text-sm font-bold text-muted-foreground">
                      {(u.display_name || u.username).charAt(0).toUpperCase()}
                    </div>
                    <div className="min-w-0">
                      <span className="font-medium text-foreground">{u.display_name || u.username}</span>
                      <span className="ml-2 text-xs text-muted-foreground">@{u.username}</span>
                    </div>
                  </CardContent>
                </Card>
              </Link>
            ))
          )}
        </div>
      )}

      {/* Bot 结果 */}
      {type === 'bots' && q.trim() && !loading && (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {bots.length === 0 ? (
            <div className="col-span-full">
              <Card><EmptyState text="无匹配 Bot" icon={<BotIcon className="size-7 opacity-40" />} /></Card>
            </div>
          ) : (
            bots.map((b) => {
              const GameIcon = gameIcon(b.game_id)
              return (
                <Link key={b.id} to={`/bot/${b.id}`} className="group">
                  <Card className="h-full transition-colors hover:border-primary/40 hover:shadow-lift">
                    <CardContent className="gap-1 py-4">
                      <div className="flex items-center justify-between gap-2">
                        <span className="font-medium text-foreground group-hover:text-primary">
                          {b.display_name || b.name}
                        </span>
                        <Badge variant="secondary" className="gap-1 text-[10px]">
                          <GameIcon className="size-3" />
                          {gameLabel(b.game_id)}
                        </Badge>
                      </div>
                      <p className="text-xs text-muted-foreground">
                        @{b.name}
                        {b.owner_name ? ` · ${b.owner_display || b.owner_name}` : ''}
                        {b.rating != null && ` · ${Number(b.rating).toFixed(0)}`}
                      </p>
                    </CardContent>
                  </Card>
                </Link>
              )
            })
          )}
        </div>
      )}

      {/* 对局结果 */}
      {type === 'matches' && q.trim() && !loading && (
        <Card>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>时间</TableHead>
                <TableHead>对阵</TableHead>
                <TableHead className="hidden sm:table-cell">游戏</TableHead>
                <TableHead className="text-right">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {matches.length === 0 ? (
                <TableRow><TableCell colSpan={4}><EmptyState text="无匹配对局" icon={<Swords className="size-7 opacity-40" />} /></TableCell></TableRow>
              ) : (
                matches.map((m) => (
                  <TableRow key={m.id}>
                    <TableCell className="whitespace-nowrap font-mono text-xs text-muted-foreground">
                      {m.created_at?.slice(0, 16).replace('T', ' ') || '—'}
                    </TableCell>
                    <TableCell>
                      <div className="flex items-center gap-1.5 text-sm">
                        <Link to={`/bot/${m.bot_a_id}`} className="font-medium text-foreground hover:text-primary">
                          {m.bot_a_display || m.bot_a_name}
                        </Link>
                        <span className="text-muted-foreground">vs</span>
                        <Link to={`/bot/${m.bot_b_id}`} className="font-medium text-foreground hover:text-primary">
                          {m.bot_b_display || m.bot_b_name}
                        </Link>
                      </div>
                    </TableCell>
                    <TableCell className="hidden text-sm text-muted-foreground sm:table-cell">{gameLabel(m.game_id)}</TableCell>
                    <TableCell className="text-right">
                      <Link to={`/match/${encodeURIComponent(m.id)}`} className="text-xs font-medium text-primary hover:underline">回放</Link>
                    </TableCell>
                  </TableRow>
                ))
              )}
            </TableBody>
          </Table>
        </Card>
      )}
    </PageStub>
  )
}
