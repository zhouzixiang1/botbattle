import type { ReactNode } from 'react'
import { Activity, Bot, Clock3, ListOrdered, PauseCircle, PlayCircle } from 'lucide-react'
import { Link } from 'react-router-dom'

import { Badge } from '@/components/ui/badge'
import { Card } from '@/components/ui/card'
import { ErrorMsg, Loading } from '@/components/ui/status'
import { fmtRating } from '@/lib/format'
import { cn } from '@/lib/utils'

export interface AutoMatchQueueBot {
  id: number
  name: string
  display_name: string
  owner: { username: string; display_name: string }
  rating: number
  matches_played: number
  is_placement: boolean
  placement_remaining: number
}

export interface AutoMatchQueueRow {
  id: number
  status: 'queued' | 'dispatched'
  position: number
  game_id: string
  match_id?: string | null
  match_status?: string | null
  started_at?: string | null
  created_at?: string | null
  reason: string
  bot_a: AutoMatchQueueBot
  bot_b: AutoMatchQueueBot
}

export interface AutoMatchQueueSnapshot {
  game_id?: string | null
  enabled: boolean
  effective_enabled: boolean
  capability_enabled: boolean
  paused: boolean
  pause_reason: string
  rating_projection_ready: boolean
  dispatcher_leader: boolean
  placement_required: number
  policy: {
    serial: boolean
    lookahead: number
    foreground_slot_reserved: boolean
  }
  active?: AutoMatchQueueRow | null
  active_game_id?: string | null
  upcoming: AutoMatchQueueRow[]
}

const GAME_LABEL: Record<string, string> = {
  holdem: '德州扑克',
  gomoku: '五子棋',
  pencil: '点格棋',
}

function BotIdentity({ bot }: { bot: AutoMatchQueueBot }) {
  const name = bot.display_name || bot.name || `Bot #${bot.id}`
  return (
    <div className="min-w-0">
      <Link
        to={`/bot/${bot.id}`}
        className="block break-words text-sm font-medium leading-snug text-foreground hover:text-primary [overflow-wrap:anywhere]"
      >
        {name}
      </Link>
      <div className="mt-0.5 flex min-w-0 flex-wrap items-center gap-x-1.5 gap-y-0.5 text-[11px] text-muted-foreground">
        {bot.owner.username ? (
          <Link
            to={`/user/${encodeURIComponent(bot.owner.username)}`}
            className="break-all hover:text-primary hover:underline"
          >
            @{bot.owner.username}
          </Link>
        ) : (
          <span>所有者未知</span>
        )}
        <span className="font-mono tabular-nums">{fmtRating(bot.rating)}</span>
        {bot.is_placement && (
          <span>定级还差 {bot.placement_remaining} 场</span>
        )}
      </div>
    </div>
  )
}

function Versus({ row }: { row: AutoMatchQueueRow }) {
  return (
    <div className="grid min-w-0 grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)] items-center gap-2">
      <BotIdentity bot={row.bot_a} />
      <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] font-semibold text-muted-foreground">VS</span>
      <div className="min-w-0 text-right [&_a]:text-right [&_div]:justify-end">
        <BotIdentity bot={row.bot_b} />
      </div>
    </div>
  )
}

function ActiveMatch({ row }: { row: AutoMatchQueueRow }) {
  return (
    <div className="rounded-lg border border-primary/25 bg-primary/5 px-3 py-2.5" data-testid="auto-match-active">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2 text-xs">
        <span className="inline-flex items-center gap-1.5 font-medium text-primary">
          <span className="relative flex size-2">
            <span className="absolute inline-flex size-full animate-ping rounded-full bg-primary opacity-60" />
            <span className="relative inline-flex size-2 rounded-full bg-primary" />
          </span>
          正在进行 · {GAME_LABEL[row.game_id] || row.game_id}
        </span>
        {row.match_id && (
          <Link to={`/match/${encodeURIComponent(row.match_id)}`} className="font-medium text-primary hover:underline">
            进入观赛
          </Link>
        )}
      </div>
      <Versus row={row} />
    </div>
  )
}

