import { useEffect, useMemo, useRef, useState } from 'react'
import { ArrowLeft, CalendarClock, Radio, WifiOff } from 'lucide-react'
import { Link, useParams } from 'react-router-dom'

import { ApiError, apiFetch, errMsg } from '@/api'
import {
  LiveContestSpectator,
  type LiveContestPairing,
  type LiveContestStanding,
} from '@/components/contest/LiveContestSpectator'
import { PageFrame, PageHeader } from '@/components/layout'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Skeleton } from '@/components/ui/skeleton'
import { ErrorMsg, StatusBadge } from '@/components/ui/status'
import { useSingleFlightPolling } from '@/hooks/use-single-flight-polling'
import { fmtTime } from '@/lib/format'
import { gameLabel } from '@/lib/games'

type ContestStatus = 'draft' | 'open' | 'published' | 'running' | 'rest' | 'finished' | 'cancelled'

interface LiveContestResponse {
  contest: {
    id: number
    title: string
    game_id: string
    status: ContestStatus
    showcase: boolean
    immutable: boolean
    official_results_ready: boolean
    starts_at: string | null
    ends_at: string | null
    rest_ends_at: string | null
  }
  stage: {
    index: number
    key: string
    label: string
    type: string
  } | null
  series: {
    games_per_pair: number | null
    duplicate: boolean
    scoring_legs_per_match: number
    scoring_legs_per_pair: number | null
  }
  progress: {
    completed: number
    total: number
    running: number
    pending: number
  }
  active: LiveContestPairing[]
  upcoming: LiveContestPairing[]
  recent: LiveContestPairing[]
  standings: LiveContestStanding[]
  updated_at: string
  generated_at: string
}

interface ScopedSnapshot {
  contestId: string
  value: LiveContestResponse
}

class ScopedContestLiveError {
  constructor(
    readonly contestId: string,
    readonly generation: number,
    readonly reason: unknown,
  ) {}
}

function intervalForContest(contest: LiveContestResponse['contest'] | undefined): number | null {
  if (contest?.status === 'running') return 2_000
  if (contest?.status === 'published' || contest?.status === 'rest') return 10_000
  if (contest?.status === 'finished' && !contest.official_results_ready) return 10_000
  return null
}

function seriesSummary(series: LiveContestResponse['series']): string | null {
  if (series.games_per_pair == null) return null
  if (series.duplicate) {
    const plannedLegs = series.scoring_legs_per_pair
    return plannedLegs == null
      ? `${series.games_per_pair} 场复式对局`
      : `${series.games_per_pair} 场复式对局 · 正常完成时 ${plannedLegs} 局计分`
  }
  return `每对选手交手 ${series.games_per_pair} 场`
}

function ContestLiveSkeleton() {
  return (
    <section
      role="status"
      aria-label="正在加载赛事直播"
      className="overflow-hidden rounded-xl border"
    >
      <div className="flex items-center justify-between gap-3 border-b px-4 py-4">
        <div className="space-y-2">
          <Skeleton className="h-5 w-36" />
          <Skeleton className="h-3 w-48 max-w-[60vw]" />
        </div>
        <Skeleton className="h-11 w-28" />
      </div>
      <div className="space-y-2 border-b px-4 py-4">
        <Skeleton className="h-3 w-44" />
        <Skeleton className="h-2 w-full" />
      </div>
      <div className="grid min-w-0 gap-0 xl:grid-cols-[minmax(0,1.55fr)_minmax(17rem,0.75fr)]">
        <div className="space-y-3 px-4 py-5 xl:border-r">
          <Skeleton className="h-4 w-24" />
          <Skeleton className="h-28 w-full" />
          <Skeleton className="h-28 w-full" />
        </div>
        <div className="space-y-3 border-t px-4 py-5 xl:border-t-0">
          <Skeleton className="h-4 w-20" />
          {Array.from({ length: 4 }, (_, index) => <Skeleton key={index} className="h-10 w-full" />)}
        </div>
      </div>
    </section>
  )
}

