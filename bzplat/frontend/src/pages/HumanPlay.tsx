import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { PlayCircle, ArrowRight, Clock, Swords, UserRound, Activity } from 'lucide-react'
import { DataRegion, PageFrame, PageHeader, StickyToolbar, SummaryStrip } from '@/components/layout'
import MatchBoard from '@/components/MatchBoard'
import { MatchNatureBadge } from '@/components/MatchParticipants'
import { Button } from '@/components/ui/button'
import { OverflowText } from '@/components/ui/overflow-text'
import { ErrorMsg, Loading } from '@/components/ui/status'
import { playWsUrl } from '@/api'
import { findGame, gameLabel, normalizeGameId, resolveTerminalReason, unsupportedGameLabel } from '@/games'
import { describePlatformEvent } from '@/games/reasons'
import {
  type MatchSeatRow,
  seatInfos,
  seatHeaderLabel,
  resolveWinnerLabel,
} from '@/lib/match-seats'
import type { HumanActionEnvelope, RawEvent } from '@/games/base'
import { SummaryMetric } from '@/pages/public-page-ui'

type Ev = Record<string, unknown> & { type?: string }

const TURN_CLOSING_EVENTS = new Set([
  'move',
  'action',
  'pass',
  'settle',
  'match_end',
  'error',
])

/** Locate the latest human turn in an authoritative replay snapshot.
 *
 * The ordinal gives a stable identity to a turn across WebSocket reconnects. It
 * lets the client keep an already-submitted turn locked when the same snapshot
 * is replayed, while still unlocking when the snapshot contains a genuinely new
 * `your_turn` that was missed during the disconnect.
 */
function humanTurnCursor(events: Ev[], humanSeat: number): { ordinal: number; pending: boolean } {
  let ordinal = 0
  let pending = false
  for (const ev of events) {
    if (ev.type === 'your_turn') {
      if (Number(ev.player) === humanSeat) {
        ordinal += 1
        pending = true
      } else if (pending) {
        pending = false
      }
    } else if (pending && ev.type && TURN_CLOSING_EVENTS.has(ev.type)) {
      pending = false
    }
  }
  return { ordinal, pending }
}

/** 默认人类超时（与后端 human_action_timeout 默认 120s 对齐，仅 UI 提示） */
const HUMAN_TIMEOUT_SEC = 120

