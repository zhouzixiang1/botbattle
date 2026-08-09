/**
 * 统一对局页（实时观赛 + 历史回放合一）。路由 /match/:id 唯一页。
 *
 * - running/pending → 直播模式：开 SSE，定位到最新后按回放速度推进（DVR 模型），
 *   Bot 瞬间连走则游标落后、显示「落后 N 个事件」、可「跳到最新」；match_end 到达后
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

const REASON_LABELS: Record<string, string> = {
  completed: '正常结束',
  protocol_error: '协议错误',
  technical_loss: 'Bot 技术判负',
  timeout: '决策超时',
  crash: 'Bot 运行异常',
  invalid_action: '非法动作',
  version_unavailable: 'Bot 版本不可用',
  platform_error: '平台运行异常',
  admin_aborted: '管理员中止',
}

function reasonLabel(reason: unknown): string {
  const value = String(reason ?? '').trim()
  return REASON_LABELS[value] ?? (value.replaceAll('_', ' ') || '未知原因')
}

function matchHasTechnicalLoss(match: MatchRow | null | undefined): boolean {
  if (!match) return false
  if (Number(match.technical_loss || 0) > 0) return true
  if ((match.result?.technical_incident_samples?.length ?? 0) > 0) return true
  if (Object.values(match.result?.technical_incidents_by_seat ?? {}).some((n) => Number(n) > 0)) return true
  return ['protocol_error', 'technical_loss', 'timeout', 'crash', 'invalid_action', 'version_unavailable']
    .includes(String(match.reason ?? ''))
}

function describeTimelineEvent(event: RawEvent, gameDescription: string): string {
  if (event.type === 'technical_incident') {
    const seat = Number(event.seat)
    const seatText = Number.isFinite(seat) ? `座位 ${seat + 1}` : 'Bot'
    const turn = Number(event.turn)
    const turnText = Number.isFinite(turn) && turn > 0 ? ` · 第 ${turn} 次决策` : ''
    return `${seatText} 技术故障${turnText}：${String(event.error || reasonLabel(event.reason))}`
  }
  if (event.type === 'match_end' && event.reason && event.reason !== 'completed') {
    return `对局结束 · ${reasonLabel(event.reason)}`
  }
  return gameDescription
}

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
          const pinTechnicalTerminal =
            matchHasTechnicalLoss(m) && Number(m.result?.hands_played ?? 0) <= 0
          setStatus('replay')
          setStepIdx(evs.length > 0 ? (pinTechnicalTerminal ? evs.length - 1 : 0) : -1)
          setPlaying(evs.length > 0 && !pinTechnicalTerminal)
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
  const realtime = Boolean(isLiveMatch && (status === 'live' || status === 'connecting'))
  // 游标差是事件数，不是游戏手数/步数；终态回放永远不显示「落后」。
  const lag = !gameSpec || atLive || !realtime ? 0 : Math.max(0, total - 1 - cur)
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
  const progressUnitLabel = gameSpec?.progressUnit === 'move' ? '步' : '手'
  const persistedProgress = Number(match?.result?.hands_played)
  const displayedProgress = finished
    ? (Number.isFinite(persistedProgress) ? persistedProgress : null)
    : liveProgress
  const progressText = displayedProgress != null && displayedProgress > 0
    ? `${finished ? '共' : '当前'} ${displayedProgress} ${progressUnitLabel}`
    : null
  const ReplayHud = gameSpec?.replay.Hud
  const navigation = gameSpec?.replay.navigation
  const typeBadge = matchTypeBadge(match?.match_type)
  const persistedIncidents = match?.result?.technical_incident_samples ?? []
  const eventIncidents = events
    .filter((event) => event.type === 'technical_incident')
    .map((event) => ({
      seat: Number(event.seat),
      error: String(event.error || reasonLabel(event.reason)),
      code: event.code == null ? undefined : String(event.code),
      reason: event.reason == null ? undefined : String(event.reason),
      turn: event.turn == null ? null : Number(event.turn),
    }))
  const technicalIncidents = persistedIncidents.length ? persistedIncidents : eventIncidents
  const technicalTerminal = finished && (matchHasTechnicalLoss(match) || technicalIncidents.length > 0)
  const staticTechnicalReplay = technicalTerminal && (!Number.isFinite(persistedProgress) || persistedProgress <= 0)
  const failedSeat = (() => {
    const sampleSeat = Number(technicalIncidents[0]?.seat)
    if (sampleSeat === 0 || sampleSeat === 1) return sampleSeat
    const counts = match?.result?.technical_incidents_by_seat ?? {}
    return ([0, 1] as const).find((seat) => Number(counts[seat] ?? 0) > 0)
  })()

  // 首个决策即技术终止时直接呈现权威终局。自动从头播放会让“已完成”页面
  // 暂时停在发牌事件，看起来像仍在正常比赛。
  useEffect(() => {
    if (!staticTechnicalReplay || total <= 0) return
    setStepIdx(total - 1)
    setPlaying(false)
  }, [staticTechnicalReplay, total])

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
  const winnerSeat = match?.winner === 0 || match?.winner === 1
    ? match.winner
    : eventWinner === 0 || eventWinner === 1
      ? eventWinner
      : null
  const renderSeat = (seat: 0 | 1) => {
    if (!match) return null
    const info = seats?.[seat]
    const botId = seat === 0 ? match.bot_a_id : match.bot_b_id
    const name = info?.botName || seatHeaderLabel(match, seat)
    const isWinner = winnerSeat === seat
    return (
      <div className={`min-w-0 rounded-lg border px-3 py-3 ${seat === 0 ? 'order-1' : 'order-2 sm:order-3'} ${isWinner ? 'border-primary/40 bg-primary/5' : 'border-border bg-muted/20'}`}>
        <div className="mb-1 flex items-center gap-2 text-xs text-muted-foreground">
          <span>座位 {seat + 1}</span>
          {gameSpec?.seatColors?.[seat] && <span>· {gameSpec.seatColors[seat]}</span>}
          {isWinner && <Badge className="ml-auto">胜</Badge>}
        </div>
        {botId != null && !info?.isHuman ? (
          <Link to={`/bot/${botId}`} className="block break-words font-semibold text-foreground hover:text-primary">
            {name}
          </Link>
        ) : (
          <div className="break-words font-semibold text-foreground">{name}</div>
        )}
        {info?.ownerName && !info.isHuman && (
          <div className="mt-0.5 break-all text-xs text-muted-foreground">@{info.ownerName}</div>
        )}
      </div>
    )
  }

  return (
    <PageStub title={isLive ? '实时观赛' : '对局详情'}>
      <div className="mb-3 flex flex-wrap items-center gap-2 text-sm">
        <span className="max-w-full break-all font-mono text-xs text-muted-foreground">{id}</span>
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
        {progressText && <Badge variant="outline">{progressText}</Badge>}
        {lag > 0 && (
          <Button variant="outline" size="sm" onClick={jumpToLive} className="gap-1">
            <Radio className="size-3" />落后 {lag} 个事件 · 跳到最新
          </Button>
        )}
      </div>

      {/* 对阵与结果形成一个稳定层级；身份不再同时散落于标题、摘要和详情链接。 */}
      {match && (
        <Card className="mb-4">
          <CardContent className="grid grid-cols-2 gap-3 py-4 sm:grid-cols-[minmax(0,1fr)_minmax(9rem,auto)_minmax(0,1fr)] sm:items-center">
            {renderSeat(0)}
            <div className="order-3 col-span-2 min-w-0 border-t border-border pt-3 text-center sm:order-2 sm:col-span-1 sm:border-x sm:border-t-0 sm:px-4 sm:py-1">
              <div className="text-[10px] font-medium uppercase tracking-[0.16em] text-muted-foreground">
                {finished ? '对局结果' : '当前状态'}
              </div>
              <div className="mt-1 break-words text-sm font-semibold text-foreground">
                {finished ? winnerLabel : '对局进行中'}
              </div>
              {match.reason && (
                <div className={`mt-1 text-xs ${match.reason === 'completed' ? 'text-muted-foreground' : 'text-destructive'}`}>
                  {reasonLabel(match.reason)}
                </div>
              )}
            </div>
            {renderSeat(1)}
          </CardContent>
        </Card>
      )}

      {match && technicalTerminal && (
        <Card role="alert" className="mb-4 border-destructive/35 bg-destructive/5">
          <CardContent className="py-4">
            <div className="flex items-start gap-3">
              <TriangleAlert className="mt-0.5 size-5 shrink-0 text-destructive" />
              <div className="min-w-0 flex-1">
                <div className="font-semibold text-foreground">Bot 技术判负</div>
                <p className="mt-1 break-words text-sm text-muted-foreground">
                  {failedSeat === 0 || failedSeat === 1
                    ? `${seatHeaderLabel(match, failedSeat)}（座位 ${failedSeat + 1}）发生技术故障`
                    : '对局因 Bot 技术故障终止'}
                  {winnerSeat === 0 || winnerSeat === 1
                    ? `，${seatHeaderLabel(match, winnerSeat)}（座位 ${winnerSeat + 1}）获胜。`
                    : '。'}
                </p>
                {technicalIncidents.length > 0 ? (
                  <div className="mt-3 space-y-2">
                    {technicalIncidents.slice(0, 3).map((incident, index) => (
                      <div key={`${incident.seat}-${incident.turn ?? 'x'}-${index}`} className="rounded-md border border-destructive/20 bg-background/70 px-3 py-2 text-xs">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="font-medium text-foreground">
                            座位 {Number(incident.seat) + 1}
                            {incident.turn != null ? ` · 第 ${incident.turn} 次决策` : ''}
                          </span>
                          {incident.code && <Badge variant="outline" className="max-w-full break-all font-mono text-[10px]">{incident.code}</Badge>}
                        </div>
                        <p className="mt-1 break-words text-muted-foreground">{incident.error}</p>
                      </div>
                    ))}
                  </div>
                ) : (
                  <p className="mt-2 text-xs text-muted-foreground">故障类型：{reasonLabel(match.reason)}</p>
                )}
              </div>
            </div>
          </CardContent>
        </Card>
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

            {navigation && bounds.length >= 2 && !staticTechnicalReplay && (
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

            {/* 技术终止且没有完成一手/一步时，直接定位终局，不展示伪装成正常赛程的播放控制。 */}
            {!staticTechnicalReplay && (
              <Card>
                <CardContent className="py-3">
                  <div className="flex flex-wrap items-center justify-center gap-1.5">
                    {navigation && (
                      <Button variant="outline" size="sm" onClick={() => jumpSegment(-1)} className="gap-1"><SkipBack className="size-3.5" />上一{navigation.unitLabel}</Button>
                    )}
                    <Button variant="outline" size="sm" onClick={() => step(-1)} className="gap-1"><ChevronLeft className="size-4" />上一个事件</Button>
                    <Button variant="default" size="sm" onClick={togglePlay} className="gap-1.5">
                      {playing ? <><Pause className="size-4" />暂停</> : <><Play className="size-4" />播放</>}
                    </Button>
                    <Button variant="outline" size="sm" onClick={() => step(1)} className="gap-1">下一个事件<ChevronRight className="size-4" /></Button>
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
                    <span className="shrink-0 font-mono text-[10px] text-muted-foreground">事件 {cur + 1}/{total}{atLive ? ' · 直播' : ''}</span>
                    <Slider min={0} max={Math.max(0, total - 1)} value={[cur]} onValueChange={(v) => seek(v[0])} className="flex-1" />
                  </div>
                </CardContent>
              </Card>
            )}
          </div>

          {/* 右：动作时序 */}
          <Card className="flex flex-col self-start">
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
                    <span className="min-w-0 flex-1 break-words opacity-80">
                      {describeTimelineEvent(ev, gameSpec?.describeEvent(ev) ?? String(ev.type || '?'))}
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