function UpcomingMatch({ row }: { row: AutoMatchQueueRow }) {
  return (
    <li className="min-w-0 rounded-lg border border-border bg-muted/20 px-3 py-2.5" data-testid="auto-match-upcoming">
      <div className="mb-2 flex min-w-0 items-center justify-between gap-2 text-[11px] text-muted-foreground">
        <span className="inline-flex shrink-0 items-center gap-1 font-mono font-semibold text-foreground">
          <ListOrdered className="size-3" /> #{row.position}
        </span>
        <span className="truncate">{GAME_LABEL[row.game_id] || row.game_id}</span>
      </div>
      <Versus row={row} />
      <p className="mt-2 break-words text-[10px] leading-relaxed text-muted-foreground [overflow-wrap:anywhere]">
        {row.reason}
      </p>
    </li>
  )
}

export function AutoMatchQueuePanel({
  snapshot,
  loading = false,
  error = '',
  action,
  maxUpcoming,
  className,
}: {
  snapshot: AutoMatchQueueSnapshot | null
  loading?: boolean
  error?: string
  action?: ReactNode
  maxUpcoming?: number
  className?: string
}) {
  const upcoming = maxUpcoming == null
    ? snapshot?.upcoming || []
    : (snapshot?.upcoming || []).slice(0, maxUpcoming)
  const hidden = Math.max(0, (snapshot?.upcoming.length || 0) - upcoming.length)

  return (
    <Card className={cn('gap-0 overflow-hidden py-0', className)} data-testid="auto-match-queue-panel">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-3 py-2.5 sm:px-4">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="inline-flex items-center gap-1.5 text-sm font-semibold">
              <Bot className="size-4 text-primary" /> 自动排位队列
            </h2>
            {snapshot && (
              <Badge variant={snapshot.effective_enabled && !snapshot.paused ? 'default' : 'secondary'}>
                {snapshot.effective_enabled
                  ? snapshot.paused ? '等待中' : '持续运行'
                  : '已暂停'}
              </Badge>
            )}
          </div>
          <p className="mt-0.5 text-[11px] leading-relaxed text-muted-foreground">
            全局串行；按游戏与定级通道轮转，优先平衡所有者服务份额、交手次数和先后手。
          </p>
        </div>
        {action}
      </div>

      {error ? (
        <div className="px-3 py-3"><ErrorMsg msg={error} /></div>
      ) : loading && !snapshot ? (
        <div className="py-4"><Loading /></div>
      ) : !snapshot ? null : (
        <div className="space-y-3 px-3 py-3 sm:px-4">
          {snapshot.active ? (
            <ActiveMatch row={snapshot.active} />
          ) : snapshot.active_game_id && snapshot.game_id ? (
            <div className="flex items-center gap-2 rounded-lg border border-border bg-muted/20 px-3 py-2 text-xs text-muted-foreground">
              <Activity className="size-3.5 shrink-0 text-primary" />
              当前由{GAME_LABEL[snapshot.active_game_id] || snapshot.active_game_id}占用全局自动排位槽
            </div>
          ) : null}

          {upcoming.length > 0 ? (
            <div>
              <div className="mb-2 flex items-center justify-between gap-2 text-xs">
                <span className="inline-flex items-center gap-1.5 font-medium">
                  <Clock3 className="size-3.5 text-muted-foreground" /> 即将进行
                </span>
                <span className="text-muted-foreground">共 {snapshot.upcoming.length} 场</span>
              </div>
              <ol className="grid min-w-0 gap-2 xl:grid-cols-2">
                {upcoming.map((row) => <UpcomingMatch key={row.id} row={row} />)}
              </ol>
              {hidden > 0 && (
                <p className="mt-2 text-right text-[11px] text-muted-foreground">另有 {hidden} 场在后续队列</p>
              )}
            </div>
          ) : !snapshot.active && !(snapshot.active_game_id && snapshot.game_id) ? (
            <div className="flex items-start gap-2 rounded-lg border border-dashed border-border px-3 py-3 text-xs text-muted-foreground">
              {snapshot.paused ? <PauseCircle className="mt-0.5 size-4 shrink-0" /> : <PlayCircle className="mt-0.5 size-4 shrink-0" />}
              <span className="break-words [overflow-wrap:anywhere]">
                {snapshot.pause_reason || '正在生成下一组公平配对'}
              </span>
            </div>
          ) : null}
        </div>
      )}
    </Card>
  )
}
