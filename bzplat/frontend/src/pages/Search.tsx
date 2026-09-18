import { useEffect, useState, type FormEvent, type ReactNode } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { Bot as BotIcon, Search as SearchIcon, Swords, User as UserIcon } from 'lucide-react'

import { apiGet, errMsg } from '@/api'
import { DataRegion, PageFrame, PageHeader, StickyToolbar } from '@/components/layout'
import { MatchOutcome } from '@/components/MatchOutcome'
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
import type { MatchOutcomeSource } from '@/lib/match-outcome'
import { outcomeSeatLabels } from '@/lib/match-seats'
import { cn } from '@/lib/utils'

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

interface MatchRow extends MatchParticipantSource, MatchOutcomeSource {
  id: string
  game_id: string
  winner: number | null
  bot_a_id: number | null
  bot_b_id: number | null
  match_type?: string
  created_at?: string
}

/** 单类结果：条目与该类独立错误互不拖累。 */
interface SectionState<T> {
  items: T[]
  error: string
}

interface ResultsState {
  users: SectionState<UserRow>
  bots: SectionState<BotRow>
  matches: SectionState<MatchRow>
}

const EMPTY_RESULTS: ResultsState = {
  users: { items: [], error: '' },
  bots: { items: [], error: '' },
  matches: { items: [], error: '' },
}

const TYPE_LABEL: Record<SearchType, string> = {
  users: '用户',
  bots: 'Bot',
  matches: '对局',
}

const SECTION_ID: Record<SearchType, string> = {
  users: 'search-section-users',
  bots: 'search-section-bots',
  matches: 'search-section-matches',
}

/** 拉取一类检索结果；失败只标记该类错误，不拖垮其余两类。 */
async function fetchSection<T>(type: SearchType, url: string): Promise<SectionState<T>> {
  try {
    const data = await apiGet<Record<string, unknown[]>>(url)
    const items = data[type]
    return { items: Array.isArray(items) ? (items as T[]) : [], error: '' }
  } catch (cause) {
    return { items: [], error: errMsg(cause) }
  }
}

