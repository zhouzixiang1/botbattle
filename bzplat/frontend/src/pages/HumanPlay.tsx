import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { PlayCircle, ArrowRight, Clock } from 'lucide-react'
import PageStub from '@/components/PageStub'
import MatchBoard from '@/components/MatchBoard'
import { Card } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { ErrorMsg } from '@/components/ui/status'
import { playWsUrl } from '@/api'
import { gameLabel, normalizeGameId } from '@/lib/games'
import { isBoardGame, getGame } from '@/games'
import {
  type MatchSeatRow,
  seatInfos,
  seatHeaderLabel,
  resolveWinnerLabel,
  fmtNet,
} from '@/lib/match-seats'
import type { RawEvent } from '@/games/base'

type Ev = Record<string, unknown> & { type?: string }

/** 默认人类超时（与后端 human_action_timeout 默认 120s 对齐，仅 UI 提示） */
const HUMAN_TIMEOUT_SEC = 120

/** 从 Botzone 标准 holdem 请求（11 字段，无 to_call 扩展）推导当前跟注额。
 *
 * 纯 Botzone TexasHoldem2p 模型：跟注额 = 本街最高下注 − 我本街已下注。
 * 本街已下注从 history 重放（含盲注：dealer_id 是 SB，另一座是 BB）。
 * 与后端 engine 的判定一致，仅供人类对战 UI 提示（精确以引擎校验为准）。
 */
function deriveToCall(request: Record<string, unknown> | null): number {
  if (!request) return 0
  const myId = Number(request.my_id ?? 0)
  const dealerId = Number(request.dealer_id ?? 0)
  const bbSeat = 1 - dealerId // dealer=SB，另一座=BB
  const SB = 50, BB = 100
  // 本街各方已下注（翻前初始含盲注）
  const bets = [0, 0]
  bets[dealerId] = SB
  bets[bbSeat] = BB
  // 翻后每进入新 street 重置；用 history.round 变化检测换街
  let lastRound = 0
  const history = (request.history as Array<Record<string, unknown>> | undefined) ?? []
  for (const h of history) {
    const round = Number(h.round ?? 0)
    if (round > lastRound) {
      // 进入新街道：重置本街下注（保留盲注语义只在 preflop）
      bets[0] = 0
      bets[1] = 0
      lastRound = round
    }
    const pid = Number(h.player_id ?? 0)
    const action = Number(h.action ?? 0)
    if (action === -1 || action === -2) {
      bets[pid] = -1 // fold/allin 标记，不参与跟注计算
    } else if (action > 0) {
      bets[pid] += action // raise delta 累加到本街已投
    }
    // call/check(0)：跟注到本街最高，用最高值兜底
    if (action === 0) {
      bets[pid] = Math.max(bets[0], bets[1])
    }
  }
  const streetBet = Math.max(bets[0], bets[1])
  return Math.max(0, streetBet - bets[myId])
}

/** 推导本街自己已下注筹码（用于 raise 换算：目标总额 − 本街已投 = 额外量）。
 *  与 deriveToCall 同源逻辑，返回 bets[myId]。 */
