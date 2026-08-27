/**
 * 统一对局页（实时观赛 + 历史回放合一）。路由 /match/:id 唯一页。
 *
 * - running/pending → 直播模式：开 SSE，从事件 1 按回放速度推进（DVR 模型），
 *   新事件先进入缓冲；用户可返回实时画面或直接查看最终结果。match_end 到达后
 *   游标继续顺序补完（不强制跳结局）；已结束对局重开页同样自动从头播放。
 * - completed/aborted → 元数据先渲染，再按需加载结构化 replay events。
 * - 座位身份：从 match.bot_a/bot_b（后端 JOIN）构造 SeatInfo 传 canvas。
 * - 合并旧 MatchDetail（回放）逻辑；ArenaWatch 已删除，/watch 旧路径不再重定向。
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useParams, useNavigate } from 'react-router-dom'
import { Play, Pause, ChevronLeft, ChevronRight, SkipBack, SkipForward, Radio, ArrowLeft, History, TriangleAlert, Download } from 'lucide-react'
import PageStub from '@/components/PageStub'
import BotDebugPanel, { type BotDebugPayload } from '@/components/BotDebugPanel'
import MatchBoard from '@/components/MatchBoard'
import { MatchNatureBadge, MatchParticipantIdentity } from '@/components/MatchParticipants'
import { useAuth } from '@/components/useAuth'
import { Card, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Slider } from '@/components/ui/slider'
import { ErrorMsg, Loading, EmptyState } from '@/components/ui/status'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { apiFetch, apiGet, apiPost, errMsg } from '@/api'
import { gameLabel, gameIcon, normalizeGameId } from '@/lib/games'
import Comments from '@/components/Comments'
import { SPEEDS } from '@/components/use-playback'
import { findGame, resolveTerminalReason, unsupportedGameLabel } from '@/games'
import type { SeatInfo } from '@/games/canvas-types'
import { describePlatformEvent } from '@/games/reasons'
import { eventSeatSubject } from '@/games/seat-display'
import type { RawEvent } from '@/games/base'
import {
  type MatchSeatRow,
  seatInfos,
  seatHeaderLabel,
  resolveWinnerLabel,
} from '@/lib/match-seats'

type MatchRow = MatchSeatRow & {
  id?: string
  game_id?: string
  status?: string
  reason?: string
  can_view_debug?: boolean
  contest_id?: number | null
}

type ReplayPayload = {
  match_id: string
  events: RawEvent[]
  event_count: number
  updated_at?: string | null
}

function matchHasTechnicalLoss(match: MatchRow | null | undefined): boolean {
  if (!match) return false
  if (Number(match.technical_loss || 0) > 0) return true
  if ((match.result?.technical_incident_samples?.length ?? 0) > 0) return true
  if (Object.values(match.result?.technical_incidents_by_seat ?? {}).some((n) => Number(n) > 0)) return true
  return ['protocol_error', 'technical_loss', 'timeout', 'crash', 'version_unavailable']
    .includes(String(match.reason ?? ''))
}

const ACTION_CONTEXT_SIZE = 7

function describeTimelineEvent(
  event: RawEvent,
  gameDescription: string,
  seats?: SeatInfo[],
): string {
  if (event.type === 'technical_incident') {
    const subject = eventSeatSubject(seats, event.seat)
    const turn = Number(event.turn)
    const turnText = Number.isFinite(turn) && turn > 0 ? ` · 第 ${turn} 次决策` : ''
    const reason = resolveTerminalReason(event.reason, 'completed').label
    return `${subject} 技术故障${turnText}：${String(event.error || reason)}`
  }
  const platformDescription = describePlatformEvent(event)
  if (platformDescription) return platformDescription
  // match_end 的裁判原因属于游戏契约；不得在通用时间线覆盖 describeEvent。
  return gameDescription
}

const STATUS_VARIANT: Record<string, 'default' | 'secondary' | 'destructive' | 'outline'> = {
  connecting: 'secondary', live: 'default', match_end: 'outline', error: 'destructive',
  completed: 'default', aborted: 'destructive', running: 'default', pending: 'secondary',
}

const RATING_REASON_LABEL: Record<string, string> = {
  eligible: '计入平台排行榜',
  same_owner: '同所有者调试 · 不计平台排行榜',
  self_play: '自博弈调试 · 不计平台排行榜',
  human: '人机对局 · 不计平台排行榜',
  contest: '赛事积分 · 不计平台排行榜',
  bot_missing: '历史 Bot 缺失 · 不计平台排行榜',
  owner_missing: '历史所有者缺失 · 不计平台排行榜',
  remote_local: '本地 Bot 练习 · 不计平台排行榜',
  ranked_bot_not_selected: '未派遣排行榜 Bot · 不计平台排行榜',
}

function ratingBadge(match: MatchRow): {
  label: string
  variant: 'default' | 'secondary' | 'destructive' | 'outline'
} | null {
  if (match.status === 'aborted') {
    return { label: '已中止未计分', variant: 'destructive' }
  }
  if (match.rated !== true) {
    if (match.rated !== false) return null
    return {
      label: RATING_REASON_LABEL[match.rating_reason || ''] || '不计平台排行榜',
      variant: 'secondary',
    }
  }
  if (match.status === 'completed') {
    return match.rating_settled === true
      ? { label: '已计分', variant: 'default' }
      : { label: '待结算', variant: 'secondary' }
  }
  return { label: '预计计分', variant: 'outline' }
}

export default function MatchViewer() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const { user } = useAuth()
  const [match, setMatch] = useState<MatchRow | null>(null)
  const [botDebug, setBotDebug] = useState<BotDebugPayload | null>(null)
  const [debugPermissionScope, setDebugPermissionScope] = useState<string | null>(null)
  const [events, setEvents] = useState<RawEvent[]>([])
  const [status, setStatus] = useState<string>('connecting')  // connecting|live|match_end|error|replay
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  // 游标始终是具体事件索引。初始化从事件 1 播放；即使已经追到实时尾部，
  // 后续事件也只增加缓冲长度，再由定时器逐条推进。
  const [cursor, setCursor] = useState(0)
  const [playing, setPlaying] = useState(false)
  const [speedIdx, setSpeedIdx] = useState(1)
  // 动作上下文折叠态（窄屏默认折叠，棋盘获全宽）
  const [timelineCollapsed, setTimelineCollapsed] = useState(false)
  // events 最新长度的 ref——SSE 回调需要在 React 提交前计算批量事件长度；
  // updater 保持纯函数，游标始终由独立的播放状态推进。
  const eventsLenRef = useRef(0)
  // Bot 对局可能在一个渲染帧内产生数百条 SSE 消息。逐条 setEvents 会触发
  // React 的 nested-update 保护；先入队、每帧合并一次，match_end 到达时同步冲刷。
  const pendingEventsRef = useRef<RawEvent[]>([])
  const flushFrameRef = useRef<number | null>(null)
  const debugPermissionRefreshRef = useRef<string | null>(null)
  const debugFetchedRef = useRef<string | null>(null)
  const debugLoadGenerationRef = useRef(0)

  // 直播 SSE / 回放加载（一次性探测状态，决定模式）
  const isLiveMatch = match?.status === 'running' || match?.status === 'pending'
  useEffect(() => {
    if (!id) return
    const controller = new AbortController()
    setLoading(true)
    setError('')
    setMatch(null)
    setStatus('connecting')
    eventsLenRef.current = 0
    pendingEventsRef.current = []
    setEvents([])
    setCursor(0)
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
    const refreshTerminalMatch = () => {
      void apiGet<{ match: MatchRow }>(`/api/matches/${encodeURIComponent(id)}`)
        .then((detail) => {
          if (cancelled) return
          if (detail.match.status === 'completed' || detail.match.status === 'aborted') {
            setMatch(detail.match)
          }
        })
        .catch(() => undefined)
    }

    // 浏览计数（公开，失败忽略）
    void apiPost(`/api/matches/${encodeURIComponent(id)}/view`, 'POST', {}).catch(() => undefined)

    void apiFetch<{ match: MatchRow }>(`/api/matches/${encodeURIComponent(id)}`, {
      method: 'GET',
      signal: controller.signal,
    })
      .then(async (d) => {
        if (cancelled) return
        setMatch(d.match)
        const m = d.match
        if (!findGame(normalizeGameId(m.game_id))) {
          // Unknown/future games have no reducer contract. Metadata is enough
          // to render the explicit unsupported state; never download a replay
          // that this client cannot safely interpret.
          setStatus('error')
          setLoading(false)
          return
        }
        const live = m.status === 'running' || m.status === 'pending'
        if (live) {
          setStatus('live'); setCursor(0); setPlaying(true)
          es = new EventSource(`/api/matches/${encodeURIComponent(id)}/events`)
          es.onmessage = (msg) => {
            if (cancelled) return
            try {
              const ev = JSON.parse(msg.data) as RawEvent
              if (ev.type === 'snapshot') {
                const queued = takePending()
                const snapshotMatch = ev.match as MatchRow | undefined
                if (snapshotMatch) setMatch(snapshotMatch)
                const hist = Array.isArray(ev.events) ? (ev.events as RawEvent[]) : []
                const localLength = eventsLenRef.current + queued.length
                // 活动 snapshot 提供完整公开前缀。不能只保留后 4000 条：一旦
                // 本地已有 >4000 条，增长后的重连快照会因裁剪后更短而被忽略，
                // 既丢新增事件也让事件 1 永久消失。仍防御旧代理返回较短快照。
                eventsLenRef.current = Math.max(localLength, hist.length)
                setEvents((prev) => {
                  const local = queued.length ? [...prev, ...queued] : prev
                  return hist.length >= local.length ? hist : local
                })
                setLoading(false)
                const terminal = snapshotMatch?.status === 'completed' || snapshotMatch?.status === 'aborted'
                if (terminal) {
                  // 初始详情仍是 live、订阅瞬间已结束：snapshot 是唯一终态信号。
                  // 切换到回放并主动关闭 EventSource；保留当前游标与播放态，
                  // 让缓冲自然排空，不强制跳到终局。
                  setStatus('replay')
                  terminalClosed = true
                  es?.close()
                  refreshTerminalMatch()
                } else {
                  setStatus('live')
                }
              } else if (ev.type === 'match_end' || ev.type === 'error') {
                // 终局只追加权威事件并更新状态。显式游标和播放态保持不变；
                // 已暂停就继续暂停，正在补播则按当前速度自然消费剩余缓冲。
                const queued = takePending()
                const beforeEnd = eventsLenRef.current + queued.length
                eventsLenRef.current = beforeEnd + 1
                setEvents((prev) => [...prev, ...queued, ev])
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
                  const eventReason = ev.reason ?? (ev.type === 'error' ? 'platform_error' : undefined)
                  if (eventReason) patch.reason = String(eventReason)
                  return patch
                })
                setStatus(String(ev.type))
                setLoading(false)
                terminalClosed = true
                es?.close()
                refreshTerminalMatch()
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
            // A first-frame failure must not leave the whole page in a
            // permanent spinner. Metadata stays visible while EventSource
            // retries and a later snapshot can still populate the replay.
            setLoading(false)
          }
        } else {
          const replay = await apiFetch<ReplayPayload>(
            `/api/matches/${encodeURIComponent(id)}/replay`,
            { method: 'GET', signal: controller.signal },
          )
          if (cancelled) return
          const evs = Array.isArray(replay.events) ? replay.events : []
          eventsLenRef.current = evs.length
          setEvents(evs)
          const pinTechnicalTerminal =
            matchHasTechnicalLoss(m) && Number(m.result?.rounds_played ?? 0) <= 0
          setStatus('replay')
          setCursor(evs.length > 0 ? (pinTechnicalTerminal ? evs.length - 1 : 0) : 0)
          setPlaying(evs.length > 0 && !pinTechnicalTerminal)
          setLoading(false)
        }
      })
      .catch((e) => {
        if (!cancelled && !(e instanceof DOMException && e.name === 'AbortError')) {
          setError(errMsg(e))
          setStatus('error')
          setLoading(false)
        }
      })

    return () => {
      cancelled = true
      controller.abort()
      cancelFlush()
      pendingEventsRef.current = []
      es?.close()
    }
  }, [id])

  // 私有 debug 只在终态且详情明确授予当前身份时读取。
  // 每个 match+user 权限域必须独立刷新详情；路由或账号切换会通过
  // generation 废弃旧响应，防止上一局/上一身份的私有内容短暂渲染。
  useEffect(() => {
    const generation = ++debugLoadGenerationRef.current
    const current = () => debugLoadGenerationRef.current === generation
    if (!id || !user) {
      setBotDebug(null)
      setDebugPermissionScope(null)
      debugPermissionRefreshRef.current = null
      debugFetchedRef.current = null
      return
    }
    if (match?.id !== id) {
      setBotDebug(null)
      setDebugPermissionScope(null)
      debugPermissionRefreshRef.current = null
      debugFetchedRef.current = null
      return
    }
    const terminal = match?.status === 'completed' || match?.status === 'aborted'
    if (!terminal) {
      setBotDebug(null)
      setDebugPermissionScope(null)
      debugPermissionRefreshRef.current = null
      debugFetchedRef.current = null
      return
    }

    const permissionKey = `${id}:${user.id}`
    if (debugPermissionScope !== permissionKey) {
      if (debugPermissionRefreshRef.current !== permissionKey) {
        debugPermissionRefreshRef.current = permissionKey
        debugFetchedRef.current = null
        setBotDebug(null)
        void apiGet<{ match: MatchRow }>(`/api/matches/${encodeURIComponent(id)}`)
          .then((detail) => {
            if (current() && detail.match.id === id) {
              setMatch(detail.match)
              setDebugPermissionScope(permissionKey)
            }
          })
          .catch(() => {
            if (current()) debugPermissionRefreshRef.current = null
          })
      }
      return
    }

    if (!match?.can_view_debug) {
      setBotDebug(null)
      return
    }
    const fetchKey = permissionKey
    if (debugFetchedRef.current === fetchKey) return
    debugFetchedRef.current = fetchKey
    void apiGet<BotDebugPayload>(`/api/matches/${encodeURIComponent(id)}/debug`)
      .then((payload) => {
        if (current() && payload.match_id === id) setBotDebug(payload)
      })
      .catch(() => {
        if (current()) setBotDebug(null)
      })
  }, [id, user?.id, match?.id, match?.status, match?.can_view_debug, debugPermissionScope])

  // 窄屏默认折叠动作上下文，并在旋转/调整窗口跨过 xl 断点时同步。
  // 同一布局内的手动折叠选择不会被 resize 覆盖。
  useEffect(() => {
    const media = window.matchMedia('(max-width: 1279px)')
    const syncBreakpoint = () => setTimelineCollapsed(media.matches)
    syncBreakpoint()
    media.addEventListener('change', syncBreakpoint)
    return () => media.removeEventListener('change', syncBreakpoint)
  }, [])

  const gameId = normalizeGameId(match?.game_id)
  const gameSpec = match ? findGame(gameId) : undefined
  const total = events.length
  // 始终使用数值游标。到达当前直播尾部后，新批次只增加 total，不能把
  // 画面粘到新尾部；下一次定时 tick 才推进一个事件。
  const cur = Math.min(cursor, Math.max(0, total - 1))
  const visible = total > 0 ? events.slice(0, cur + 1) : []
  const atLive = total > 0 && cur >= total - 1
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
  const fullVm = useMemo(() => {
    if (total === 0 || !gameSpec) return null
    return gameSpec.reduce(events as RawEvent[])
  }, [gameSpec, events, total])
  const finished =
    match?.status === 'completed' ||
    match?.status === 'aborted' ||
    status === 'match_end' ||
    status === 'replay'
  const terminalBacklog = finished && total > 0
    ? Math.max(0, total - 1 - cur)
    : 0
  const eventWinner = visibleVm && gameSpec ? gameSpec.winner(visibleVm) : undefined
  const visibleSeatDetail = (seat: number) => (
    gameSpec?.seatDetail?.(visibleVm, seat) ?? gameSpec?.seatColors?.[seat]
  )
  const colorLabel = (seat: number) => {
    // 显示从 1 起计（后端 0 起计，DB CHECK 约束未变）。
    if (!match) return `座位 ${seat + 1}`
    // 动态棋色（如五子棋交换）经 seatDetail 取；固定棋色回退 seatColors。
    const detail = visibleSeatDetail(seat)
    return detail
      ? `${seatHeaderLabel(match, seat as 0 | 1)}（${detail}）`
      : seatHeaderLabel(match, seat as 0 | 1)
  }
  const winnerLabel = resolveWinnerLabel(match, eventWinner, finished, colorLabel)
  const visibleProgress = visibleVm && gameSpec ? gameSpec.replay.progress(visibleVm) : null
  const visibleProgressTotal = visibleVm && gameSpec?.replay.progressTotal
    ? gameSpec.replay.progressTotal(visibleVm)
    : null
  const fullReplayProgress = fullVm && gameSpec ? gameSpec.replay.progress(fullVm) : null
  const progressUnitLabel = gameSpec?.progressUnit === 'move' ? '步' : '手'
  const persistedProgress = Number(match?.result?.rounds_played)
  // 声明总量的游戏展示当前画面的 X/总量；未声明总量的棋类终态仍显示
  // “共 N 步”。持久化字段用于缺少可归约事件时的权威兜底。
  const progressFromReplay = visibleProgressTotal != null && Number.isFinite(visibleProgressTotal)
    ? visibleProgress
    : finished
      ? fullReplayProgress
      : visibleProgress
  const displayedProgress = progressFromReplay != null && Number.isFinite(progressFromReplay)
    ? progressFromReplay
    : Number.isFinite(persistedProgress)
      ? persistedProgress
      : null
  const ReplaySummary = gameSpec?.replay.Summary
  const ReplayHud = gameSpec?.replay.Hud
  const navigation = gameSpec?.replay.navigation
  const viewportFitCanvas = gameSpec?.canvasFit === 'viewport'
  const viewportDashboard = viewportFitCanvas && Boolean(ReplayHud)
  const compactViewportDashboard = viewportDashboard && timelineCollapsed
  const ratingStateBadge = match ? ratingBadge(match) : null
  const terminalReason = gameSpec
    ? gameSpec.terminalReason(match?.reason, match?.status)
    : resolveTerminalReason(match?.reason, match?.status)
  const hasPersistedTerminalStatus =
    match?.status === 'completed' || match?.status === 'aborted'
  const persistedIncidents = match?.result?.technical_incident_samples ?? []
  const eventIncidents = events
    .filter((event) => event.type === 'technical_incident')
    .map((event) => ({
      seat: Number(event.seat),
      error: String(event.error || resolveTerminalReason(event.reason, 'completed').label),
      code: event.code == null ? undefined : String(event.code),
      reason: event.reason == null ? undefined : String(event.reason),
      turn: event.turn == null ? null : Number(event.turn),
    }))
  const technicalIncidents = persistedIncidents.length ? persistedIncidents : eventIncidents
  const technicalTerminal = finished && (matchHasTechnicalLoss(match) || technicalIncidents.length > 0)
  // 首决策德扑故障已经有 hand_start，但没有完成一手；只数 settle，避免终局
  // 到达、REST 尚未刷新时短暂显示伪造的“第 1/70 手”。没有 hand_start 的
  // admin/platform 中止则由 Holdem progress 返回 null，同样不显示假进度。
  const completedProgress = gameSpec?.progressUnit === 'hand'
    ? events.filter((event) => event.type === 'settle').length
    : fullReplayProgress
  const zeroProgressTechnicalTerminal = technicalTerminal && (
    completedProgress == null || !Number.isFinite(completedProgress) || completedProgress <= 0
  )
  const progressText = !zeroProgressTechnicalTerminal && displayedProgress != null && displayedProgress > 0
    ? visibleProgressTotal != null && Number.isFinite(visibleProgressTotal) && visibleProgressTotal > 0
      ? `第 ${Math.min(displayedProgress, visibleProgressTotal)}/${visibleProgressTotal} ${progressUnitLabel}`
      : `${finished ? '共' : '当前'} ${displayedProgress} ${progressUnitLabel}`
    : null
  const failedSeat = (() => {
    const sampleSeat = Number(technicalIncidents[0]?.seat)
    if (sampleSeat === 0 || sampleSeat === 1) return sampleSeat
    const counts = match?.result?.technical_incidents_by_seat ?? {}
    return ([0, 1] as const).find((seat) => Number(counts[seat] ?? 0) > 0)
  })()

  // 首个决策即技术终止时直接呈现权威终局。自动从头播放会让“已完成”页面
  // 暂时停在发牌事件，看起来像仍在正常比赛。
  useEffect(() => {
    if (!zeroProgressTechnicalTerminal || total <= 0) return
    setCursor(total - 1)
    setPlaying(false)
  }, [zeroProgressTechnicalTerminal, total])

  // 定速播放节拍（直播+回放共用）：只由播放态/速度控制生命周期。不能依赖
  // total；高频 SSE 每次增加 total 都会清理并重建 timeout，事件间隔持续短于
  // 播放速度时游标会被永久饿死。interval 每个稳定节拍读取最新长度 ref。
  useEffect(() => {
    if (!playing) return
    const timer = window.setInterval(() => {
      setCursor((value) => Math.min(
        Math.max(0, eventsLenRef.current - 1),
        value + 1,
      ))
    }, SPEEDS[speedIdx].ms)
    return () => window.clearInterval(timer)
  }, [playing, speedIdx])

  // 终态缓冲消费完毕后停止；直播追到当前尾部则保留 playing，让稳定节拍
  // 等待下一批事件。这个 effect 不拥有 timer，因此 total 变化不会重置节拍。
  useEffect(() => {
    if (playing && total > 0 && !realtime && cur >= total - 1) setPlaying(false)
  }, [playing, total, realtime, cur])

  const pause = () => {
    setCursor(cur)
    setPlaying(false)
  }
  const step = (delta: number) => {
    setPlaying(false)
    setCursor(Math.max(0, Math.min(Math.max(0, total - 1), cur + delta)))
  }
  const seek = (idx: number) => {
    setPlaying(false)
    setCursor(Math.max(0, Math.min(Math.max(0, total - 1), idx)))
  }
  const jumpToLive = () => {
    setCursor(Math.max(0, total - 1))
    setPlaying(true)
  }
  const jumpToTerminal = () => {
    setCursor(Math.max(0, total - 1))
    setPlaying(false)
  }
  const togglePlay = () => {
    if (playing) {
      pause()
      return
    }
    if (cur >= total - 1 && !realtime) setCursor(0)
    setPlaying(true)
  }

  // 可选的游戏分段导航（当前德州按手）；边界算法由游戏包提供。
  const bounds = useMemo(() => navigation?.boundaries(events) ?? [], [events, navigation])
  const jumpSegment = (delta: number) => {
    pause()
    if (!bounds.length) return
    let hIdx = 0
    for (let i = 0; i < bounds.length - 1; i++) if (cur >= bounds[i] && cur < bounds[i + 1]) { hIdx = i; break }
    const target = Math.max(0, Math.min(bounds.length - 2, hIdx + delta))
    setCursor(bounds[target] ?? 0)
  }
  const curSegmentIdx = (() => { for (let i = 0; i < bounds.length - 1; i++) if (cur >= bounds[i] && cur < bounds[i + 1]) return i; return bounds.length >= 2 ? bounds.length - 2 : 0 })()

  // 页面 main 是唯一纵向滚动 owner。右轨只给当前事件有限上下文，避免长回放
  // 在 1280/1560 与移动视口形成“页面滚动 + 日志滚动”的双纵滚。
  const actionContextStart = Math.max(0, cur - ACTION_CONTEXT_SIZE + 1)
  const actionContext = events.slice(actionContextStart, cur + 1)

  const GameIcon = gameIcon(gameId)
  const isLive = status === 'live' || isLiveMatch
  const winnerSeat = match?.winner === 0 || match?.winner === 1
    ? match.winner
    : eventWinner === 0 || eventWinner === 1
      ? eventWinner
      : null
  const playbackLabel = playing
    ? '暂停回放'
    : cur >= total - 1
      ? realtime ? '继续跟播' : '从头重播'
      : '继续回放'
  const recordDownload = match?.id === id
    && (match?.status === 'completed' || match?.status === 'aborted')
    ? gameSpec?.replay.recordDownload
    : undefined
  const matchLogDownload = match?.id === id
    && (match?.status === 'completed' || match?.status === 'aborted')
    && gameSpec
    && id
    ? {
        href: `/api/matches/${encodeURIComponent(id)}/log`,
        label: '导出对局日志（JSON）',
      }
    : undefined
  const renderSeat = (seat: 0 | 1) => {
    if (!match) return null
    const isWinner = winnerSeat === seat
    return (
      <MatchParticipantIdentity
        source={match}
        side={seat}
        variant="panel"
        state={isWinner ? 'winner' : winnerSeat != null ? 'loser' : 'neutral'}
        seatDetail={visibleSeatDetail(seat)}
        className={`${seat === 0 ? 'order-1' : 'order-2 sm:order-3'} border ${isWinner ? 'border-primary/40 bg-primary/5' : 'border-border bg-muted/20'}`}
      />
    )
  }

  return (
    <PageStub
      title={isLive ? '实时观赛' : '对局详情'}
      actions={matchLogDownload || (recordDownload && id) ? (
        <>
          {matchLogDownload && (
            <Button asChild variant="outline" size="sm" className="min-h-11">
              <a href={matchLogDownload.href} download>
                <Download aria-hidden="true" className="size-4" />
                {matchLogDownload.label}
              </a>
            </Button>
          )}
          {recordDownload && id && (
            <Button asChild variant="outline" size="sm" className="min-h-11">
              <a href={`/api/matches/${encodeURIComponent(id)}/record`} download>
                <Download aria-hidden="true" className="size-4" />
                {recordDownload.label}
              </a>
            </Button>
          )}
        </>
      ) : undefined}
    >
      <div className="mb-3 flex flex-wrap items-center gap-2 text-sm">
        <span className="max-w-full break-all font-mono text-xs text-muted-foreground">{id}</span>
        {match && (
          <Badge variant="secondary" className="gap-1"><GameIcon className="size-3" />{gameLabel(gameId)}</Badge>
        )}
        {match && <MatchNatureBadge matchType={match.match_type} source={match} />}
        {ratingStateBadge && (
          <Badge
            data-testid="rating-state"
            variant={ratingStateBadge.variant}
            className="max-w-full whitespace-normal text-[10px]"
          >
            {ratingStateBadge.label}
          </Badge>
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
          <Button
            variant="outline"
            size="sm"
            onClick={jumpToLive}
            className="gap-1"
            aria-label={`返回实时画面，当前落后 ${lag} 个回放事件`}
          >
            <Radio className="size-3" />返回实时画面
          </Button>
        )}
        {terminalBacklog > 0 && (
          <Button
            variant="outline"
            size="sm"
            onClick={jumpToTerminal}
            className="h-auto max-w-full gap-1 whitespace-normal py-1.5 text-left"
            aria-label={`直接查看最终结果，跳过剩余 ${terminalBacklog} 个回放事件`}
          >
            <SkipForward className="size-3 shrink-0" />
            直接查看最终结果
          </Button>
        )}
      </div>

      {/* 对阵与结果形成一个稳定层级；身份不再同时散落于标题、摘要和详情链接。 */}
      {match && (
        <Card data-testid="match-result-card" className="mb-3 gap-0 py-0">
          <CardContent className="grid grid-cols-2 gap-2 px-3 py-2 sm:grid-cols-[minmax(0,1fr)_minmax(8rem,auto)_minmax(0,1fr)] sm:items-center">
            {renderSeat(0)}
            <div className="order-3 col-span-2 min-w-0 border-t border-border pt-3 text-center sm:order-2 sm:col-span-1 sm:border-x sm:border-t-0 sm:px-4 sm:py-1">
              <div className="text-[10px] font-medium uppercase tracking-[0.16em] text-muted-foreground">
                {finished ? '对局结果' : '当前状态'}
              </div>
              <div className="mt-1 break-words text-sm font-semibold text-foreground">
                {finished ? winnerLabel : '对局进行中'}
              </div>
              {hasPersistedTerminalStatus && match.reason && (
                <div
                  data-testid="terminal-reason"
                  data-tone={terminalReason.tone}
                  className={`mt-1 text-xs ${terminalReason.tone === 'danger' ? 'text-destructive' : 'text-muted-foreground'}`}
                >
                  {terminalReason.label}
                </div>
              )}
            </div>
            {renderSeat(1)}
          </CardContent>
        </Card>
      )}

      {match && technicalTerminal && (
        <Card role="alert" className="mb-3 gap-0 border-destructive/35 bg-destructive/5 py-0">
          <CardContent className="px-4 py-3">
            <div className="flex items-start gap-3">
              <TriangleAlert className="mt-0.5 size-5 shrink-0 text-destructive" />
              <div className="min-w-0 flex-1">
                <div className="font-semibold text-foreground">Bot 技术判负</div>
                <p className="mt-1 break-words text-sm text-muted-foreground">
                  {failedSeat === 0 || failedSeat === 1
                    ? `${seatHeaderLabel(match, failedSeat)} · 座位 ${failedSeat + 1} 发生技术故障`
                    : '对局因 Bot 技术故障终止'}
                  {winnerSeat === 0 || winnerSeat === 1
                    ? `，${seatHeaderLabel(match, winnerSeat)} · 座位 ${winnerSeat + 1} 获胜。`
                    : '。'}
                </p>
                {technicalIncidents.length > 0 ? (
                  <div className="mt-3 space-y-2">
                    {technicalIncidents.slice(0, 3).map((incident, index) => (
                      <div key={`${incident.seat}-${incident.turn ?? 'x'}-${index}`} className="rounded-md border border-destructive/20 bg-background/70 px-3 py-2 text-xs">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="font-medium text-foreground">
                            {eventSeatSubject(seats, incident.seat)} · 座位 {Number(incident.seat) + 1}
                            {incident.turn != null ? ` · 第 ${incident.turn} 次决策` : ''}
                          </span>
                          {incident.code && <Badge variant="outline" className="max-w-full break-all font-mono text-[10px]">{incident.code}</Badge>}
                        </div>
                        <p className="mt-1 break-words text-muted-foreground">{incident.error}</p>
                      </div>
                    ))}
                  </div>
                ) : (
                  <p className="mt-2 text-xs text-muted-foreground">故障类型：{terminalReason.label}</p>
                )}
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      {user
        && match
        && match.id === id
        && debugPermissionScope === `${id}:${user.id}`
        && match.can_view_debug
        && botDebug
        && botDebug.match_id === match.id
        && botDebug.entry_count > 0 && (
        <BotDebugPanel
          payload={botDebug}
          seatNames={[seatHeaderLabel(match, 0), seatHeaderLabel(match, 1)]}
        />
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
        <div className={gameSpec?.replay.layout === 'wide'
          ? 'space-y-3'
          : viewportDashboard
            ? compactViewportDashboard
              ? 'grid items-start justify-center gap-3 md:grid-cols-[minmax(12rem,15rem)_minmax(0,min(52rem,calc(100dvh-6rem)))]'
              : 'grid items-start justify-center gap-3 md:grid-cols-[minmax(12rem,15rem)_minmax(0,min(52rem,calc(100dvh-6rem)))] xl:grid-cols-[minmax(0,min(52rem,calc(100dvh-16rem)))_minmax(17rem,19rem)] 2xl:grid-cols-[minmax(13rem,15rem)_minmax(0,min(52rem,calc(100dvh-16rem)))_minmax(17rem,19rem)]'
            : timelineCollapsed
              ? ReplayHud
                ? 'grid items-start gap-3 xl:grid-cols-[15rem_minmax(0,1fr)]'
                : 'grid gap-3'
              : ReplayHud
                ? 'grid items-start gap-3 xl:grid-cols-[minmax(0,1fr)_minmax(17rem,19rem)] 3xl:grid-cols-[15rem_minmax(0,1fr)_minmax(17rem,19rem)]'
                : viewportFitCanvas
                  ? 'grid items-start justify-center gap-3 xl:grid-cols-[minmax(0,min(52rem,calc(100dvh-16rem)))_minmax(17rem,19rem)]'
                  : 'grid items-start gap-3 xl:grid-cols-[minmax(0,1fr)_minmax(17rem,19rem)]'}>
          {viewportDashboard && ReplayHud && visibleVm !== null && (
            <div className={compactViewportDashboard
              ? 'min-w-0 md:col-start-1 md:row-start-1'
              : 'min-w-0 md:col-start-1 md:row-start-1 xl:col-start-1 xl:row-start-1 2xl:col-start-1 2xl:row-start-1'}>
              <ReplayHud vm={visibleVm} seats={seats} />
            </div>
          )}
          {!viewportDashboard && ReplayHud && visibleVm !== null && (
            <div className={`min-w-0 ${timelineCollapsed
              ? 'xl:col-start-1 xl:row-start-1'
              : 'xl:col-start-1 xl:row-start-1 3xl:col-start-1 3xl:row-start-1'}`}>
              <ReplayHud vm={visibleVm} seats={seats} />
            </div>
          )}

          {/* 左：canvas 棋盘/牌桌 + 分段导航 + 控制条 */}
          <div className={`min-w-0 space-y-2.5 ${viewportFitCanvas ? 'w-full justify-self-center md:max-w-[min(52rem,calc(100dvh-6rem))] xl:max-w-[min(52rem,calc(100dvh-16rem))]' : ''} ${viewportDashboard
            ? compactViewportDashboard
              ? 'md:col-start-2 md:row-start-1'
              : 'md:col-start-2 md:row-start-1 xl:col-start-1 xl:row-start-2 2xl:col-start-2 2xl:row-start-1'
            : ReplayHud
              ? timelineCollapsed
              ? 'xl:col-start-2 xl:row-start-1'
              : 'xl:col-start-1 xl:row-start-2 3xl:col-start-2 3xl:row-start-1'
            : ''}`}>
            {ReplaySummary && visibleVm !== null && (
              <div className="flex min-w-0 flex-wrap items-center gap-2 rounded-lg border border-border bg-card px-3 py-2">
                <ReplaySummary vm={visibleVm} seats={seats} />
              </div>
            )}
            <MatchBoard gameId={gameId} events={visible} seats={seats} revealMode="all" />

            {/* 技术终止且没有完成一手/一步时，直接定位终局，不展示伪装成正常赛程的播放控制。 */}
            {!zeroProgressTechnicalTerminal && (
              <Card className="gap-0 py-0">
                <CardContent className="px-3 py-2.5">
                  <div className="flex flex-wrap items-center justify-center gap-1.5">
                    {navigation && (
                      <Button variant="outline" size="sm" onClick={() => jumpSegment(-1)} className="gap-1"><SkipBack className="size-3.5" />上一{navigation.unitLabel}</Button>
                    )}
                    <Button variant="outline" size="sm" onClick={() => step(-1)} className="gap-1"><ChevronLeft className="size-4" />上一个事件</Button>
                    <Button variant="default" size="sm" onClick={togglePlay} className="gap-1.5">
                      {playing ? <Pause className="size-4" /> : <Play className="size-4" />}{playbackLabel}
                    </Button>
                    <Button variant="outline" size="sm" onClick={() => step(1)} className="gap-1">下一个事件<ChevronRight className="size-4" /></Button>
                    {navigation && (
                      <Button variant="outline" size="sm" onClick={() => jumpSegment(1)} className="gap-1">下一{navigation.unitLabel}<SkipForward className="size-3.5" /></Button>
                    )}
                    {navigation && bounds.length >= 2 && (
                      <Select
                        value={String(curSegmentIdx)}
                        onValueChange={(value) => seek(bounds[Number(value)] ?? 0)}
                      >
                        <SelectTrigger size="sm" className="h-8 w-[6.5rem] text-xs" aria-label={`跳转${navigation.unitLabel}`}>
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          {Array.from({ length: bounds.length - 1 }, (_, segment) => (
                            <SelectItem key={segment} value={String(segment)}>
                              {navigation.label?.(segment, events) ?? `第 ${segment + 1} ${navigation.unitLabel}`}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    )}
                    <Select value={String(speedIdx)} onValueChange={(v) => setSpeedIdx(Number(v))}>
                      <SelectTrigger size="sm" className="h-8 w-[5rem] text-xs" aria-label="回放速度">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {SPEEDS.map((s, i) => (<SelectItem key={i} value={String(i)}>{s.label}</SelectItem>))}
                      </SelectContent>
                    </Select>
                  </div>
                  <div className="mt-2.5 flex items-center gap-3">
                    <span data-testid="playback-position" className="shrink-0 font-mono text-[10px] text-muted-foreground">事件 {cur + 1}/{total}{atLive && realtime ? ' · 直播' : ''}</span>
                    <Slider aria-label="回放进度" min={0} max={Math.max(0, total - 1)} value={[cur]} onValueChange={(v) => seek(v[0])} className="flex-1" />
                  </div>
                </CardContent>
              </Card>
            )}
          </div>

          {/* 右：有限动作上下文 */}
          <Card
            data-testid="match-timeline"
            className={`flex flex-col gap-0 self-start overflow-hidden py-0 ${compactViewportDashboard ? '' : 'xl:sticky xl:top-6'} ${viewportDashboard
              ? compactViewportDashboard
                ? 'md:col-span-2 md:col-start-1 md:row-start-2'
                : 'md:col-span-2 md:col-start-1 md:row-start-2 xl:col-span-1 xl:col-start-2 xl:row-span-2 xl:row-start-1 2xl:col-start-3 2xl:row-span-1 2xl:row-start-1'
              : ReplayHud
                ? timelineCollapsed
                  ? 'xl:col-span-2 xl:row-start-2'
                  : 'xl:col-start-2 xl:row-start-1 xl:row-span-2 3xl:col-start-3 3xl:row-start-1 3xl:row-span-1'
                : ''}`}
          >
            <div className="border-b border-border px-4 py-2">
              <div className="flex items-center justify-between">
                <span className="min-w-0 text-sm font-medium">
                  动作上下文 <span className="text-xs font-normal text-muted-foreground">({actionContextStart + 1}–{cur + 1}/{total})</span>
                </span>
                <Button variant="ghost" size="sm" onClick={() => setTimelineCollapsed(c => !c)}>
                  {timelineCollapsed ? '展开动作' : '收起动作'}
                </Button>
              </div>
            </div>
            {!timelineCollapsed && (
              <div className="p-2 text-xs">
                <p className="px-2 pb-1.5 text-[11px] leading-relaxed text-muted-foreground">
                  当前事件及之前最多 {ACTION_CONTEXT_SIZE - 1} 条；完整过程用下方进度条定位。
                </p>
                {actionContext.map((ev, index) => {
                  const eventIndex = actionContextStart + index
                  return (
                    <div
                      key={eventIndex}
                      data-testid="match-action-context-row"
                      className={`flex items-center gap-2 rounded px-2 py-1.5 ${eventIndex === cur ? 'bg-primary/10 font-medium text-primary' : 'text-muted-foreground'}`}
                    >
                      <span className="w-8 shrink-0 font-mono opacity-60">{eventIndex + 1}</span>
                      <span className="min-w-0 flex-1 break-words [overflow-wrap:anywhere]">
                        {describeTimelineEvent(ev, gameSpec?.describeEvent(ev, seats) ?? String(ev.type || '?'), seats)}
                      </span>
                    </div>
                  )
                })}
              </div>
            )}
          </Card>
        </div>
      )}

      {match?.contest_id != null ? (
        <Button asChild variant="ghost" size="sm" className="mt-6 min-h-11 gap-1.5">
          <Link to={`/contests/${match.contest_id}/live`}>
            <ArrowLeft aria-hidden="true" className="size-4" />返回赛事直播
          </Link>
        </Button>
      ) : (
        <Button variant="ghost" size="sm" className="mt-6 min-h-11 gap-1.5" onClick={() => navigate(-1)}>
          <ArrowLeft aria-hidden="true" className="size-4" />返回
        </Button>
      )}
      {id && <Comments targetType="match" targetId={id} />}
    </PageStub>
  )
}
