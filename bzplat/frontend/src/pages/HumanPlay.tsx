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

export default function HumanPlay() {
  const { id } = useParams<{ id: string }>()
  const [match, setMatch] = useState<MatchSeatRow | null>(null)
  const [events, setEvents] = useState<Ev[]>([])
  const [over, setOver] = useState(false)
  const [error, setError] = useState('')
  const [endInfo, setEndInfo] = useState<{ winner?: number | null; earnings_a?: number; earnings_b?: number; reason?: string } | null>(null)
  const [turnDeadline, setTurnDeadline] = useState<number | null>(null)
  const [nowTs, setNowTs] = useState(() => Date.now())
  const wsRef = useRef<WebSocket | null>(null)

  useEffect(() => {
    if (!id) return
    if (wsRef.current) {
      wsRef.current.close()
      wsRef.current = null
    }
    const ws = new WebSocket(playWsUrl(id))
    wsRef.current = ws
    ws.onmessage = (e) => {
      try {
        const ev = JSON.parse(e.data)
        if (ev.type === 'snapshot') {
          setMatch(ev.match || {})
          setEvents(ev.events || [])
        } else if (ev.type === 'match_end' || ev.type === 'error') {
          setEvents((prev) => [...prev, ev])
          setOver(true)
          setTurnDeadline(null)
          setEndInfo({
            winner: ev.winner as number | null | undefined,
            earnings_a: ev.earnings_a != null ? Number(ev.earnings_a) : undefined,
            earnings_b: ev.earnings_b != null ? Number(ev.earnings_b) : undefined,
            reason: ev.reason || ev.message,
          })
          setMatch((prev) => prev ? {
            ...prev,
            status: 'completed',
            winner: ev.winner as number | null | undefined,
            earnings_a: ev.earnings_a != null ? Number(ev.earnings_a) : prev.earnings_a,
            earnings_b: ev.earnings_b != null ? Number(ev.earnings_b) : prev.earnings_b,
          } : prev)
        } else {
          setEvents((prev) => [...prev, ev])
          if (ev.type === 'your_turn') {
            setTurnDeadline(Date.now() + HUMAN_TIMEOUT_SEC * 1000)
          }
        }
      } catch {
        /* ignore parse error */
      }
    }
    ws.onerror = () => setError('连接异常')
    ws.onclose = () => { /* 服务端在 match_end 后会关闭 */ }
    return () => {
      ws.close()
      wsRef.current = null
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
    wsRef.current?.send(JSON.stringify(move))
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
    (seat) => (match ? seatHeaderLabel(match, seat as 0 | 1) : `座位 ${seat}`),
  )

  return (
    <PageStub title="人类对战">
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <span className="text-sm text-muted-foreground">
          {gameLabel(gameId)} · 你坐【座位 {humanSeat}】
        </span>
        {match && (
          <span className="text-sm text-muted-foreground">
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
          <span className="font-mono text-xs text-muted-foreground">
            累计 {fmtNet(endVm.seats[0]?.net ?? 0)} / {fmtNet(endVm.seats[1]?.net ?? 0)}
          </span>
        )}
        <Link to={`/match/${id}`} className="ml-auto inline-flex items-center gap-1 text-sm font-medium text-primary hover:underline">
          查看回放
          <ArrowRight className="size-4" />
        </Link>
      </div>
      {error && <ErrorMsg msg={error} className="mb-3" />}

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_22rem]">
        <div className="space-y-3">
          {/* 棋类（board kind）：统一点击落子交互，消除 per-game gameId=== 分支 */}
          {isBoard && (
            <MatchBoard
              gameId={gameId}
              events={events}
              seats={seats}
              onMove={(x, y) => sendMove({ x, y })}
              interactive={myTurn}
            />
          )}
          {/* 扑克（cards kind）：牌桌 + 动作面板 */}
          {!isBoard && (
            <div className="relative">
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
                onAct={(a, x) => sendMove(x !== undefined ? { a, x } : { a })}
              />
            </div>
          )}
        </div>

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
      </div>
    </PageStub>
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
  // 协议：to=跟注额；c=己方筹码；展开字段可能用 to_call / chips
  const toCall = Number(request?.to ?? request?.to_call ?? 0)
  const myChips = Number(request?.c ?? request?.chips ?? 20000)
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
      <Label className="flex items-center gap-2 text-sm text-muted-foreground">
        加注到
        <Input
          type="number"
          min={1}
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
        <span className="flex items-center gap-1 text-xs text-success">
          <PlayCircle className="size-3.5" />
          轮到你{toCall > 0 ? ` · 需跟 ${toCall}` : ' · 可过牌'}
        </span>
      )}
    </Card>
  )
}
