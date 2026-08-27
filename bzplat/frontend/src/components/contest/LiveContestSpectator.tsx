import { Eye, Radio, RefreshCw } from 'lucide-react'
import { Link } from 'react-router-dom'

import { MatchParticipantIdentity } from '@/components/MatchParticipants'
import { PairingResult } from '@/components/contest/pairing-result'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { EntityName } from '@/components/ui/overflow-text'
import { fmtTime } from '@/lib/format'
import type { MatchParticipantSource } from '@/lib/match-participants'
import { cn } from '@/lib/utils'

export interface LiveContestPairing extends MatchParticipantSource {
  id: number
  round_num: number
  bracket_slot?: number | null
  bot_a_id: number | null
  bot_b_id: number | null
  is_bye?: boolean
  match_id?: string | null
  status: 'pending' | 'running' | 'completed'
  display_status: 'pending' | 'queued' | 'running' | 'completed'
  stage_idx: number
  stage_key?: string | null
  group_id?: string | null
  match_winner?: number | null
  scheduled_at?: string | null
  started_at?: string | null
  ended_at?: string | null
  series_index: number
  series_size: number
  bye?: boolean
  series_summary?: {
    bye?: boolean
    series_size: number
    completed_matches: number
    game_points_a: number | null
    game_points_b: number | null
    normalized_delta_a: number
    settled: boolean
    standings_points_a: number | null
    standings_points_b: number | null
  } | null
}

export interface LiveContestStanding {
  bot_id?: number | null
  bot_name?: string
  owner_name?: string
  owner_display?: string
  points: number
  wins: number
  draws: number
  losses: number
  byes?: number
  delta_total?: number
  group_id?: string | null
  rank: number
}

interface LiveContestSpectatorProps {
  status: string
  stageLabel: string
  stageType?: string
  duplicate: boolean
  snapshot?: boolean
  progress: {
    completed: number
    total: number
    running: number
    pending: number
  }
  activePairings: LiveContestPairing[]
  upcomingPairings: LiveContestPairing[]
  recentPairings: LiveContestPairing[]
  standings: LiveContestStanding[]
  lastUpdatedAt: number | null
  polling: boolean
  offline: boolean
  refreshEnabled?: boolean
  onRefresh: () => void
}

function compareSchedule(a: LiveContestPairing, b: LiveContestPairing): number {
  const round = (a.round_num ?? 1) - (b.round_num ?? 1)
  if (round !== 0) return round
  const slot = (a.bracket_slot ?? Number.MAX_SAFE_INTEGER) - (b.bracket_slot ?? Number.MAX_SAFE_INTEGER)
  return slot !== 0 ? slot : a.id - b.id
}

function seriesGroupKey(pairing: LiveContestPairing, stageType?: string): string {
  if (pairing.is_bye || pairing.bye) return `bye:${pairing.id}`
  if (!pairing.series_size || pairing.series_size <= 1) return `match:${pairing.id}`
  const players = [
    pairing.bot_a_id ?? pairing.owner_a_name ?? pairing.bot_a_name ?? `unknown-a-${pairing.id}`,
    pairing.bot_b_id ?? pairing.owner_b_name ?? pairing.bot_b_name ?? `unknown-b-${pairing.id}`,
  ].sort().join(':')
  const round = stageType === 'swiss' ? pairing.round_num ?? 1 : 0
  return `${pairing.stage_key || pairing.stage_idx}:${round}:${pairing.group_id || ''}:${players}`
}

function groupSeries(pairings: LiveContestPairing[], stageType?: string): LiveContestPairing[][] {
  const groups = new Map<string, LiveContestPairing[]>()
  for (const pairing of pairings) {
    const key = seriesGroupKey(pairing, stageType)
    const group = groups.get(key) || []
    group.push(pairing)
    groups.set(key, group)
  }
  return Array.from(groups.values()).map((group) => group.sort((a, b) => a.series_index - b.series_index))
}

function signedBb(value: number): string {
  const rounded = Math.round(value * 100) / 100
  return `${rounded > 0 ? '+' : ''}${rounded}BB`
}

