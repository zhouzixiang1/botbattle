import { useEffect, useState, type FormEvent } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { Bot as BotIcon, Search as SearchIcon, Swords, User as UserIcon } from 'lucide-react'

import { apiGet, errMsg } from '@/api'
import { DataRegion, PageFrame, PageHeader, StickyToolbar, SummaryStrip } from '@/components/layout'
import { MatchNatureBadge, MatchParticipants } from '@/components/MatchParticipants'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { EntityName, OverflowText } from '@/components/ui/overflow-text'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { EmptyState, ErrorMsg, Loading } from '@/components/ui/status'
import {
  DataTable,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { fmtRating, fmtTime } from '@/lib/format'
import { GAMES, gameIcon, gameLabel } from '@/lib/games'
import type { MatchParticipantSource } from '@/lib/match-participants'
import { SummaryMetric } from '@/pages/public-page-ui'

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

interface MatchRow extends MatchParticipantSource {
  id: string
  game_id: string
  winner: number | null
  bot_a_id: number | null
  bot_b_id: number | null
  match_type?: string
  created_at?: string
}

const TYPE_LABEL: Record<SearchType, string> = {
  users: '用户',
  bots: 'Bot',
  matches: '对局',
}

export default function Search() {
  const [params, setParams] = useSearchParams()
  const navigate = useNavigate()
  const query = params.get('q') || ''
  const requestedType = params.get('type')
  const type: SearchType = requestedType === 'bots' || requestedType === 'matches' ? requestedType : 'users'
  const gameId = params.get('game_id') || ''
  const [input, setInput] = useState(query)
  const [users, setUsers] = useState<UserRow[]>([])
  const [bots, setBots] = useState<BotRow[]>([])
  const [matches, setMatches] = useState<MatchRow[]>([])
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    setInput(query)
  }, [query])

  useEffect(() => {
    let cancelled = false
    if (!query.trim()) {
      setUsers([])
      setBots([])
      setMatches([])
      setError('')
      setLoading(false)
      return
    }
    setLoading(true)
    setError('')
    const gameParam = gameId ? `&game_id=${encodeURIComponent(gameId)}` : ''
    apiGet<Record<string, unknown[]>>(
      `/api/search?q=${encodeURIComponent(query)}&type=${type}&limit=30${gameParam}`,
    )
      .then((data) => {
        if (cancelled) return
        setUsers(type === 'users' ? (data.users as UserRow[]) : [])
        setBots(type === 'bots' ? (data.bots as BotRow[]) : [])
        setMatches(type === 'matches' ? (data.matches as MatchRow[]) : [])
      })
      .catch((cause) => {
        if (!cancelled) setError(errMsg(cause))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [gameId, query, type])

  const submit = (event: FormEvent) => {
    event.preventDefault()
    const next = new URLSearchParams(params)
    const value = input.trim()
    if (value) next.set('q', value)
    else next.delete('q')
    next.set('type', type)
    setParams(next)
  }

  const switchType = (nextType: SearchType) => {
    const next = new URLSearchParams(params)
    next.set('type', nextType)
    if (query) next.set('q', query)
    if (nextType === 'users') next.delete('game_id')
    navigate(`/search?${next.toString()}`)
  }

  const resultCount = type === 'users' ? users.length : type === 'bots' ? bots.length : matches.length

  return (
    <PageFrame layout="public-search">
      <PageHeader
        title="全站搜索"
        description="按用户、Bot 或对局分别检索；Bot 与对局结果可进一步限定游戏。"
      />

      <StickyToolbar label="搜索与结果筛选" className="items-stretch">
        <form onSubmit={submit} className="flex min-w-0 flex-[1_1_22rem] gap-2">
          <div className="relative min-w-0 flex-1">
            <SearchIcon aria-hidden="true" className="absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              value={input}
              onChange={(event) => setInput(event.target.value)}
              placeholder="输入用户名、Bot 名称或对局标识"
              aria-label="搜索关键词"
              className="w-full pl-9"
              autoFocus
            />
          </div>
          <Button type="submit">搜索</Button>
        </form>
        <Tabs value={type} onValueChange={(value) => switchType(value as SearchType)} className="min-w-0 flex-[1_1_auto]">
          <TabsList>
            <TabsTrigger value="users"><UserIcon className="size-3.5" />用户</TabsTrigger>
            <TabsTrigger value="bots"><BotIcon className="size-3.5" />Bot</TabsTrigger>
            <TabsTrigger value="matches"><Swords className="size-3.5" />对局</TabsTrigger>
          </TabsList>
        </Tabs>
        {type !== 'users' && (
          <Select
            value={gameId || 'all'}
            onValueChange={(value) => {
              const next = new URLSearchParams(params)
              if (value === 'all') next.delete('game_id')
              else next.set('game_id', value)
              navigate(`/search?${next.toString()}`)
            }}
          >
            <SelectTrigger className="w-[8.5rem] max-w-full"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">全部游戏</SelectItem>
              {GAMES.map((game) => <SelectItem key={game.id} value={game.id}>{game.label}</SelectItem>)}
            </SelectContent>
          </Select>
        )}
      </StickyToolbar>

      <SummaryStrip columns={3}>
        <SummaryMetric label="关键词" value={query || '等待输入'} detail={query ? '当前 URL 可分享此搜索' : '输入后开始查询'} mono={false} icon={<SearchIcon className="size-4" />} />
        <SummaryMetric label="结果类型" value={TYPE_LABEL[type]} detail={type === 'users' ? '不应用游戏筛选' : gameId ? gameLabel(gameId) : '全部游戏'} mono={false} />
        <SummaryMetric label="本次结果" value={resultCount} detail="最多返回 30 条" />
      </SummaryStrip>

      <DataRegion title={`${TYPE_LABEL[type]}结果`} description={query ? `关键词：${query}` : '输入关键词后显示结果'}>
        {error ? (
          <ErrorMsg msg={error} className="px-4 py-6" />
        ) : !query.trim() ? (
          <EmptyState text="输入关键词开始搜索" icon={<SearchIcon className="size-5 opacity-50" />} className="py-8" />
        ) : loading ? (
          <Loading text="正在搜索…" />
        ) : type === 'users' ? (
          <UserResults users={users} />
        ) : type === 'bots' ? (
          <BotResults bots={bots} />
        ) : (
          <MatchResults matches={matches} />
        )}
      </DataRegion>
    </PageFrame>
  )
}

function UserResults({ users }: { users: UserRow[] }) {
  if (users.length === 0) {
    return <EmptyState text="无匹配用户" icon={<UserIcon className="size-5 opacity-50" />} className="py-8" />
  }
  return (
    <ul className="divide-y divide-border">
      {users.map((user, index) => (
        <li key={user.id}>
          <Link to={`/user/${encodeURIComponent(user.username)}`} className="grid min-w-0 gap-2 px-3 py-2.5 hover:bg-muted/40 sm:grid-cols-[2rem_2.25rem_minmax(0,1fr)_auto] sm:items-center">
            <span className="hidden font-mono text-xs tabular-nums text-muted-foreground sm:block">{index + 1}</span>
            <span aria-hidden="true" className="flex min-w-0 size-9 shrink-0 items-center justify-center rounded-full bg-muted text-sm font-bold text-muted-foreground">
              {(user.display_name || user.username).charAt(0).toUpperCase()}
            </span>
            <span className="min-w-0">
              <EntityName lines={2} tooltip={false} tooltipFocusable={false}>{user.display_name || user.username}</EntityName>
              <OverflowText tooltip={false} className="text-xs text-muted-foreground">@{user.username}</OverflowText>
            </span>
            <span className="hidden shrink-0 text-xs font-medium text-primary sm:block">查看主页</span>
          </Link>
        </li>
      ))}
    </ul>
  )
}

function BotResults({ bots }: { bots: BotRow[] }) {
  if (bots.length === 0) {
    return <EmptyState text="无匹配 Bot" icon={<BotIcon className="size-5 opacity-50" />} className="py-8" />
  }
  return (
    <ul className="grid min-w-0 gap-px bg-border sm:grid-cols-2 xl:grid-cols-3">
      {bots.map((bot, index) => {
        const GameIcon = gameIcon(bot.game_id)
        return (
          <li key={bot.id} className="min-w-0 bg-card">
            <Link to={`/bot/${bot.id}`} className="flex min-w-0 gap-3 px-3 py-3 hover:bg-muted/40">
              <span className="font-mono text-xs tabular-nums text-muted-foreground">{index + 1}</span>
              <span className="min-w-0 flex-1">
                <EntityName lines={2} tooltip={false} tooltipFocusable={false} className="hover:text-primary">{bot.display_name || bot.name}</EntityName>
                <OverflowText tooltip={false} className="mt-0.5 text-xs text-muted-foreground">
                  @{bot.name}{bot.owner_name ? ` · ${bot.owner_display || bot.owner_name}` : ''}{bot.rating != null ? ` · ${fmtRating(bot.rating)}` : ''}
                </OverflowText>
              </span>
              <Badge variant="secondary" className="self-start"><GameIcon className="size-3" />{gameLabel(bot.game_id)}</Badge>
            </Link>
          </li>
        )
      })}
    </ul>
  )
}

function MatchResults({ matches }: { matches: MatchRow[] }) {
  if (matches.length === 0) {
    return <EmptyState text="无匹配对局" icon={<Swords className="size-5 opacity-50" />} className="py-8" />
  }

  return (
    <>
      <div className="hidden md:block">
        <DataTable className="rounded-none border-0" scrollLabel="搜索到的对局">
          <Table aria-label="搜索到的对局" className="min-w-[38rem]">
            <TableHeader>
              <TableRow>
                <TableHead>序号</TableHead>
                <TableHead>时间</TableHead>
                <TableHead className="w-full min-w-[16rem]">对阵</TableHead>
                <TableHead>游戏 / 性质</TableHead>
                <TableHead className="text-right">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {matches.map((match, index) => (
                <TableRow key={match.id}>
                  <TableCell className="font-mono text-xs tabular-nums text-muted-foreground">{index + 1}</TableCell>
                  <TableCell className="font-mono text-xs tabular-nums text-muted-foreground">{fmtTime(match.created_at)}</TableCell>
                  <TableCell className="whitespace-normal">
                    <MatchParticipants source={match} />
                  </TableCell>
                  <TableCell>
                    <div className="flex min-w-0 flex-col items-start gap-1">
                      <span>{gameLabel(match.game_id)}</span>
                      <MatchNatureBadge matchType={match.match_type} source={match} />
                    </div>
                  </TableCell>
                  <TableCell className="text-right"><Button asChild variant="ghost" size="xs"><Link to={`/match/${encodeURIComponent(match.id)}`}>回放</Link></Button></TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </DataTable>
      </div>
      <ul className="divide-y divide-border md:hidden">
        {matches.map((match, index) => (
          <li key={match.id} className="min-w-0 px-3 py-2.5">
            <div className="flex min-w-0 flex-wrap items-center gap-2 text-xs text-muted-foreground">
              <span className="font-mono">{index + 1} · {fmtTime(match.created_at)}</span>
              <Badge variant="secondary" className="ml-auto">{gameLabel(match.game_id)}</Badge>
              <MatchNatureBadge matchType={match.match_type} source={match} />
            </div>
            <MatchParticipants source={match} className="mt-2" />
            <Button asChild variant="link" size="xs" className="mt-1 px-0"><Link to={`/match/${encodeURIComponent(match.id)}`}>打开回放</Link></Button>
          </li>
        ))}
      </ul>
    </>
  )
}