export default function ContestLive() {
  const { id } = useParams<{ id: string }>()
  const contestId = id ?? ''
  const validContestId = /^\d+$/.test(contestId)
  const currentScopeRef = useRef(contestId)
  const requestGenerationRef = useRef(0)
  const [snapshot, setSnapshot] = useState<ScopedSnapshot | null>(null)
  const [error, setError] = useState('')
  const [unavailableScope, setUnavailableScope] = useState<string | null>(null)

  currentScopeRef.current = contestId
  const unavailable = unavailableScope === contestId
  const live = !unavailable && snapshot?.contestId === contestId ? snapshot.value : null
  const immutableSnapshot = Boolean(live?.contest.showcase || live?.contest.immutable)
  const pollInterval = immutableSnapshot ? null : intervalForContest(live?.contest)
  const pollingEnabled = validContestId && !unavailable && (live == null || pollInterval != null)

  useEffect(() => {
    ++requestGenerationRef.current
    setError('')
    setUnavailableScope(null)
  }, [contestId])

  const task = async (signal: AbortSignal) => {
    const requestScope = contestId
    const generation = ++requestGenerationRef.current
    let value: LiveContestResponse
    try {
      value = await apiFetch<LiveContestResponse>(
        `/api/contests/${encodeURIComponent(requestScope)}/live`,
        { signal },
      )
    } catch (reason) {
      if (signal.aborted) throw reason
      throw new ScopedContestLiveError(requestScope, generation, reason)
    }
    if (
      signal.aborted
      || currentScopeRef.current !== requestScope
      || generation !== requestGenerationRef.current
    ) return
    setSnapshot({ contestId: requestScope, value })
  }

  const { refresh, polling, offline } = useSingleFlightPolling({
    task,
    enabled: pollingEnabled,
    intervalMs: pollInterval ?? 10_000,
    initialDelayMs: live == null ? 0 : (pollInterval ?? 0),
    maxIntervalMs: pollInterval === 2_000 ? 16_000 : 40_000,
    scopeKey: contestId,
    onError: (scopedError) => {
      if (
        !(scopedError instanceof ScopedContestLiveError)
        || scopedError.contestId !== currentScopeRef.current
        || scopedError.generation !== requestGenerationRef.current
      ) return
      const reason = scopedError.reason
      if (reason instanceof ApiError && reason.status === 404) {
        setSnapshot((current) => current?.contestId === scopedError.contestId ? null : current)
        setUnavailableScope(scopedError.contestId)
        setError('赛事不存在或当前不可见')
        return
      }
      setError(errMsg(reason, '赛事直播加载失败'))
    },
    onSuccess: () => {
      setUnavailableScope(null)
      setError('')
    },
  })

  const updatedAt = useMemo(() => {
    const timestamp = immutableSnapshot ? live?.updated_at : live?.generated_at
    if (!timestamp) return null
    const value = new Date(timestamp).getTime()
    return Number.isFinite(value) ? value : null
  }, [immutableSnapshot, live?.generated_at, live?.updated_at])

  if (!validContestId) {
    return (
      <PageFrame width="narrow" layout="contest-live">
        <PageHeader title="赛事直播" description="赛事编号无效。" />
        <ErrorMsg msg="无法识别要查看的赛事。" />
        <Button asChild variant="outline" className="min-h-11 w-fit">
          <Link to="/contests"><ArrowLeft aria-hidden="true" className="size-4" />返回赛事列表</Link>
        </Button>
      </PageFrame>
    )
  }

  if (!live) {
    return (
      <PageFrame width="wide" layout="contest-live">
        <PageHeader
          eyebrow={<span className="inline-flex items-center gap-1.5"><Radio aria-hidden="true" className="size-3.5 text-primary" />赛事直播</span>}
          title="正在进入直播间"
          description={offline ? '当前离线，联网后会自动加载。' : '正在读取当前桌台、赛程与排名。'}
          actions={(
            <Button asChild variant="outline" className="min-h-11">
              <Link to={`/contests/${contestId}`}><ArrowLeft aria-hidden="true" className="size-4" />返回赛事详情</Link>
            </Button>
          )}
        />
        {offline ? (
          <div className="flex min-h-36 items-center justify-center gap-2 rounded-xl border border-dashed px-4 text-sm text-muted-foreground">
            <WifiOff aria-hidden="true" className="size-4" />无网络连接，已暂停请求。
          </div>
        ) : polling && !error ? (
          <ContestLiveSkeleton />
        ) : unavailable ? (
          <div className="flex min-h-36 flex-col items-center justify-center gap-3 rounded-xl border border-dashed px-4">
            <ErrorMsg msg={error} />
            <Button asChild variant="outline" className="min-h-11">
              <Link to="/contests"><ArrowLeft aria-hidden="true" className="size-4" />返回赛事列表</Link>
            </Button>
          </div>
        ) : (
          <div className="flex min-h-36 flex-col items-center justify-center gap-3 rounded-xl border border-dashed px-4">
            <ErrorMsg msg={error || '暂时无法读取赛况。'} />
            <Button type="button" variant="outline" className="min-h-11" onClick={refresh} disabled={offline || polling}>
              重试
            </Button>
          </div>
        )}
      </PageFrame>
    )
  }

  const status = live.contest.status
  const terminal = status === 'cancelled' || (status === 'finished' && live.contest.official_results_ready)
  const seriesText = seriesSummary(live.series)
  const scheduleText = status === 'rest' && live.contest.rest_ends_at
    ? `休息至 ${fmtTime(live.contest.rest_ends_at)}`
    : status === 'published' && live.contest.starts_at
      ? `计划 ${fmtTime(live.contest.starts_at)} 开赛`
      : status === 'finished' && live.contest.ends_at
        ? `${fmtTime(live.contest.ends_at)} 结束`
        : null
  const resultsPending = status === 'finished' && !live.contest.official_results_ready

  return (
    <PageFrame width="wide" layout="contest-live">
      <PageHeader
        eyebrow={(
          <span className="inline-flex items-center gap-2">
            <Radio aria-hidden="true" className="size-3.5 text-primary" />
            赛事直播
            <StatusBadge status={status} />
            {immutableSnapshot && <Badge variant="outline" className="text-xs">演示快照</Badge>}
          </span>
        )}
        title={live.contest.title}
        description={(
          <span className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
            <span>{live.stage?.label || '等待赛程'} · {gameLabel(live.contest.game_id)}</span>
            {seriesText && <span>{seriesText}</span>}
            {scheduleText && <span className="inline-flex items-center gap-1"><CalendarClock aria-hidden="true" className="size-3.5" />{scheduleText}</span>}
            {resultsPending && <span className="font-medium text-primary">成绩正在整理，正式名次稍后公布</span>}
          </span>
        )}
        actions={(
          <Button asChild variant="outline" className="min-h-11 w-full sm:w-auto">
            <Link to={`/contests/${contestId}`}><ArrowLeft aria-hidden="true" className="size-4" />返回赛事详情</Link>
          </Button>
        )}
      />
      {error && !offline && <ErrorMsg msg={`${error}；已保留上一次赛况。`} />}
      <LiveContestSpectator
        status={status}
        stageLabel={live.stage?.label || ''}
        duplicate={live.series.duplicate}
        snapshot={immutableSnapshot}
        progress={live.progress}
        activePairings={live.active}
        upcomingPairings={live.upcoming}
        recentPairings={live.recent}
        standings={live.standings}
        lastUpdatedAt={updatedAt}
        polling={polling}
        offline={offline}
        refreshEnabled={!terminal && !immutableSnapshot && pollInterval != null}
        onRefresh={refresh}
      />
    </PageFrame>
  )
}