export default function Search() {
  const [params, setParams] = useSearchParams()
  const query = params.get('q') || ''
  const requestedType = params.get('type')
  // type 参数仅作锚点：接受 users|bots|matches，其他取值视为无锚点。
  const anchorType: SearchType | null =
    requestedType === 'users' || requestedType === 'bots' || requestedType === 'matches'
      ? requestedType
      : null
  const gameId = params.get('game_id') || ''
  const [input, setInput] = useState(query)
  const [results, setResults] = useState<ResultsState>(EMPTY_RESULTS)
  const [loading, setLoading] = useState(false)
  const [anchored, setAnchored] = useState<SearchType | null>(null)

  useEffect(() => {
    setInput(query)
  }, [query])

  // 一次并行拉三类，每类独立容错；cancelled 代际守卫确保旧响应不覆盖新关键词。
  useEffect(() => {
    if (!query.trim()) {
      setResults(EMPTY_RESULTS)
      setLoading(false)
      return
    }
    let cancelled = false
    setLoading(true)
    const encoded = encodeURIComponent(query)
    const gameParam = gameId ? `&game_id=${encodeURIComponent(gameId)}` : ''
    Promise.all([
      fetchSection<UserRow>('users', `/api/search?q=${encoded}&type=users&limit=30`),
      fetchSection<BotRow>('bots', `/api/search?q=${encoded}&type=bots&limit=30${gameParam}`),
      fetchSection<MatchRow>('matches', `/api/search?q=${encoded}&type=matches&limit=30${gameParam}`),
    ])
      .then(([users, bots, matches]) => {
        if (cancelled) return
        setResults({ users, bots, matches })
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [gameId, query])

  // 锚点定位：等待分区渲染完成后平滑滚动并高亮对应标题。
  useEffect(() => {
    if (!anchorType) {
      setAnchored(null)
      return
    }
    if (loading) return
    setAnchored(anchorType)
    document.getElementById(SECTION_ID[anchorType])?.scrollIntoView?.({ behavior: 'smooth' })
  }, [anchorType, loading])

  const submit = (event: FormEvent) => {
    event.preventDefault()
    const next = new URLSearchParams(params)
    const value = input.trim()
    if (value) next.set('q', value)
    else next.delete('q')
    setParams(next)
  }

  const jumpTo = (nextType: SearchType) => {
    const next = new URLSearchParams(params)
    next.set('type', nextType)
    setParams(next)
  }

  const hasQuery = !!query.trim()

  return (
    <PageFrame layout="public-search" width="full">
      <PageHeader
        title="全站搜索"
        description="一次搜索同时返回用户、Bot 与对局三类结果；Bot 与对局结果可进一步限定游戏。"
      />

      <StickyToolbar label="搜索与结果筛选" className="items-stretch">
        <form onSubmit={submit} className="flex min-w-0 flex-[1_1_22rem] gap-2">
          <div className="relative min-w-0 flex-1">
            <SearchIcon aria-hidden="true" className="absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              value={input}
              onChange={(event) => setInput(event.target.value)}
              placeholder="搜索用户、Bot 或对局"
              aria-label="搜索关键词"
              className="w-full pl-9"
              autoFocus
            />
          </div>
          <Button type="submit">搜索</Button>
        </form>
        <Tabs
          value={anchorType ?? 'users'}
          onValueChange={(value) => jumpTo(value as SearchType)}
          className="min-w-0 flex-[1_1_auto]"
        >
          <TabsList aria-label="定位到结果分区">
            <TabsTrigger value="users"><UserIcon className="size-3.5" />{TYPE_LABEL.users}</TabsTrigger>
            <TabsTrigger value="bots"><BotIcon className="size-3.5" />{TYPE_LABEL.bots}</TabsTrigger>
            <TabsTrigger value="matches"><Swords className="size-3.5" />{TYPE_LABEL.matches}</TabsTrigger>
          </TabsList>
        </Tabs>
        <Select
          value={gameId || 'all'}
          onValueChange={(value) => {
            const next = new URLSearchParams(params)
            if (value === 'all') next.delete('game_id')
            else next.set('game_id', value)
            setParams(next)
          }}
        >
          <SelectTrigger className="w-[8.5rem] max-w-full"><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="all">全部游戏</SelectItem>
            {GAMES.map((game) => <SelectItem key={game.id} value={game.id}>{game.label}</SelectItem>)}
          </SelectContent>
        </Select>
      </StickyToolbar>

      {!hasQuery ? (
        <DataRegion title="搜索结果" description="输入关键词后显示结果">
          <EmptyState text="输入关键词开始搜索" icon={<SearchIcon className="size-5 opacity-50" />} className="py-8" />
        </DataRegion>
      ) : loading ? (
        <DataRegion
          title="搜索结果"
          description={`关键词：${query}${gameId ? ` · ${gameLabel(gameId)}` : ''}`}
        >
          <Loading text="正在搜索…" />
        </DataRegion>
      ) : (
        <>
          <SearchSection
            id={SECTION_ID.users}
            label={TYPE_LABEL.users}
            icon={<UserIcon aria-hidden="true" className="size-4 shrink-0" />}
            anchored={anchored === 'users'}
            state={results.users}
          >
            <UserResults users={results.users.items} />
          </SearchSection>
          <SearchSection
            id={SECTION_ID.bots}
            label={TYPE_LABEL.bots}
            icon={<BotIcon aria-hidden="true" className="size-4 shrink-0" />}
            anchored={anchored === 'bots'}
            state={results.bots}
          >
            <BotResults bots={results.bots.items} />
          </SearchSection>
          <SearchSection
            id={SECTION_ID.matches}
            label={TYPE_LABEL.matches}
            icon={<Swords aria-hidden="true" className="size-4 shrink-0" />}
            anchored={anchored === 'matches'}
            state={results.matches}
          >
            <MatchResults matches={results.matches.items} />
          </SearchSection>
        </>
      )}
    </PageFrame>
  )
}

/** 单个结果分区：带 id 的标题（供锚点与 aria-labelledby）、计数、独立错误/空态。 */
function SearchSection({
  id,
  label,
  icon,
  anchored,
  state,
  children,
}: {
  id: string
  label: string
  icon: ReactNode
  anchored: boolean
  state: SectionState<unknown>
  children: ReactNode
}) {
  return (
    <DataRegion
      id={id}
      aria-labelledby={`${id}-heading`}
      data-anchored={anchored || undefined}
      className={cn(
        'scroll-mt-[var(--sticky-table-offset,3.5rem)]',
        anchored && 'border-primary/50 ring-1 ring-primary/30',
      )}
      title={
        <span id={`${id}-heading`} className={cn('inline-flex min-w-0 flex-wrap items-center gap-1.5', anchored && 'text-primary')}>
          {icon}
          <span>{label}</span>
          {!state.error && <span className="font-normal text-muted-foreground">· {state.items.length} 条</span>}
        </span>
      }
    >
      {state.error ? (
        <ErrorMsg msg={state.error} className="px-4 py-6" />
      ) : (
        children
      )}
    </DataRegion>
  )
}

function UserResults({ users }: { users: UserRow[] }) {
  if (users.length === 0) {
    return (
      <div className="flex flex-col items-center gap-1 py-8">
        <EmptyState text="无匹配用户" icon={<UserIcon className="size-5 opacity-50" />} className="py-0" />
        <p className="text-xs text-muted-foreground">换个关键词或检查拼写</p>
      </div>
    )
  }
  return (
    <ul className="divide-y divide-border">
      {users.map((user, index) => (
        <li key={user.id}>
          <Link to={`/user/${encodeURIComponent(user.username)}`} className="grid min-w-0 gap-2 px-3 py-1.5 hover:bg-muted/40 sm:grid-cols-[2rem_2.25rem_minmax(0,1fr)_auto] sm:items-center">
            <span className="hidden font-mono text-xs tabular-nums text-muted-foreground sm:block">{index + 1}</span>
            <span aria-hidden="true" className="flex min-w-0 size-8 shrink-0 items-center justify-center rounded-full bg-muted text-sm font-bold text-muted-foreground">
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
    return (
      <div className="flex flex-col items-center gap-1 py-8">
        <EmptyState text="无匹配 Bot" icon={<BotIcon className="size-5 opacity-50" />} className="py-0" />
        <p className="text-xs text-muted-foreground">换个关键词或检查拼写</p>
      </div>
    )
  }
  return (
    <ul className="grid min-w-0 gap-px bg-border sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4">
      {bots.map((bot, index) => {
        const GameIcon = gameIcon(bot.game_id)
        return (
          <li key={bot.id} className="min-w-0 bg-card">
            <Link to={`/bot/${bot.id}`} className="flex min-w-0 gap-2.5 px-3 py-1.5 hover:bg-muted/40">
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
    return (
      <div className="flex flex-col items-center gap-1 py-8">
        <EmptyState text="无匹配对局" icon={<Swords className="size-5 opacity-50" />} className="py-0" />
        <p className="text-xs text-muted-foreground">换个关键词或检查拼写</p>
      </div>
    )
  }

  return (
    <>
      <div className="hidden md:block">
        <DataTable className="rounded-none border-0" scrollLabel="搜索到的对局">
          <Table aria-label="搜索到的对局" className="min-w-[46rem]">
            <TableHeader>
              <TableRow>
                <TableHead>时间</TableHead>
                <TableHead className="w-full min-w-[16rem]">对阵</TableHead>
                <TableHead>赛果</TableHead>
                <TableHead>游戏 / 性质</TableHead>
                <TableHead className="text-right">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {matches.map((match) => (
                <TableRow key={match.id}>
                  <TableCell className="font-mono text-xs tabular-nums text-muted-foreground">{fmtTime(match.created_at)}</TableCell>
                  <TableCell className="whitespace-normal">
                    <MatchParticipants source={match} variant="inline" />
                  </TableCell>
                  <TableCell className="whitespace-normal">
                    <MatchOutcome
                      source={match}
                      seatLabels={outcomeSeatLabels(match)}
                      primaryOnly
                      className="whitespace-nowrap"
                    />
                  </TableCell>
                  <TableCell>
                    <span className="flex min-w-0 flex-nowrap items-center gap-1.5 whitespace-nowrap">
                      <span className="text-xs text-muted-foreground">{gameLabel(match.game_id)}</span>
                      <MatchNatureBadge matchType={match.match_type} source={match} />
                    </span>
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
          <li key={match.id} className="min-w-0 px-3 py-2">
            <div className="flex min-w-0 flex-wrap items-center gap-2 text-xs text-muted-foreground">
              <span className="font-mono">{index + 1} · {fmtTime(match.created_at)}</span>
              <Badge variant="secondary" className="ml-auto">{gameLabel(match.game_id)}</Badge>
              <MatchNatureBadge matchType={match.match_type} source={match} />
            </div>
            <MatchParticipants source={match} className="mt-2" />
            <MatchOutcome
              source={match}
              seatLabels={outcomeSeatLabels(match)}
              primaryOnly
              className="mt-1.5"
            />
            <Button asChild variant="link" size="xs" className="mt-1 px-0"><Link to={`/match/${encodeURIComponent(match.id)}`}>打开回放</Link></Button>
          </li>
        ))}
      </ul>
    </>
  )
}
