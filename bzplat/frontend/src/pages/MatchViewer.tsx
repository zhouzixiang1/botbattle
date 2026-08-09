/**
 * 统一对局页（实时观赛 + 历史回放合一）。路由 /match/:id 唯一页。
 *
 * - running/pending → 直播模式：开 SSE，定位到最新后按回放速度推进（DVR 模型），
 *   Bot 瞬间连走则游标落后、显示「落后 N 手」、可「跳到最新」；match_end 到达后
 *   游标走完剩余停（不强制跳结局）；已结束对局重开页 → 自动从头播放。
 * - completed/aborted → 回放模式：一次性加载 events_json，从头自动播放。
 * - 座位身份：从 match.bot_a/bot_b（后端 JOIN）构造 SeatInfo 传 canvas。
 * - 合并旧 MatchDetail（回放）逻辑；ArenaWatch 已删除，/watch 旧路径不再重定向。
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useParams, useNavigate } from 'react-router-dom'
import { Play, Pause, ChevronLeft, ChevronRight, SkipBack, SkipForward, Radio, ArrowLeft, History, TriangleAlert } from 'lucide-react'
import PageStub from '@/components/PageStub'
import MatchBoard from '@/components/MatchBoard'
import { Card, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Slider } from '@/components/ui/slider'
import { ErrorMsg, Loading, EmptyState } from '@/components/ui/status'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { apiGet, apiPost, errMsg } from '@/api'
import { gameLabel, gameIcon, normalizeGameId, matchTypeBadge } from '@/lib/games'
import Comments from '@/components/Comments'
import { SPEEDS } from '@/components/use-playback'
import { findGame, unsupportedGameLabel } from '@/games'
import type { RawEvent } from '@/games/base'
import {
  type MatchSeatRow,
  seatInfos,
  seatHeaderLabel,
  resolveWinnerLabel,
} from '@/lib/match-seats'

type MatchRow = MatchSeatRow & { game_id?: string; status?: string; reason?: string }

const STATUS_VARIANT: Record<string, 'default' | 'secondary' | 'destructive' | 'outline'> = {
  connecting: 'secondary', live: 'default', match_end: 'outline', error: 'destructive',
  completed: 'default', aborted: 'destructive', running: 'default', pending: 'secondary',
}

export default function MatchViewer() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const [match, setMatch] = useState<MatchRow | null>(null)
  const [events, setEvents] = useState<RawEvent[]>([])
  const [status, setStatus] = useState<string>('connecting')  // connecting|live|match_end|error|replay
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  // 播放游标（-1 = 贴尾/未启动；否则 0-based）
  const [stepIdx, setStepIdx] = useState(-1)
  const [playing, setPlaying] = useState(false)
  const [speedIdx, setSpeedIdx] = useState(1)
  // 时序面板折叠态（窄屏默认折叠，棋盘获全宽）
  const [timelineCollapsed, setTimelineCollapsed] = useState(false)
  const logRef = useRef<HTMLDivElement>(null)
  const curActionRef = useRef<HTMLDivElement>(null)
  // events 最新长度的 ref——SSE 回调里读「当前长度」算 match_end 贴尾游标，
  // 避免在 setEvents updater 内部触发 setStepIdx（React updater 须为纯函数；
  // 审计 P1-D 反模式修复）。
  const eventsLenRef = useRef(0)
  // Bot 对局可能在一个渲染帧内产生数百条 SSE 消息。逐条 setEvents 会触发
  // React 的 nested-update 保护；先入队、每帧合并一次，match_end 到达时同步冲刷。
  const pendingEventsRef = useRef<RawEvent[]>([])
  const flushFrameRef = useRef<number | null>(null)

  // 直播 SSE / 回放加载（一次性探测状态，决定模式）
  const isLiveMatch = match?.status === 'running' || match?.status === 'pending'
  useEffect(() => {
    if (!id) return
    setLoading(true)
    setError('')
    eventsLenRef.current = 0
    setEvents([])
    setStepIdx(-1)
    setPlaying(false)
    let cancelled = false
    let terminalClosed = false
    let es: EventSource | null = null

    const cancelFlush = () => {
      if (flushFrameRef.current !== null) cancelAnimationFrame(flushFrameRef.current)
      flushFrameRef.current = null
    }
    const takePending = () => {
      cancelFlush()
      const batch = pendingEventsRef.current
      pendingEventsRef.current = []
      return batch
    }
    const flushPending = () => {
      flushFrameRef.current = null
      if (cancelled) return
      const batch = pendingEventsRef.current
      pendingEventsRef.current = []
      if (!batch.length) return
      eventsLenRef.current += batch.length
      setEvents((prev) => [...prev, ...batch])
    }
    const queueEvent = (ev: RawEvent) => {
      pendingEventsRef.current.push(ev)
      if (flushFrameRef.current === null) {
        flushFrameRef.current = requestAnimationFrame(flushPending)
      }
    }

    // 浏览计数（公开，失败忽略）
    void apiPost(`/api/matches/${encodeURIComponent(id)}/view`, 'POST', {}).catch(() => undefined)

    void apiGet<{ match: MatchRow; replay: { events_json?: string } }>(`/api/matches/${encodeURIComponent(id)}`)
      .then((d) => {
        if (cancelled) return
        setMatch(d.match)
        const m = d.match
        const evs: RawEvent[] = (() => { try { return JSON.parse(d.replay?.events_json || '[]') as RawEvent[] } catch { return [] } })()
        eventsLenRef.current = evs.length
        setEvents(evs)
        const live = m.status === 'running' || m.status === 'pending'
        if (live) {
          setStatus('live'); setStepIdx(-1); setPlaying(true)
          es = new EventSource(`/api/matches/${encodeURIComponent(id)}/events`)
          es.onmessage = (msg) => {
            try {
              const ev = JSON.parse(msg.data) as RawEvent
              if (ev.type === 'snapshot') {
                takePending()
                const snapshotMatch = ev.match as MatchRow | undefined
                if (snapshotMatch) setMatch(snapshotMatch)
                const hist = Array.isArray(ev.events) ? (ev.events as RawEvent[]) : []
                const sliced = hist.slice(-4000)
                eventsLenRef.current = sliced.length
                setEvents(sliced)
                const terminal = snapshotMatch?.status === 'completed' || snapshotMatch?.status === 'aborted'
                if (terminal) {
                  // 初始详情仍是 live、订阅瞬间已结束：snapshot 是唯一终态信号。
                  // 切换到普通回放并主动关闭 EventSource，避免浏览器自动重连。
                  setStatus('replay')
                  setStepIdx(sliced.length > 0 ? 0 : -1)
                  setPlaying(sliced.length > 0)
                  terminalClosed = true
                  es?.close()
                } else {
                  setStatus('live')
                  setStepIdx(-1); setPlaying(true)
                }
              } else if (ev.type === 'match_end' || ev.type === 'error') {
                // match_end/error 时游标停当前位置（不跳尾）：
                // 在追加该事件前，把游标钉在当时看到的最后一条；停止自动播放。
                // 否则 stepIdx=-1(贴尾) 会因 match_end 入列 total+1 而跳到尾部结局。
                // 用 ref 读「追加前长度」算游标，避免在 setEvents updater 内部
                // 触发 setStepIdx（React updater 须为纯函数；审计 P1-D）。
                const queued = takePending()
                const beforeEnd = eventsLenRef.current + queued.length
                if (beforeEnd > 0) setStepIdx(beforeEnd - 1)
                eventsLenRef.current = beforeEnd + 1
                setEvents((prev) => [...prev, ...queued, ev])
                setPlaying(false)
                // 回写权威终态，避免顶栏一直停在直播中的旧快照。
                setMatch((prev) => {
                  if (!prev) return prev
                  const patch: MatchRow = {
                    ...prev,
                    status: ev.type === 'error' ? 'aborted' : 'completed',
                  }
                  if (ev.winner !== undefined) patch.winner = ev.winner as number | null
                  // live match_end 唯一结果字段是 canonical result.deltas；不再
                  // 接受已退役的双标量第二套合约。
                  if (Array.isArray(ev.deltas) && ev.deltas.length >= 2) {
                    patch.result = {
                      ...(prev.result || {}),
                      deltas: [
                        Number(ev.deltas[0]),
                        Number(ev.deltas[1]),
                      ],
                    }
                  }
                  const eventReason = ev.reason ?? ev.message
                  if (eventReason) patch.reason = String(eventReason)
                  return patch
                })
                setStatus(String(ev.type))
                terminalClosed = true
                es?.close()
              } else {
                // 常规事件（落子/判决等）：逐帧批量追加，避免瞬时对局触发深度更新告警。
                queueEvent(ev)
              }
            } catch { /* ignore */ }
          }
          es.onerror = () => {
            if (cancelled || terminalClosed) return
            // Native EventSource reconnects automatically after a transient
            // transport failure. Keep it alive and expose a connecting state;
            // the next authoritative snapshot restores `live` above.
            setStatus('connecting')
          }
        } else {
          setStatus('replay'); setStepIdx(evs.length > 0 ? 0 : -1); setPlaying(evs.length > 0)
        }
      })
      .catch((e) => { if (!cancelled) { setError(errMsg(e)); setStatus('error') } })
      .finally(() => { if (!cancelled) setLoading(false) })

    return () => {
      cancelled = true
      cancelFlush()
      pendingEventsRef.current = []
      es?.close()
    }
  }, [id])

  // 窄屏默认折叠时序面板（不影响桌面布局）
  useEffect(() => {
    if (window.innerWidth < 1280) setTimelineCollapsed(true)
  }, [])

  const gameId = normalizeGameId(match?.game_id)
  const gameSpec = match ? findGame(gameId) : undefined
  const total = events.length
  // cur：当前显示到第几步。-1 = 贴尾（直播跟随/回放启动前）
  const cur = stepIdx < 0 ? Math.max(0, total - 1) : Math.min(stepIdx, total - 1)
  const visible = total > 0 ? events.slice(0, cur + 1) : []
  const atLive = stepIdx < 0
  const lag = !gameSpec || atLive ? 0 : Math.max(0, total - 1 - cur)
  const seats = seatInfos(match)
  // 当前可见事件只由游戏 reducer 解释，页面不读取具体 ViewModel 字段。
  const visibleVm = useMemo(() => {
    if (total === 0 || !gameSpec) return null
    const slice = events.slice(0, cur + 1)
    return gameSpec.reduce(slice as RawEvent[])
  }, [gameSpec, events, cur, total])
  const finished =
    match?.status === 'completed' ||
    match?.status === 'aborted' ||
    status === 'match_end' ||
    status === 'replay'
  const eventWinner = visibleVm && gameSpec ? gameSpec.winner(visibleVm) : undefined
  const colorLabel = (seat: number) => {
    // 显示从 1 起计（后端 0 起计，DB CHECK 约束未变）。
    if (!match) return `座位 ${seat + 1}`
    // 棋类座位着色（黑白/红蓝）经 spec.seatColors 取（消除游戏名分支）
    const seatColors = gameSpec?.seatColors
    if (seatColors && seatColors[seat]) {
      return `${seatHeaderLabel(match, seat as 0 | 1)}（${seatColors[seat]}）`
    }
    return seatHeaderLabel(match, seat as 0 | 1)
  }
  const winnerLabel = resolveWinnerLabel(match, eventWinner, finished, colorLabel)
  const liveProgress = visibleVm && gameSpec ? gameSpec.replay.progress(visibleVm) : null
  const progressLabel = gameSpec ? (gameSpec.progressUnit === 'move' ? '步数' : '手数') : '进度'
  const progressUnitLabel = gameSpec?.progressUnit === 'move' ? '步' : '手'
  const ReplaySummary = gameSpec?.replay.Summary
  const ReplayHud = gameSpec?.replay.Hud
  const navigation = gameSpec?.replay.navigation
  const typeBadge = matchTypeBadge(match?.match_type)

  // 定速播放定时器（直播+回放共用）：按 SPEEDS 步进；到末尾后直播继续等（保持 playing），
  // 回放则停。直播时游标追上末尾 → 转贴尾(-1)，新事件来了继续推进。
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => {
    if (!playing || total === 0) return
    if (cur >= total - 1) {
      if (!isLiveMatch && status !== 'live') {
        // 回放到头：停
        setPlaying(false)
      } else {
        // 直播追上末尾：转贴尾等新事件（保持 playing，不步进）
        setStepIdx(-1)
      }
      return
    }
    timerRef.current = setTimeout(() => {
      setStepIdx((s) => {
        const next = (s < 0 ? total - 1 : s) + 1
        return next >= total - 1 ? -1 : next
      })
    }, SPEEDS[speedIdx].ms)
    return () => { if (timerRef.current) clearTimeout(timerRef.current) }
  }, [playing, cur, total, speedIdx, isLiveMatch, status])

  const pause = () => setPlaying(false)
  const step = (delta: number) => {
    setPlaying(false)
    setStepIdx((s) => {
      const base = s < 0 ? Math.max(0, total - 1) : s
      return Math.max(0, Math.min(Math.max(0, total - 1), base + delta))
    })
  }
  const seek = (idx: number) => { setPlaying(false); setStepIdx(idx) }
  const jumpToLive = () => { setStepIdx(-1); setPlaying(true) }
  const togglePlay = () => {
    if (!playing && cur >= total - 1 && !atLive) setStepIdx(total > 1 ? 0 : -1)
    setPlaying((p) => !p)
  }

  // 可选的游戏分段导航（当前德州按手）；边界算法由游戏包提供。
  const bounds = useMemo(() => navigation?.boundaries(events) ?? [], [events, navigation])
  const jumpSegment = (delta: number) => {
    pause()
    if (!bounds.length) return
    let hIdx = 0
    for (let i = 0; i < bounds.length - 1; i++) if (cur >= bounds[i] && cur < bounds[i + 1]) { hIdx = i; break }
    const target = Math.max(0, Math.min(bounds.length - 2, hIdx + delta))
    setStepIdx(bounds[target] ?? 0)
  }
  const curSegmentIdx = (() => { for (let i = 0; i < bounds.length - 1; i++) if (cur >= bounds[i] && cur < bounds[i + 1]) return i; return bounds.length >= 2 ? bounds.length - 2 : 0 })()

  // 动作日志自动滚动到当前步
  useEffect(() => {
    const c = logRef.current, row = curActionRef.current
    if (!c || !row) return
    const cTop = c.scrollTop, cBottom = cTop + c.clientHeight
    const rTop = row.offsetTop, rBottom = rTop + row.offsetHeight
    if (rTop < cTop) c.scrollTop = rTop
    else if (rBottom > cBottom) c.scrollTop = rBottom - c.clientHeight
  }, [cur])

  const GameIcon = gameIcon(gameId)
  const isLive = status === 'live' || isLiveMatch

  return (
    <PageStub title={isLive ? '实时观赛' : '对局详情'}>
      <div className="mb-4 flex flex-wrap items-center gap-2 text-sm">
        <span className="font-mono text-xs text-muted-foreground">{id}</span>
        {match && (
          <Badge variant="secondary" className="gap-1"><GameIcon className="size-3" />{gameLabel(gameId)}</Badge>
        )}
        {typeBadge && (
          <Badge variant="outline" className={`text-[10px] ${typeBadge.cls}`}>{typeBadge.label}</Badge>
        )}
        {/* 状态徽标：优先用 DB 权威字段 match.status（completed/aborted/running/pending），
            回退到本地连接态（connecting/live/match_end/error/replay）。
            原仅读本地 status 导致已完成的对局刷新后显示「回放」而非「已完成」。 */}
        {(() => {
          const dbStatus = match?.status  // 'completed'|'aborted'|'running'|'pending'（权威）
          const showLive = status === 'live'
          const label = showLive ? '直播中'
            : dbStatus === 'completed' ? '已完成'
            : dbStatus === 'aborted' ? '已中止'
            : status === 'match_end' ? '已结束'
            : status === 'error' ? '出错'
            : status === 'connecting' ? '连接中'
            : '回放'
          const variant = showLive ? STATUS_VARIANT['live']
            : dbStatus ? (STATUS_VARIANT[dbStatus] ?? 'secondary')
            : (STATUS_VARIANT[status] ?? 'secondary')
          return (
            <Badge variant={variant ?? 'secondary'} className="gap-1">
              {showLive && <span className="size-1.5 animate-pulse rounded-full bg-current" />}
              {label}
            </Badge>
          )
        })()}
        {match && (
          <span className="text-muted-foreground">
            {progressLabel}：
            <span className="font-mono text-foreground">
              {String(liveProgress ?? match.result?.hands_played ?? 0)}
            </span>
          </span>
        )}
        {match && (
          <span className="text-muted-foreground">
            胜者：
            <span className="font-medium text-foreground">{winnerLabel}</span>
          </span>
        )}
        {match?.reason && match.reason !== 'completed' && (
          <span className="inline-flex items-center gap-1 text-xs text-destructive">
            <TriangleAlert className="size-3" />
            原因：{match.reason}
          </span>
        )}
        {match?.result?.technical_incidents_by_seat && (
          <span className="inline-flex items-center gap-1 text-xs text-warning">
            <TriangleAlert className="size-3" />
            {Object.entries(match.result.technical_incidents_by_seat)
              .filter(([, n]) => Number(n) > 0)
              .map(([seat, n]) => `座位${Number(seat) + 1} ${n} 次技术故障`)
              .join('，') || ''}
          </span>
        )}
        {lag > 0 && (
          <Button variant="outline" size="sm" onClick={jumpToLive} className="gap-1">
            <Radio className="size-3" />落后 {lag} {progressUnitLabel}·跳最新
          </Button>
        )}
      </div>

      {/* 对阵双方；游戏专属摘要经 replay.Summary 插件挂载。 */}
      {match && (
        <div className="mb-4 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
          <span className="min-w-0 max-w-[40%] truncate font-medium text-foreground">{seatHeaderLabel(match, 0)}</span>
          <span className="text-muted-foreground">vs</span>
          <span className="min-w-0 max-w-[40%] truncate font-medium text-foreground">{seatHeaderLabel(match, 1)}</span>
          {ReplaySummary && visibleVm !== null && <ReplaySummary vm={visibleVm} seats={seats} />}
          {match.bot_a_id != null && match.bot_b_id != null && match.match_type !== 'human' && (
            <span className="text-xs text-muted-foreground">
              <Link to={`/bot/${match.bot_a_id}`} className="text-primary hover:underline">座1 详情</Link>
              {' · '}
              <Link to={`/bot/${match.bot_b_id}`} className="text-primary hover:underline">座2 详情</Link>
            </span>
          )}
        </div>
      )}

      {error && <ErrorMsg msg={error} className="mb-4" />}
      {match && !gameSpec && (
        <ErrorMsg msg={`无法显示该对局：${unsupportedGameLabel(match.game_id)}`} className="mb-4" />
      )}

      {loading ? (
        <Loading text="加载中…" />
      ) : match && !gameSpec ? (
        <Card><EmptyState
          text={`回放不可用：${unsupportedGameLabel(match.game_id)}`}
          icon={<TriangleAlert className="size-7 opacity-40" />}
        /></Card>
      ) : visible.length === 0 ? (
        <Card><EmptyState
          text={match?.status === 'aborted' ? '此对局已中止，无回放数据' : '暂无事件'}
          icon={<History className="size-7 opacity-40" />}
        /></Card>
      ) : (
        <div className={gameSpec?.replay.layout === 'wide' ? "space-y-4" : timelineCollapsed ? "grid gap-4" : "grid gap-4 xl:grid-cols-[minmax(0,1fr)_22rem]"}>
          {/* 左：canvas 棋盘/牌桌 + 手导航 + 控制条 */}
          <div className="space-y-3">
            {ReplayHud && visibleVm !== null && <ReplayHud vm={visibleVm} seats={seats} />}
            <MatchBoard gameId={gameId} events={visible} seats={seats} revealMode="all" />

            {navigation && bounds.length >= 2 && (
              <div>
                <div className="mb-1 text-[10px] text-muted-foreground">{navigation.unitLabel}导航（点击跳转）</div>
                <div className="flex max-h-24 flex-wrap gap-1 overflow-y-auto rounded-lg border border-border bg-card p-2">
                  {Array.from({ length: bounds.length - 1 }, (_, h) => (
                    <button key={h} type="button" onClick={() => seek(bounds[h] ?? 0)}
                      className={`size-7 shrink-0 rounded text-[10px] font-medium transition ${h === curSegmentIdx ? 'bg-primary text-primary-foreground' : 'bg-muted text-muted-foreground hover:bg-accent'}`}>
                      {h + 1}
                    </button>
                  ))}
                </div>
              </div>
            )}

            {/* 控制条 */}
            <Card>
              <CardContent className="py-3">
                <div className="flex flex-wrap items-center justify-center gap-1.5">
                  {navigation && (
                    <Button variant="outline" size="sm" onClick={() => jumpSegment(-1)} className="gap-1"><SkipBack className="size-3.5" />上一{navigation.unitLabel}</Button>
                  )}
                  <Button variant="outline" size="sm" onClick={() => step(-1)} className="gap-1"><ChevronLeft className="size-4" />上一步</Button>
                  <Button variant="default" size="sm" onClick={togglePlay} className="gap-1.5">
                    {playing ? <><Pause className="size-4" />暂停</> : <><Play className="size-4" />播放</>}
                  </Button>
                  <Button variant="outline" size="sm" onClick={() => step(1)} className="gap-1">下一步<ChevronRight className="size-4" /></Button>
                  {navigation && (
                    <Button variant="outline" size="sm" onClick={() => jumpSegment(1)} className="gap-1">下一{navigation.unitLabel}<SkipForward className="size-3.5" /></Button>
                  )}
                  <Select value={String(speedIdx)} onValueChange={(v) => setSpeedIdx(Number(v))}>
                    <SelectTrigger size="sm" className="h-8 w-[5rem] text-xs">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {SPEEDS.map((s, i) => (<SelectItem key={i} value={String(i)}>{s.label}</SelectItem>))}
                    </SelectContent>
                  </Select>
                </div>
                <div className="mt-3 flex items-center gap-3">
                  <span className="font-mono text-[10px] text-muted-foreground">步 {cur + 1}/{total}{atLive ? ' ·直播' : ''}</span>
                  <Slider min={0} max={Math.max(0, total - 1)} value={[cur]} onValueChange={(v) => seek(v[0])} className="flex-1" />
                </div>
              </CardContent>
            </Card>
          </div>

          {/* 右：动作时序 */}
          <Card className="flex flex-col">
            <div className="border-b border-border px-4 py-2">
              <div className="flex items-center justify-between">
                <span className="text-sm font-medium">
                  动作时序 <span className="text-xs font-normal text-muted-foreground">({visible.length})</span>
                </span>
                <Button variant="ghost" size="sm" onClick={() => setTimelineCollapsed(c => !c)}>
                  {timelineCollapsed ? '展开' : '折叠'}
                </Button>
              </div>
            </div>
            {!timelineCollapsed && (
              <div ref={logRef} className="max-h-[60vh] flex-1 overflow-y-auto p-2 text-xs">
                {visible.map((ev, i) => (
                  <div key={i} ref={i === cur ? curActionRef : undefined}
                    className={`flex items-center gap-2 rounded px-2 py-1 ${i === cur ? 'bg-primary/10 font-medium text-primary' : 'text-muted-foreground'}`}>
                    <span className="w-8 shrink-0 font-mono opacity-60">{i + 1}</span>
                    <span className="min-w-0 flex-1 break-words opacity-80" title={String(ev.type || '')}>
                      {gameSpec?.describeEvent(ev) ?? String(ev.type || '?')}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </Card>
        </div>
      )}

      <Button variant="ghost" size="sm" className="mt-6 gap-1.5" onClick={() => navigate(-1)}>
        <ArrowLeft className="size-4" />返回
      </Button>
      {id && <Comments targetType="match" targetId={id} />}
    </PageStub>
  )
}