function SeriesScoreline({ pairing }: { pairing: LiveContestPairing }) {
  const summary = pairing.series_summary
  if (pairing.is_bye || pairing.bye) {
    const points = summary?.standings_points_a
    return (
      <div className="mt-2 rounded-md border border-primary/20 bg-primary/5 px-3 py-2">
        <p className="text-lg font-semibold tabular-nums text-primary">轮空{points != null ? ` · +${points} 赛事积分` : ''}</p>
        <p className="text-xs text-muted-foreground">本轮没有生成实际对局。</p>
      </div>
    )
  }
  if (!summary) return null
  return (
    <div className="mt-2 rounded-md border bg-muted/25 px-3 py-2">
      <p className="text-lg font-semibold tabular-nums text-foreground">
        {summary.settled && summary.standings_points_a != null && summary.standings_points_b != null
          ? `${summary.standings_points_a}–${summary.standings_points_b} 赛事积分`
          : '本轮积分待结算'}
      </p>
      <p className="mt-0.5 text-xs leading-relaxed text-muted-foreground">
        本轮交锋 {summary.series_size} 场 · 已完成 {summary.completed_matches}/{summary.series_size}
        {' · '}小分 {summary.game_points_a ?? 0}–{summary.game_points_b ?? 0}
        {' · '}净胜 {signedBb(summary.normalized_delta_a)}
      </p>
    </div>
  )
}

function PairingIdentityLine({
  pairings,
  showResult = false,
  duplicate = false,
}: {
  pairings: LiveContestPairing[]
  showResult?: boolean
  duplicate?: boolean
}) {
  const pairing = pairings[0]
  if (!pairing) return null
  return (
    <li className="min-w-0 py-2.5 first:pt-0 last:pb-0">
      <div className="flex min-w-0 items-center gap-2 text-xs text-muted-foreground">
        <span className="shrink-0 font-mono font-medium tabular-nums text-foreground">
          {pairing.group_id ? `${pairing.group_id} · ` : ''}R{pairing.round_num ?? 1}
        </span>
        {pairing.series_size && pairing.series_size > 1 && (
          <span className="shrink-0 font-mono tabular-nums">
            第 {pairing.series_index ?? 1}/{pairing.series_size} 场
          </span>
        )}
        {showResult && duplicate && !pairing.is_bye && !pairing.bye ? (
          <span className="min-w-0 truncate font-medium text-foreground">复式对局已裁决</span>
        ) : showResult ? (
          <PairingResult pairing={pairing} className="min-w-0 truncate" />
        ) : pairing.display_status === 'queued' ? (
          <span className="min-w-0 truncate">已派桌，等待启动</span>
        ) : (
          pairing.scheduled_at ? (
            <time dateTime={pairing.scheduled_at} className="min-w-0 truncate">
              计划 {fmtTime(pairing.scheduled_at)}
            </time>
          ) : (
            <span className="min-w-0 truncate">等待调度</span>
          )
        )}
        {pairing.match_id && (
          <Link
            to={`/match/${pairing.match_id}`}
            className="ml-auto inline-flex min-h-11 shrink-0 touch-manipulation items-center text-primary underline-offset-4 hover:underline focus-visible:rounded-sm focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50 sm:min-h-8"
          >
            {showResult ? '回看' : '详情'}
          </Link>
        )}
      </div>
      <div className="mt-1 grid min-w-0 grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)] items-start gap-2">
        <MatchParticipantIdentity source={pairing} side={0} textLines={2} />
        <span aria-hidden="true" className="pt-3 text-xs font-semibold text-muted-foreground">VS</span>
        <MatchParticipantIdentity
          source={pairing}
          side={1}
          textLines={2}
          emptyLabel={pairing.is_bye ? '轮空 (bye)' : undefined}
        />
      </div>
      <SeriesScoreline pairing={pairing} />
      {pairings.length > 1 && (
        <div className="mt-2 flex min-w-0 flex-wrap gap-1.5" aria-label="本轮交锋的实际对局">
          {pairings.map((item) => item.match_id ? (
            <Link
              key={item.id}
              to={`/match/${item.match_id}`}
              className="inline-flex min-h-11 items-center rounded-md border px-2.5 text-xs font-medium text-primary hover:bg-muted focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50 sm:min-h-8"
            >
              第 {item.series_index}/{item.series_size} 场{showResult ? '回看' : '详情'}
            </Link>
          ) : (
            <span key={item.id} className="inline-flex min-h-11 items-center rounded-md bg-muted px-2.5 text-xs text-muted-foreground sm:min-h-8">
              第 {item.series_index}/{item.series_size} 场待调度
            </span>
          ))}
        </div>
      )}
    </li>
  )
}