export default function HumanPlay() {
  const { id } = useParams<{ id: string }>()
  const [match, setMatch] = useState<MatchSeatRow | null>(null)
  const [events, setEvents] = useState<Ev[]>([])
  const [over, setOver] = useState(false)
  const [error, setError] = useState('')
  const [endInfo, setEndInfo] = useState<{ winner?: number | null; reason?: string } | null>(null)
  const [turnDeadline, setTurnDeadline] = useState<number | null>(null)
  const [nowTs, setNowTs] = useState(() => Date.now())
  const wsRef = useRef<WebSocket | null>(null)
  // state drives disabled styling; the ref closes the same-render double-click
  // window synchronously before React has committed the state update.
  const [actionSubmitted, setActionSubmitted] = useState(false)
  const actionSubmittedRef = useRef(false)
  const turnOrdinalRef = useRef(0)
  const activeTurnOrdinalRef = useRef<number | null>(null)
  // 重连：网络断开时自动重连（指数退避，≤5 次）。match 结束或组件卸载后停止。
  const overRef = useRef(false)
  const [reconnecting, setReconnecting] = useState(false)

  useEffect(() => {
    if (!id) return
    // Hash 参数切换时先丢弃旧局 UI；不能让同游戏、同事件数的旧 canvas 继续可点。
    setMatch(null)
    setEvents([])
    setOver(false)
    setError('')
    setEndInfo(null)
    setTurnDeadline(null)
    setReconnecting(false)
    overRef.current = false
    actionSubmittedRef.current = false
    activeTurnOrdinalRef.current = null
    turnOrdinalRef.current = 0
    setActionSubmitted(false)
    // 每次 effect 独立的失效标记。不能用共享 ref：React StrictMode cleanup 后新 effect
    // 会把 ref 重置，旧 socket 的延迟 close 随即误判为仍存活并启动重连风暴。
    let disposed = false
    let attempt = 0
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null

    const connect = () => {
      if (overRef.current || disposed) return
      if (wsRef.current) { wsRef.current.close(); wsRef.current = null }
      const ws = new WebSocket(playWsUrl(id))
      wsRef.current = ws
      ws.onmessage = (e) => {
        if (disposed) return
        try {
          const ev = JSON.parse(e.data)
          if (ev.type === 'snapshot') {
            // 重连后服务端重发 snapshot：resync 状态（含已发生事件）
            attempt = 0
            const snapshotMatch = (ev.match || {}) as MatchSeatRow
            const history = (Array.isArray(ev.events) ? ev.events : []) as Ev[]
            const terminal = history.slice().reverse().find(
              (item) => item.type === 'match_end' || item.type === 'error',
            )
            const terminalStatus = snapshotMatch.status === 'completed' || snapshotMatch.status === 'aborted'
            const snapshotHumanSeat = snapshotMatch.human_seat ?? 1
            const cursor = humanTurnCursor(history, snapshotHumanSeat)
            turnOrdinalRef.current = cursor.ordinal
            setMatch(snapshotMatch)
            setEvents(history)
            setReconnecting(false)
            if (terminal || terminalStatus) {
              actionSubmittedRef.current = true
              setActionSubmitted(true)
              setOver(true)
              overRef.current = true
              setTurnDeadline(null)
              setEndInfo({
                winner: (terminal?.winner ?? snapshotMatch.winner) as number | null | undefined,
                reason: String(
                  terminal?.reason ||
                  (terminal?.type === 'error' ? 'platform_error' : snapshotMatch.reason || ''),
                ),
              })
              // 服务端 pump 在 terminal event 后结束，但 handler 仍等待 receive_json；
              // 客户端确认快照为终态后主动关闭，触发后端 finally 取消订阅。
              ws.close(1000, 'match complete')
            } else {
              setOver(false)
              setEndInfo(null)
              if (cursor.pending && activeTurnOrdinalRef.current !== cursor.ordinal) {
                // First authoritative view of this turn (initial load or a turn
                // missed while disconnected): the user must be able to act.
                activeTurnOrdinalRef.current = cursor.ordinal
                actionSubmittedRef.current = false
                setActionSubmitted(false)
                setError('')
              } else if (!cursor.pending) {
                // No action is currently legal. Preserve the submitted lock; the
                // next live `your_turn` is the only normal path that unlocks it.
                activeTurnOrdinalRef.current = cursor.ordinal
              }
            }
          } else if (ev.type === 'match_end' || ev.type === 'error') {
            actionSubmittedRef.current = true
            setActionSubmitted(true)
            setEvents((prev) => [...prev, ev])
            setOver(true)
            overRef.current = true
            setTurnDeadline(null)
            setEndInfo({
              winner: ev.winner as number | null | undefined,
              reason: String(ev.reason || (ev.type === 'error' ? 'platform_error' : '')),
            })
            setMatch((prev) => {
              if (!prev) return prev
              const next: MatchSeatRow = {
                ...prev,
                status: ev.type === 'error' ? 'aborted' : 'completed',
                winner: ev.winner as number | null | undefined,
              }
              if (Array.isArray(ev.deltas) && ev.deltas.length >= 2) {
                next.result = {
                  ...(prev.result || {}),
                  deltas: [Number(ev.deltas[0]), Number(ev.deltas[1])],
                }
              }
              return next
            })
            setReconnecting(false)
            // 终态后不再需要双向通道；主动关闭令后端 receive_json 退出并 unsubscribe。
            ws.close(1000, 'match complete')
          } else {
            setEvents((prev) => [...prev, ev])
            setReconnecting(false)
            if (ev.type === 'your_turn') {
              turnOrdinalRef.current += 1
              activeTurnOrdinalRef.current = turnOrdinalRef.current
              actionSubmittedRef.current = false
              setActionSubmitted(false)
              setError('')
              setTurnDeadline(Date.now() + HUMAN_TIMEOUT_SEC * 1000)
            } else if (ev.type === 'reject') {
              // The backend explicitly did not consume this frame, so this same
              // turn may be retried. Do not unlock for ordinary action/move events.
              actionSubmittedRef.current = false
              setActionSubmitted(false)
              setError(String(ev.message || '动作未被接受，请重试。'))
            }
          }
        } catch {
          /* ignore parse error */
        }
      }
      ws.onerror = () => { if (!disposed) setError('连接异常') }
      ws.onclose = () => {
        // match 结束后服务端正常关闭；否则尝试重连
        if (overRef.current || disposed) return
        attempt += 1
        if (attempt > 5) {
          setError('连接已断开，重连失败。请刷新页面。')
          setReconnecting(false)
          return
        }
        setReconnecting(true)
        setError('')
        const delay = Math.min(8000, 500 * 2 ** (attempt - 1)) // 0.5/1/2/4/8s
        reconnectTimer = setTimeout(connect, delay)
      }
    }
    // 推迟到当前任务结束：StrictMode 的探测性首轮 effect 会先 cleanup 并取消定时器，
    // 避免创建后立即关闭 CONNECTING socket 所产生的 Console warning。
    reconnectTimer = setTimeout(connect, 0)
    return () => {
      disposed = true
      if (reconnectTimer) clearTimeout(reconnectTimer)
      if (wsRef.current) { wsRef.current.close(); wsRef.current = null }
    }
  }, [id])

  // 回合倒计时 tick
  useEffect(() => {
    if (!turnDeadline || over) return
    const t = setInterval(() => setNowTs(Date.now()), 500)
    return () => clearInterval(t)
  }, [turnDeadline, over])

  const gameId = normalizeGameId(match?.game_id)
  const gameSpec = match ? findGame(gameId) : undefined
  const humanSeat = match?.human_seat ?? 1
  const seats = seatInfos(match)

  const myTurn = useMemo(() => {
    if (over) return false
    return humanTurnCursor(events, humanSeat).pending
  }, [events, humanSeat, over])

  // 最近一次当前玩家的权威 request；具体字段只由当前游戏动作插件解释。
  const turnRequest = useMemo(() => {
    if (!myTurn) return null
    for (let i = events.length - 1; i >= 0; i--) {
      const ev = events[i]
      if (ev.type === 'your_turn' && Number(ev.player) === humanSeat) {
        return (ev.request as Record<string, unknown> | undefined) ?? null
      }
    }
    return null
  }, [events, humanSeat, myTurn])

  const sendMove = (move: HumanActionEnvelope) => {
    if (!myTurn || actionSubmittedRef.current) return
    // 检查连接状态：socket 已关闭时给明确反馈，否则动作被静默吞掉（用户以为出了牌）。
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
      setError('连接已断开，动作未发送。请刷新页面重连。')
      return
    }
    actionSubmittedRef.current = true
    setActionSubmitted(true)
    setError('')
    try {
      wsRef.current.send(JSON.stringify(move))
      setTurnDeadline(null)
    } catch {
      // OPEN can race with a transport close. No frame was accepted, so keep the
      // current turn retryable and surface a concrete connection error.
      actionSubmittedRef.current = false
      setActionSubmitted(false)
      setError('连接已断开，动作未发送。请刷新页面重连。')
    }
  }

  const canSubmitAction = myTurn && !actionSubmitted
  const remainSec = turnDeadline ? Math.max(0, Math.ceil((turnDeadline - nowTs) / 1000)) : null

  // 当前局面与结局摘要都经注册表 spec.reduce；点格棋、德州等游戏可把
  // 同一权威 ViewModel 用于 HUD，通用页不读取游戏专属字段。
  const currentVm = useMemo(() => {
    if (!events.length) return null
    return gameSpec?.reduce(events as RawEvent[]) ?? null
  }, [events, gameSpec])
  const endVm = over ? currentVm : null

  const winnerLabel = resolveWinnerLabel(
    match,
    endInfo?.winner ?? (endVm && gameSpec ? gameSpec.winner(endVm) : undefined),
    over,
    // 显示从 1 起计（后端 0 起计，DB CHECK 约束未变）。
    (seat) => (match ? seatHeaderLabel(match, seat as 0 | 1) : `座位 ${seat + 1}`),
  )
  const endSummary = endVm ? gameSpec?.humanPlay.endSummary?.(endVm) : null
  const ActionPanel = gameSpec?.humanPlay.ActionPanel
  const ReplayHud = gameSpec?.replay.Hud
  const viewportFitCanvas = gameSpec?.canvasFit === 'viewport'
  const viewportDashboard = viewportFitCanvas && Boolean(ReplayHud)
  const turnLabel = gameSpec?.humanPlay.turnLabelForRequest?.(turnRequest)
    ?? gameSpec?.humanPlay.turnLabel
    ?? '轮到你操作'
  const boardInteractive = canSubmitAction
    && Boolean(gameSpec?.humanPlay.serializeBoardPick)
    && (gameSpec?.humanPlay.canPickBoard?.(turnRequest) ?? true)
  const terminalReason = gameSpec
    ? gameSpec.terminalReason(endInfo?.reason, match?.status)
    : resolveTerminalReason(endInfo?.reason, match?.status)
  const matchupLabel = match
    ? `${seatHeaderLabel(match, 0)} vs ${seatHeaderLabel(match, 1)}`
    : '正在建立连接'

  // useEffect 在提交后清状态；这一同步 guard 还会挡住路由切换后的首个 render。
  if (match?.id && String(match.id) !== id) {
    return (
      <PageFrame width="wide" layout="game-human-play-switching">
        <PageHeader title="人类对战" description="正在载入新的权威对局状态。" />
        <DataRegion title="对局连接" contentClassName="px-4 py-6 text-sm text-muted-foreground">
          正在切换对局…
        </DataRegion>
      </PageFrame>
    )
  }

  return (
    <PageFrame width="wide" layout="game-human-play">
      <PageHeader
        title="人类对战"
        description="权威局面、行动控件与裁判事件保持同步；断线时页面会自动重连。"
        actions={
          <Button asChild variant="outline" size="sm">
            <Link to={`/match/${id}`}>查看回放<ArrowRight aria-hidden="true" className="size-4" /></Link>
          </Button>
        }
      />

      <SummaryStrip columns={4} label="人类对战概览" className="grid-cols-2 xl:grid-cols-4">
        <SummaryMetric label="游戏" value={match ? gameLabel(gameId) : '加载中'} mono={false} icon={<Swords className="size-4" />} />
        <SummaryMetric label="我的座位" value={match ? `座位 ${humanSeat + 1}` : '加载中'} icon={<UserRound className="size-4" />} />
        <SummaryMetric label="对阵" value={matchupLabel} mono={false} icon={<Swords className="size-4" />} />
        <SummaryMetric label="权威事件" value={events.length} detail={reconnecting ? '连接恢复中' : over ? '对局已结束' : '实时同步'} icon={<Activity className="size-4" />} />
      </SummaryStrip>

      <StickyToolbar label="人类对战状态" className="justify-between">
        <div className="flex min-w-0 flex-1 flex-wrap items-center gap-x-2 gap-y-1 text-sm">
          {match && <MatchNatureBadge matchType={match.match_type} source={match} />}
          {over ? (
            <span className="min-w-0 break-words font-medium text-foreground">
              对局结束 · 胜者：{winnerLabel}
              {endInfo?.reason && (
                <span
                  data-testid="terminal-reason"
                  data-tone={terminalReason.tone}
                  className={terminalReason.tone === 'danger' ? 'text-destructive' : 'text-muted-foreground'}
                >
                  {`（${terminalReason.label}）`}
                </span>
              )}
            </span>
          ) : canSubmitAction ? (
            <span className="flex min-w-0 items-center gap-1 font-medium text-success">
              <PlayCircle aria-hidden="true" className="size-4 shrink-0" />
              {turnLabel}
              {remainSec != null && (
                <span aria-label={`本回合剩余约 ${remainSec} 秒`} className="ml-1 inline-flex shrink-0 items-center gap-0.5 text-xs text-muted-foreground tabular-nums">
                  <Clock aria-hidden="true" className="size-3" />{remainSec}s
                </span>
              )}
            </span>
          ) : actionSubmitted && myTurn ? (
            <span className="text-muted-foreground">动作已提交，等待裁判处理…</span>
          ) : (
              <span className="text-muted-foreground">等待中…</span>
          )}
        </div>
        {over && endSummary && (
          <OverflowText tooltip={endSummary} className="max-w-full font-mono text-xs text-muted-foreground sm:max-w-sm">
            {endSummary}
          </OverflowText>
        )}
      </StickyToolbar>

      {error && <ErrorMsg msg={error} />}
      {reconnecting && (
        <div role="status" className="rounded-lg border border-warning/40 bg-warning/10 px-3 py-2 text-sm text-warning">
          连接已断开，正在重连…（请勿关闭页面）
        </div>
      )}

      {!match && !error && (
        <DataRegion title="对局连接" contentClassName="px-4 py-6">
          <Loading text="正在读取权威局面…" />
        </DataRegion>
      )}

      {match && !gameSpec && (
        <ErrorMsg msg={`无法进入人类对战：${unsupportedGameLabel(match.game_id)}`} />
      )}

      {/* 排布、动作控件和 WS 序列化均由当前游戏规格提供。 */}
      {gameSpec?.humanPlay.layout === 'canvas-with-log' && (
        <div className={viewportDashboard
          ? 'grid min-w-0 items-start justify-center gap-4 md:grid-cols-[minmax(12rem,15rem)_minmax(0,min(52rem,calc(100dvh-6rem)))] xl:grid-cols-[minmax(0,min(52rem,calc(100dvh-16rem)))_minmax(17rem,19rem)] 2xl:grid-cols-[minmax(13rem,15rem)_minmax(0,min(52rem,calc(100dvh-16rem)))_minmax(17rem,19rem)]'
          : viewportFitCanvas
            ? 'grid min-w-0 items-start justify-center gap-4 xl:grid-cols-[minmax(0,min(52rem,calc(100dvh-16rem)))_22rem]'
          : 'grid min-w-0 gap-4 xl:grid-cols-[minmax(0,1fr)_22rem]'}>
          {viewportDashboard && ReplayHud && currentVm !== null && (
            <div className="min-w-0 md:col-start-1 md:row-start-1 xl:col-start-1 xl:row-start-1 2xl:col-start-1 2xl:row-start-1">
              <ReplayHud vm={currentVm} seats={seats} />
            </div>
          )}
          <div className={`space-y-3 ${viewportFitCanvas ? 'w-full justify-self-center md:max-w-[min(52rem,calc(100dvh-6rem))] xl:max-w-[min(52rem,calc(100dvh-16rem))]' : ''} ${viewportDashboard ? 'md:col-start-2 md:row-start-1 xl:col-start-1 xl:row-start-2 2xl:col-start-2 2xl:row-start-1' : ''}`}>
            <MatchBoard
              gameId={gameSpec.id}
              events={events}
              seats={seats}
              revealMode={gameSpec.humanPlay.revealMode}
              onMove={(x, y) => {
                const action = gameSpec.humanPlay.serializeBoardPick?.(x, y)
                if (action) sendMove(action)
              }}
              interactive={boardInteractive}
            />
            {ActionPanel && (
              <ActionPanel
                disabled={!canSubmitAction || over}
                legal={canSubmitAction}
                request={turnRequest}
                onSubmit={sendMove}
              />
            )}
          </div>
          <div className={viewportDashboard ? 'min-w-0 md:col-span-2 md:col-start-1 md:row-start-2 xl:sticky xl:top-[var(--sticky-table-offset)] xl:col-span-1 xl:col-start-2 xl:row-span-2 xl:row-start-1 2xl:col-start-3 2xl:row-span-1 2xl:row-start-1' : 'min-w-0'}>
            <EventLogCard events={events} describeEvent={gameSpec.describeEvent} />
          </div>
        </div>
      )}

      {gameSpec?.humanPlay.layout === 'canvas-controls-log' && (
        <div className={ReplayHud
          ? 'grid min-w-0 items-start gap-3 xl:grid-cols-[minmax(0,1fr)_18rem] 3xl:grid-cols-[15rem_minmax(0,1fr)_18rem]'
          : 'grid min-w-0 items-start gap-3 xl:grid-cols-[minmax(0,1fr)_18rem]'}>
          {ReplayHud && currentVm !== null && (
            <div className="min-w-0 xl:col-start-1 xl:row-start-1 3xl:col-start-1 3xl:row-start-1">
              <ReplayHud vm={currentVm} seats={seats} />
            </div>
          )}
          <div className={`min-w-0 space-y-3 ${ReplayHud ? 'xl:col-start-1 xl:row-start-2 3xl:col-start-2 3xl:row-start-1' : ''}`}>
            <MatchBoard
              gameId={gameSpec.id}
              events={events}
              seats={seats}
              revealMode={gameSpec.humanPlay.revealMode}
            />
            {ActionPanel && (
              <ActionPanel
                disabled={!canSubmitAction || over}
                legal={canSubmitAction}
                request={turnRequest}
                onSubmit={sendMove}
              />
            )}
          </div>
          <div className={`min-w-0 xl:sticky xl:top-[var(--sticky-table-offset)] ${ReplayHud
            ? 'xl:col-start-2 xl:row-start-1 xl:row-span-2 3xl:col-start-3 3xl:row-start-1 3xl:row-span-1'
            : 'xl:col-start-2 xl:row-start-1'}`}>
            <EventLogCard events={events} describeEvent={gameSpec.describeEvent} />
          </div>
        </div>
      )}
    </PageFrame>
  )
}