function deriveMyBet(request: Record<string, unknown> | null): number {
  if (!request) return 0
  const myId = Number(request.my_id ?? 0)
  const dealerId = Number(request.dealer_id ?? 0)
  const bbSeat = 1 - dealerId
  const SB = 50, BB = 100
  const bets = [0, 0]
  bets[dealerId] = SB
  bets[bbSeat] = BB
  let lastRound = 0
  const history = (request.history as Array<Record<string, unknown>> | undefined) ?? []
  for (const h of history) {
    const round = Number(h.round ?? 0)
    if (round > lastRound) { bets[0] = 0; bets[1] = 0; lastRound = round }
    const pid = Number(h.player_id ?? 0)
    const action = Number(h.action ?? 0)
    if (action === -1 || action === -2) bets[pid] = -1
    else if (action > 0) bets[pid] += action
    if (action === 0) bets[pid] = Math.max(bets[0], bets[1])
  }
  return Math.max(0, bets[myId])
}

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
  // 重连：网络断开时自动重连（指数退避，≤5 次）。match 结束或组件卸载后停止。
  const overRef = useRef(false)
  const unmountedRef = useRef(false)
  const [reconnecting, setReconnecting] = useState(false)

  useEffect(() => {
    if (!id) return
    overRef.current = false
    unmountedRef.current = false
    let attempt = 0
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null

    const connect = () => {
      if (overRef.current || unmountedRef.current) return
      if (wsRef.current) { wsRef.current.close(); wsRef.current = null }
      const ws = new WebSocket(playWsUrl(id))
      wsRef.current = ws
      ws.onmessage = (e) => {
        try {
          const ev = JSON.parse(e.data)
          if (ev.type === 'snapshot') {
            // 重连后服务端重发 snapshot：resync 状态（含已发生事件）
            setMatch(ev.match || {})
            setEvents(ev.events || [])
            setReconnecting(false)
          } else if (ev.type === 'match_end' || ev.type === 'error') {
            setEvents((prev) => [...prev, ev])
            setOver(true)
            overRef.current = true
            setTurnDeadline(null)
            setEndInfo({
              winner: ev.winner as number | null | undefined,
              reason: ev.reason || ev.message,
            })
            setMatch((prev) => prev ? {
              ...prev,
              status: 'completed',
              winner: ev.winner as number | null | undefined,
              result: {
                ...(prev.result || {}),
                deltas: [
                  ev.earnings_a != null ? Number(ev.earnings_a) : prev.result?.deltas?.[0] ?? 0,
                  ev.earnings_b != null ? Number(ev.earnings_b) : prev.result?.deltas?.[1] ?? 0,
                ],
              },
            } : prev)
          } else {
            setEvents((prev) => [...prev, ev])
            setReconnecting(false)
            if (ev.type === 'your_turn') {
              setTurnDeadline(Date.now() + HUMAN_TIMEOUT_SEC * 1000)
            }
          }
        } catch {
          /* ignore parse error */
        }
      }
      ws.onerror = () => setError('连接异常')
      ws.onclose = () => {
        // match 结束后服务端正常关闭；否则尝试重连
        if (overRef.current || unmountedRef.current) return
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
    connect()
    return () => {
      unmountedRef.current = true
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
  const humanSeat = match?.human_seat ?? 1
  const seats = seatInfos(match)

  const myTurn = useMemo(() => {
    if (over) return false
    let pendingIdx = -1
    for (let i = 0; i < events.length; i++) {
      const ev = events[i]
      if (ev.type === 'your_turn' && Number(ev.player) === humanSeat) {
        pendingIdx = i
      } else if (pendingIdx >= 0) {
        if (
          ev.type === 'move' || ev.type === 'action' || ev.type === 'pass' ||
          ev.type === 'settle' || ev.type === 'match_end' || ev.type === 'error' ||
          ev.type === 'your_turn'
        ) {
          pendingIdx = -1
        }
      }
    }
    return pendingIdx >= 0
  }, [events, humanSeat, over])

  // 最近 your_turn.request（德州合法动作 / to_call）
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

  const sendMove = (move: Record<string, unknown>) => {
    if (!myTurn) return
    // 检查连接状态：socket 已关闭时给明确反馈，否则动作被静默吞掉（用户以为出了牌）。
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
      setError('连接已断开，动作未发送。请刷新页面重连。')
      return
    }
    wsRef.current.send(JSON.stringify(move))
    setTurnDeadline(null)
  }

  const isBoard = isBoardGame(gameId)
  const remainSec = turnDeadline ? Math.max(0, Math.ceil((turnDeadline - nowTs) / 1000)) : null

  // 结局摘要：经注册表 spec.reduce（消除 gameId=== if-chain）
  const endVm = useMemo(() => {
    if (!over || !events.length) return null
    return getGame(gameId).reduce(events as RawEvent[]) as
      | { matchWinner?: number | null; winner?: number | null; seats?: { net: number }[] }
      | null
  }, [over, events, gameId])

  const winnerLabel = resolveWinnerLabel(
    match,
    endInfo?.winner ?? (endVm && 'matchWinner' in endVm ? endVm.matchWinner : endVm && 'winner' in endVm ? endVm.winner : undefined),
    over,
    // 显示从 1 起计（后端 0 起计，DB CHECK 约束未变）。
    (seat) => (match ? seatHeaderLabel(match, seat as 0 | 1) : `座位 ${seat + 1}`),
  )

  return (
    <PageStub title="人类对战">
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <span className="text-sm text-muted-foreground">
          {gameLabel(gameId)} · 你坐【座位 {humanSeat + 1}】
        </span>
        {match && (
          // min-w-0 + truncate：长 Bot 名换行时压缩自身而非整行增高、挤压 canvas 高度
          <span className="min-w-0 max-w-full truncate text-sm text-muted-foreground">
            {seatHeaderLabel(match, 0)} vs {seatHeaderLabel(match, 1)}
          </span>
        )}
        <span className="text-sm">
          {over ? (
            <span className="font-medium text-foreground">
              对局结束 · 胜者：{winnerLabel}
              {endInfo?.reason ? `（${endInfo.reason}）` : ''}
            </span>
          ) : myTurn ? (
            <span className="flex items-center gap-1 font-medium text-success">
              <PlayCircle className="size-4" />
              轮到你落子
              {remainSec != null && (
                <span className="ml-1 inline-flex items-center gap-0.5 text-xs text-muted-foreground">
                  <Clock className="size-3" />{remainSec}s
                </span>
              )}
            </span>
          ) : (
            <span className="text-muted-foreground">等待中…</span>
          )}
        </span>
        {!isBoard && over && endVm && endVm.seats && (
          <span className="min-w-0 truncate font-mono text-xs text-muted-foreground">
            累计 {fmtNet(endVm.seats[0]?.net ?? 0)} / {fmtNet(endVm.seats[1]?.net ?? 0)}
          </span>
        )}
        <Link to={`/match/${id}`} className="ml-auto inline-flex shrink-0 items-center gap-1 text-sm font-medium text-primary hover:underline">
          查看回放
          <ArrowRight className="size-4" />
        </Link>
      </div>
      {error && <ErrorMsg msg={error} className="mb-3" />}
      {reconnecting && (
        <div className="mb-3 rounded-lg border border-warning/40 bg-warning/10 px-4 py-2 text-sm text-warning">
          连接已断开，正在重连…（请勿关闭页面）
        </div>
      )}

      {/* 棋类：左 canvas + 右对局进程（双栏，沿用紧凑布局） */}
      {isBoard && (
        <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_22rem]">
          <div className="space-y-3">
            {/* 棋类（board kind）：统一点击落子交互，消除 per-game gameId=== 分支 */}
            <MatchBoard
              gameId={gameId}
              events={events}
              seats={seats}
              onMove={(x, y) => sendMove({ x, y })}
              interactive={myTurn}
            />
          </div>
          <EventLogCard id={id} events={events} />
        </div>
      )}

      {/* 扑克：牌桌独占整行宽度（撑满主区）+ 动作面板独立成行 + 对局进程在下。
          比旧双栏（canvas 挤在 1fr 列里被 22rem 侧栏压到 ~688px）空间利用率更高。 */}
      {!isBoard && (
        <div className="space-y-4">
          <MatchBoard
            gameId={gameId}
            events={events}
            seats={seats}
            revealMode="showdown"
          />
          <HoldemActions
            disabled={!myTurn || over}
            legal={myTurn}
            request={turnRequest}
            onAct={(a, x) => {
              // 人类德州动作 → Botzone 标准整数（协议层只接受 -1/-2/0/>0）。
              //   fold → -1, allin → -2, check/call → 0, raise → 额外下注筹码（目标总额 − 本街已投）
              // 旧 {a,x} 格式已被 PR#130 移除（parse_response 只认裸整数/{"response":int}）。
              let resp = 0
              if (a === 'f') resp = -1
              else if (a === 'all') resp = -2
              else if (a === 'k' || a === 'c') resp = 0
              else if (a === 'r' && typeof x === 'number') {
                // raise：x=目标总额，需转成额外量（= 目标 − 本街已投 my_bet）。
                const myBet = Number(turnRequest?.my_bet ?? turnRequest?.b ?? deriveMyBet(turnRequest))
                resp = Math.max(1, Math.round(x - myBet))
              }
              sendMove({ response: resp })
            }}
          />
          <EventLogCard id={id} events={events} />
        </div>
      )}
    </PageStub>
  )
}

/** 对局进程事件日志卡（棋类/扑克分支共用，避免布局分叉后重复）。 */
function EventLogCard({ id, events }: { id?: string; events: Ev[] }) {
  return (
    <Card className="flex flex-col">
      <div className="border-b border-border px-4 py-2 text-sm font-semibold text-foreground">
        对局进程 <span className="text-xs font-normal text-muted-foreground">({events.length})</span>
      </div>
      <div className="max-h-[60vh] flex-1 overflow-y-auto p-2 text-xs">
        {events.length === 0 ? (
          <p className="py-6 text-center text-muted-foreground">等待对局开始…</p>
        ) : (
          events.slice().reverse().map((ev, i) => (
            <div key={i} className="flex items-center gap-2 rounded px-2 py-1 text-muted-foreground">
              <span className="w-16 shrink-0 opacity-60">{ev.type}</span>
              <span className="min-w-0 flex-1 truncate opacity-80">
                {ev.type === 'action' ? `座${ev.player} · ${ev.action}` : ''}
                {ev.type === 'move' ? `座${ev.player} · (${ev.x},${ev.y})` : ''}
                {ev.type === 'your_turn' ? '轮到你' : ''}
                {ev.type === 'settle' ? `赢家 座${(ev.winners as number[] | undefined)?.join('/')}` : ''}
                {ev.type === 'hand_start' ? `第 ${(Number(ev.hand) || 0) + 1} 手` : ''}
                {ev.type === 'match_end' ? `结束 · 胜者 ${ev.winner ?? '平'}` : ''}
                {ev.type === 'error' ? String(ev.message || '') : ''}
              </span>
            </div>
          ))
        )}
      </div>
      <Button asChild variant="ghost" size="sm" className="m-2 gap-1 self-start">
        <Link to={`/match/${id}`}>查看回放<ArrowRight className="size-3.5" /></Link>
      </Button>
    </Card>
  )
}

function HoldemActions({
  disabled,
  legal,
  request,
  onAct,
}: {
  disabled: boolean
  legal: boolean
  /** your_turn.request：紧凑协议字段 c/o/to/… 或展开字段 */
  request: Record<string, unknown> | null
  onAct: (a: string, x?: number) => void
}) {
  // 跟注额：优先用紧凑协议 to / 旧扩展 to_call（如有），否则从 Botzone 标准
  // history + my_chips 推导（协议已严格对齐 Botzone 11 字段，不再下发 to_call）。
  const toCall = Number(request?.to ?? request?.to_call ?? deriveToCall(request))
  const myChips = Number(request?.c ?? request?.my_chips ?? request?.chips ?? 20000)
  const canCheck = toCall === 0
  const canCall = toCall > 0 && myChips > 0
  const minRaise = Math.max(toCall * 2, 200) // 粗略默认；精确以引擎为准
  const [raiseTo, setRaiseTo] = useState(minRaise)

  useEffect(() => {
    setRaiseTo(Math.max(minRaise, toCall + 100))
  }, [minRaise, toCall])

  const dis = disabled || !legal
  return (
    <Card className="flex flex-row flex-wrap items-center gap-2 py-3">
      <Button type="button" variant="destructive" size="sm" disabled={dis} onClick={() => onAct('f')}>
        弃牌
      </Button>
      <Button type="button" variant="outline" size="sm" disabled={dis || !canCheck} onClick={() => onAct('k')}>
        过牌
      </Button>
      <Button type="button" variant="outline" size="sm" disabled={dis || !canCall} onClick={() => onAct('c')}>
        跟注{toCall > 0 ? ` ${toCall}` : ''}
      </Button>
      <Label className="flex min-w-0 items-center gap-2 text-sm text-muted-foreground">
        加注到
        <Input
          type="number"
          min={1}
          max={myChips}
          className="w-24"
          value={raiseTo}
          onChange={(e) => setRaiseTo(Number(e.target.value))}
        />
      </Label>
      <Button type="button" size="sm" disabled={dis || myChips <= 0} onClick={() => onAct('r', raiseTo)}>
        加注
      </Button>
      <Button type="button" size="sm" disabled={dis || myChips <= 0} onClick={() => onAct('all')}>
        All-in
      </Button>
      {legal && (
        <span className="flex min-w-0 items-center gap-1 text-xs text-success">
          <PlayCircle className="size-3.5" />
          轮到你{toCall > 0 ? ` · 需跟 ${toCall}` : ' · 可过牌'}
        </span>
      )}
    </Card>
  )
}