function ActiveTable({
  pairing,
  tableNumber,
  snapshot,
}: {
  pairing: LiveContestPairing
  tableNumber: number
  snapshot: boolean
}) {
  return (
    <article
      data-testid="contest-live-table"
      aria-label={`${snapshot ? '演示' : '正在进行的'}第 ${tableNumber} 桌`}
      className="min-w-0 border-t border-primary/15 py-3 first:border-t-0 first:pt-0 last:pb-0"
    >
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <Badge variant="outline" className="border-primary/25 bg-primary/5 text-primary">
          <span className="mr-1 inline-flex size-2 rounded-full bg-primary" aria-hidden="true" />
          {snapshot ? '演示' : '直播'} · 第 {tableNumber} 桌
        </Badge>
        <span className="text-xs font-medium text-muted-foreground">
          {pairing.group_id ? `${pairing.group_id} · ` : ''}第 {pairing.round_num ?? 1} 轮
          {pairing.series_size && pairing.series_size > 1
            ? ` · 系列第 ${pairing.series_index ?? 1}/${pairing.series_size} 场`
            : ''}
        </span>
      </div>
      <div className="mt-2 grid min-w-0 grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)] items-stretch gap-2">
        <MatchParticipantIdentity source={pairing} side={0} variant="panel" textLines={2} />
        <span aria-hidden="true" className="self-center text-xs font-semibold tracking-wide text-muted-foreground">VS</span>
        <MatchParticipantIdentity source={pairing} side={1} variant="panel" textLines={2} />
      </div>
      {pairing.match_id ? (
        <Button asChild size="sm" className="mt-3 min-h-11 w-full sm:w-auto">
          <Link to={`/match/${pairing.match_id}`} aria-label={`${snapshot ? '查看演示' : '进入'}第 ${tableNumber} 桌观赛`}>
            <Eye aria-hidden="true" className="size-4" />{snapshot ? '查看演示对局' : '进入实时观赛'}
          </Link>
        </Button>
      ) : (
        <p className="mt-2 text-xs text-muted-foreground">对局正在建立，观赛入口生成后会自动出现。</p>
      )}
    </article>
  )
}