/** 对局进程事件日志卡；事件含义由游戏包负责描述。 */
function EventLogCard({
  events,
  describeEvent,
}: {
  events: Ev[]
  describeEvent: (event: RawEvent) => string
}) {
  const describe = (event: Ev) => {
    if (event.type === 'technical_incident') {
      const seat = Number(event.seat)
      const seatText = Number.isFinite(seat) ? `座位 ${seat + 1}` : 'Bot'
      const turn = Number(event.turn)
      const turnText = Number.isFinite(turn) && turn > 0 ? ` · 第 ${turn} 次决策` : ''
      return `${seatText} 技术故障${turnText}：${String(event.error || 'Bot 响应异常')}`
    }
    const platformDescription = describePlatformEvent(event)
    if (platformDescription) return platformDescription
    return describeEvent(event)
  }
  return (
    <DataRegion
      data-testid="human-event-log"
      title="对局进程"
      description={`${events.length} 条权威事件 · 最新在前`}
      className="flex min-w-0 flex-col"
      contentClassName="max-h-none flex-1 p-2 text-xs xl:max-h-[min(70dvh,36rem)]"
      overflow="y"
      regionLabel="人类对战事件日志"
    >
        {events.length === 0 ? (
          <p className="py-6 text-center text-muted-foreground">等待对局开始…</p>
        ) : (
          events.slice().reverse().map((ev, i) => (
            <div key={i} className="flex min-w-0 items-start gap-2 rounded-md px-2 py-1.5 text-muted-foreground hover:bg-muted/50">
              <span className="w-10 shrink-0 font-mono tabular-nums opacity-60">#{events.length - i}</span>
              <span className="min-w-0 flex-1 break-words leading-relaxed opacity-80 [overflow-wrap:anywhere]">
                {describe(ev)}
              </span>
            </div>
          ))
        )}
    </DataRegion>
  )
}
