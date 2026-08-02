import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { PlayCircle, ArrowRight } from 'lucide-react'
import PageStub from '@/components/PageStub'
import MatchBoard from '@/components/MatchBoard'
import { reduceEvents } from '@/components/poker/useMatchState'
import { Card } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { ErrorMsg } from '@/components/ui/status'
import { playWsUrl } from '@/api'
import { gameLabel, normalizeGameId } from '@/lib/games'
import { isBoardGame } from '@/games'

type Ev = Record<string, unknown> & { type?: string }

interface MatchRow {
  game_id?: string
  human_seat?: number | null
  status?: string
  match_type?: string
}

export default function HumanPlay() {
  const { id } = useParams<{ id: string }>()
  const [match, setMatch] = useState<MatchRow | null>(null)
  const [events, setEvents] = useState<Ev[]>([])
  const [myTurn, setMyTurn] = useState(false)
  const [over, setOver] = useState(false)
  const [error, setError] = useState('')
  const wsRef = useRef<WebSocket | null>(null)

  useEffect(() => {
    if (!id) return
    const ws = new WebSocket(playWsUrl(id))
    wsRef.current = ws
    ws.onmessage = (e) => {
      try {
        const ev = JSON.parse(e.data)
        if (ev.type === 'snapshot') {
          setMatch(ev.match || {})
          setEvents(ev.events || [])
        } else if (ev.type === 'your_turn') {
          setMyTurn(true)
          setEvents((prev) => [...prev, ev])
        } else if (ev.type === 'match_end' || ev.type === 'error') {
          setEvents((prev) => [...prev, ev])
          setMyTurn(false)
          setOver(true)
        } else {
          setEvents((prev) => [...prev, ev])
          // 收到任何非 your_turn 事件，暂时关闭输入（直到下一个 your_turn）
          if (ev.type === 'move' || ev.type === 'action' || ev.type === 'pass') {
            setMyTurn(false)
          }
        }
      } catch {
        /* ignore parse error */
      }
    }
    ws.onerror = () => setError('连接异常')
    ws.onclose = () => {
      /* 服务端在 match_end 后会关闭 */
    }
    return () => ws.close()
  }, [id])

  const gameId = normalizeGameId(match?.game_id)
  const humanSeat = match?.human_seat ?? 1

  const sendMove = (move: Record<string, unknown>) => {
    if (!myTurn) return
    wsRef.current?.send(JSON.stringify(move))
    setMyTurn(false)
  }

  // 德州 viewmodel（用于判定 toAct）
  const pokerVm = useMemo(
    () => reduceEvents(events as unknown as Parameters<typeof reduceEvents>[0]),
    [events],
  )
  const isBoard = isBoardGame(gameId)

  return (
    <PageStub title="人类对战">
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <span className="text-sm text-muted-foreground">
          {gameLabel(gameId)} · 你坐【座位 {humanSeat}】
        </span>
        <span className="text-sm">
          {over ? (
            <span className="text-muted-foreground">对局结束</span>
          ) : myTurn ? (
            <span className="flex items-center gap-1 font-medium text-success">
              <PlayCircle className="size-4" />
              轮到你落子
            </span>
          ) : (
            <span className="text-muted-foreground">等待中…</span>
          )}
        </span>
        <Link to={`/match/${id}`} className="ml-auto inline-flex items-center gap-1 text-sm font-medium text-primary hover:underline">
          查看回放
          <ArrowRight className="size-4" />
        </Link>
      </div>
      {error && <ErrorMsg msg={error} className="mb-3" />}

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_22rem]">
        {/* 左：棋盘/牌桌 + 动作按钮 */}
        <div className="space-y-3">
          {gameId === 'gomoku' && (
            <MatchBoard gameId="gomoku" events={events} onMove={(x, y) => sendMove({ x, y })} interactive={myTurn} />
          )}
          {gameId === 'pencil' && (
            <MatchBoard gameId="pencil" events={events} onMove={(x, y) => sendMove({ x, y })} interactive={myTurn} />
          )}
          {gameId === 'holdem' && (
            <>
              <MatchBoard gameId="holdem" events={events} revealMode="all" />
              <HoldemActions
                disabled={!myTurn || over}
                legal={pokerVm.toAct === humanSeat}
                onAct={(a, x) => sendMove(x !== undefined ? { a, x } : { a })}
              />
            </>
          )}
          {!isBoard && gameId !== 'holdem' && (
            <p className="text-sm text-muted-foreground">未知游戏：{match?.game_id}</p>
          )}
        </div>

        {/* 右：事件日志 */}
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
  onAct,
}: {
  disabled: boolean
  legal: boolean
  onAct: (a: string, x?: number) => void
}) {
  const [raiseTo, setRaiseTo] = useState(200)
  const dis = disabled || !legal
  return (
    <Card className="flex flex-row flex-wrap items-center gap-2 py-3">
      <Button type="button" variant="destructive" size="sm" disabled={dis} onClick={() => onAct('f')}>
        弃牌
      </Button>
      <Button type="button" variant="outline" size="sm" disabled={dis} onClick={() => onAct('k')}>
        过牌
      </Button>
      <Button type="button" variant="outline" size="sm" disabled={dis} onClick={() => onAct('c')}>
        跟注
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
      <Button type="button" size="sm" disabled={dis} onClick={() => onAct('r', raiseTo)}>
        加注
      </Button>
      <Button type="button" size="sm" disabled={dis} onClick={() => onAct('all')}>
        All-in
      </Button>
      {legal && (
        <span className="flex items-center gap-1 text-xs text-success">
          <PlayCircle className="size-3.5" />
          轮到你
        </span>
      )}
    </Card>
  )
}