export function LiveContestSpectator({
  status,
  stageLabel,
  stageType,
  duplicate,
  snapshot = false,
  progress: progressValue,
  activePairings,
  upcomingPairings,
  recentPairings,
  standings,
  lastUpdatedAt,
  polling,
  offline,
  refreshEnabled = true,
  onRefresh,
}: LiveContestSpectatorProps) {
  const active = [...activePairings].sort(compareSchedule)
  const upcoming = upcomingPairings
    .filter((pairing) => !pairing.is_bye)
    .sort(compareSchedule)
  const recent = recentPairings
  const activeGroups = groupSeries(active, stageType)
  const upcomingGroups = groupSeries(upcoming, stageType)
  const recentGroups = groupSeries(recent, stageType)
  const completed = Math.max(0, progressValue.completed)
  const total = Math.max(0, progressValue.total)
  const progress = total > 0 ? Math.min(100, Math.round((completed / total) * 100)) : 0
  const ranked = [...standings]
    .sort((a, b) => a.rank - b.rank)
  const hasGroupedStandings = ranked.some((row) => Boolean(row.group_id))
  const isRest = status === 'rest'
  const isPublished = status === 'published'
  const isFinished = status === 'finished'
  const isCancelled = status === 'cancelled'
  const isBeforePublication = status === 'draft' || status === 'open'
  const frozenTerminal = !refreshEnabled && (isFinished || isCancelled)
  const liveTitle = snapshot
    ? '演示赛况快照'
    : isRest
    ? '阶段间歇'
    : isPublished
      ? '直播间已开放'
      : isFinished
        ? '赛事已结束'
        : isCancelled
          ? '赛事已取消'
          : isBeforePublication
            ? '赛事直播尚未开放'
          : '赛事实况'
  const emptyActiveTitle = isRest
    ? '本阶段已收官'
    : isPublished
      ? '赛事尚未开赛'
      : isFinished
        ? '全部对局已结束'
        : isCancelled
          ? '本赛事已取消'
          : isBeforePublication
            ? '赛程尚未发布'
          : '当前暂无已派桌对局'

  return (
    <section
      data-testid="contest-live-spectator"
      aria-labelledby="contest-live-title"
      className="min-w-0 overflow-hidden rounded-xl border border-primary/25 bg-card shadow-xs"
    >
      <header className="flex min-w-0 flex-col gap-3 border-b border-primary/15 bg-primary/[0.035] px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0">
          <div className="flex min-w-0 items-center gap-2">
            <Radio aria-hidden="true" className="size-4 shrink-0 text-primary" />
            {status === 'running' && !snapshot && (
              <span className="relative flex size-2 shrink-0" aria-hidden="true">
                <span className="absolute inline-flex size-full animate-ping rounded-full bg-primary/45 motion-reduce:animate-none" />
                <span className="relative inline-flex size-2 rounded-full bg-primary" />
              </span>
            )}
            <h2 id="contest-live-title" className="text-base font-semibold text-foreground">
              {liveTitle}
            </h2>
            {stageLabel && <span className="text-xs font-medium text-primary">{stageLabel}</span>}
          </div>
          <p data-testid="contest-live-sync-status" className="mt-1 text-xs text-muted-foreground">
            {offline
              ? '当前离线，保留最后一次赛况。'
              : snapshot && lastUpdatedAt
                ? `快照时间 · ${new Date(lastUpdatedAt).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })}`
              : frozenTerminal && lastUpdatedAt
                ? `最终赛况 · ${new Date(lastUpdatedAt).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })}`
              : polling
                ? '正在同步最新赛况…'
                : lastUpdatedAt
                  ? `已自动更新 · ${new Date(lastUpdatedAt).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })}`
                  : '正在载入最新赛况…'}
          </p>
          <span className="sr-only" aria-live="polite" aria-atomic="true">
            {offline
              ? '已离线，保留最后一次赛况。'
              : snapshot
                ? `${liveTitle}，内容固定。`
                : refreshEnabled
                  ? `${liveTitle}，已连接自动更新。`
                  : `${liveTitle}，赛况已冻结。`}
          </span>
        </div>
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="min-h-11 w-full sm:w-auto"
          onClick={onRefresh}
          disabled={!refreshEnabled || polling || offline}
          aria-busy={polling || undefined}
        >
          <RefreshCw aria-hidden="true" className={cn('size-4', polling && 'animate-spin motion-reduce:animate-none')} />
          {refreshEnabled ? '刷新赛况' : '赛况已冻结'}
        </Button>
      </header>

      <div className="border-b px-4 py-3">
        <div className="flex items-end justify-between gap-3 text-xs">
          <span className="font-medium text-foreground">本阶段进度</span>
          <span className="font-mono tabular-nums text-muted-foreground">
            {completed} / {total} 场 · {progress}%
          </span>
        </div>
        <p className="mt-1 text-xs text-muted-foreground">
          {progressValue.running} 桌进行中 · {progressValue.pending} 场待赛
        </p>
        <div
          role="progressbar"
          aria-label="本阶段已完成对局"
          aria-valuemin={0}
          aria-valuemax={Math.max(total, 1)}
          aria-valuenow={Math.min(completed, Math.max(total, 1))}
          aria-valuetext={`${completed} / ${total} 场已完成`}
          className="mt-2 h-1.5 overflow-hidden rounded-full bg-muted"
        >
          <div
            className="h-full rounded-full bg-primary transition-[width] duration-300 motion-reduce:transition-none"
            style={{ width: `${progress}%` }}
          />
        </div>
      </div>

      <div className="grid min-w-0 xl:grid-cols-[minmax(0,1.55fr)_minmax(17rem,0.75fr)]">
        <section aria-labelledby="active-tables-title" className="min-w-0 px-4 py-4 xl:border-r">
          <div className="mb-3 flex items-center justify-between gap-3">
            <h3 id="active-tables-title" className="text-sm font-semibold text-foreground">{snapshot ? '演示桌台' : '正在进行'}</h3>
            <span className="font-mono text-xs tabular-nums text-muted-foreground">{active.length} 桌</span>
          </div>
          {active.length > 0 ? (
            <div className="divide-y divide-primary/15">
              {activeGroups.map((group) => (
                <div key={seriesGroupKey(group[0]!, stageType)} className="py-3 first:pt-0 last:pb-0">
                  <SeriesScoreline pairing={group[0]!} />
                  <div className="mt-3">
                    {group.map((pairing) => (
                      <ActiveTable
                        key={pairing.id}
                        pairing={pairing}
                        tableNumber={active.findIndex((item) => item.id === pairing.id) + 1}
                        snapshot={snapshot}
                      />
                    ))}
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <div className="flex min-h-28 items-center rounded-lg border border-dashed px-4 py-5">
              <div>
                <p className="text-sm font-medium text-foreground">{emptyActiveTitle}</p>
                <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
                  {isRest
                    ? '查看最近赛果和阶段排名，下一阶段开始后这里会自动出现直播入口。'
                    : isPublished
                      ? '对阵已发布，开赛后这里会优先显示正在进行的桌台。'
                      : isFinished || isCancelled || isBeforePublication
                        ? '你仍可以查看阶段排名、最近赛果与已产生的对局回放。'
                        : upcoming.length > 0
                          ? '下一场正在等待平台调度，页面会自动更新。'
                          : '排期或阶段推进后，直播桌台会优先显示在这里。'}
                </p>
              </div>
            </div>
          )}
        </section>

        <section aria-labelledby="live-standings-title" className="min-w-0 border-t px-4 py-4 xl:border-t-0">
          <div className="mb-3 flex items-center justify-between gap-3">
            <h3 id="live-standings-title" className="text-sm font-semibold text-foreground">{hasGroupedStandings ? '各组前列' : '阶段前列'}</h3>
            <span className="text-xs text-muted-foreground">{snapshot ? '快照积分' : '实时积分'}</span>
          </div>
          {ranked.length > 0 ? (
            <ol className="divide-y divide-border">
              {ranked.map((row) => {
                const groupLabel = row.group_id
                  ? `${row.group_id.endsWith('组') ? row.group_id : `${row.group_id}组`} · `
                  : ''
                return (
                  <li key={`${row.group_id || 'all'}-${row.rank}-${row.bot_id ?? row.bot_name}`} className="grid min-w-0 grid-cols-[max-content_minmax(0,1fr)_auto] items-center gap-2 py-2 first:pt-0 last:pb-0">
                    <span className="font-mono text-sm font-semibold tabular-nums text-primary">{groupLabel}{row.rank}</span>
                    <div className="min-w-0">
                      {row.bot_id != null ? (
                        <Link to={`/bot/${row.bot_id}`} className="hover:text-primary">
                          <EntityName tooltip={row.bot_name || 'Bot 名称不可用'} tooltipFocusable={false} className="text-sm font-medium hover:text-primary">
                            {row.bot_name || 'Bot 名称不可用'}
                          </EntityName>
                        </Link>
                      ) : (
                        <EntityName tooltip={row.bot_name || 'Bot 已删除'} tooltipFocusable={false} className="text-sm font-medium">
                          {row.bot_name || 'Bot 已删除'}
                        </EntityName>
                      )}
                      <p className="truncate text-xs text-muted-foreground">
                        {row.owner_display || row.owner_name || `${row.wins} 胜 · ${row.draws} 平 · ${row.losses} 负`}
                      </p>
                    </div>
                    <span className="font-mono text-sm font-semibold tabular-nums text-foreground">{row.points} 分</span>
                  </li>
                )
              })}
            </ol>
          ) : (
            <p className="py-5 text-sm text-muted-foreground">首批赛果产生后显示阶段排名。</p>
          )}
        </section>
      </div>

      <div className="grid min-w-0 border-t md:grid-cols-2">
        <section aria-labelledby="upcoming-matches-title" className="min-w-0 px-4 py-4 md:border-r">
          <h3 id="upcoming-matches-title" className="text-sm font-semibold text-foreground">接下来</h3>
          {upcoming.length > 0 ? (
            <ul className="mt-3 divide-y divide-border">{upcomingGroups.map((group) => <PairingIdentityLine key={seriesGroupKey(group[0]!, stageType)} pairings={group} />)}</ul>
          ) : (
            <p className="mt-2 text-xs text-muted-foreground">当前阶段暂无待进行对局。</p>
          )}
        </section>
        <section aria-labelledby="recent-results-title" className="min-w-0 border-t px-4 py-4 md:border-t-0">
          <h3 id="recent-results-title" className="text-sm font-semibold text-foreground">最近赛果</h3>
          {recent.length > 0 ? (
            <ul className="mt-3 divide-y divide-border">{recentGroups.map((group) => <PairingIdentityLine key={seriesGroupKey(group[0]!, stageType)} pairings={group} showResult duplicate={duplicate} />)}</ul>
          ) : (
            <p className="mt-2 text-xs text-muted-foreground">本阶段尚无已完成对局。</p>
          )}
        </section>
      </div>
    </section>
  )
}
