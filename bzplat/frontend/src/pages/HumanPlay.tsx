import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import PageStub from '../components/PageStub'
import MatchBoard from '../components/MatchBoard'
import { reduceEvents } from '../components/poker/useMatchState'
import { playWsUrl } from '../api'
import { gameLabel, normalizeGameId } from '../lib/games'

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
  const isBoard = gameId === 'gomoku' || gameId === 'pencil'

  return (
    <PageStub title="人类对战">
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <span className="text-sm text-slate-500">
          {gameLabel(gameId)} · 你坐【座位 {humanSeat}】
        </span>
        <span className="text-sm">
          {over ? (
            <span className="text-slate-500">对局结束</span>
          ) : myTurn ? (
            <span className="font-medium text-emerald-600">轮到你落子</span>
          ) : (
            <span className="text-slate-400">等待中…</span>
          )}
        </span>
        <Link to={`/match/${id}`} className="ml-auto text-sm text-brand-600 hover:underline">
          查看回放 →
        </Link>
      </div>
      {error && <p className="mb-3 text-sm text-error-500">{error}</p>}

      {/* 棋类：可点击棋盘；扑克：动作按钮栏 */}
      {gameId === 'gomoku' && (
        <MatchBoard gameId="gomoku" events={events} onMove={(x, y) => sendMove({ x, y })} interactive={myTurn} />
      )}
      {gameId === 'pencil' && (
        <MatchBoard gameId="pencil" events={events} onMove={(x, y) => sendMove({ x, y })} interactive={myTurn} />
      )}
      {gameId === 'holdem' && (
        <div className="space-y-3">
          <MatchBoard gameId="holdem" events={events} revealMode="all" />
          <HoldemActions
            disabled={!myTurn || over}
            legal={pokerVm.toAct === humanSeat}
            onAct={(a, x) => sendMove(x !== undefined ? { a, x } : { a })}
          />
        </div>
      )}
      {!isBoard && gameId !== 'holdem' && (
        <p className="text-sm text-slate-400">未知游戏：{match?.game_id}</p>
      )}
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
  const btn = 'rounded-lg border px-3 py-1.5 text-sm disabled:opacity-40'
  return (
    <div className="flex flex-wrap items-center gap-2 rounded-xl border border-slate-200 bg-white p-3">
      <button type="button" disabled={dis} onClick={() => onAct('f')} className={`${btn} border-error-300 text-error-600 hover:bg-error-50`}>弃牌</button>
      <button type="button" disabled={dis} onClick={() => onAct('k')} className={`${btn} border-slate-300 text-slate-600 hover:bg-slate-50`}>过牌</button>
      <button type="button" disabled={dis} onClick={() => onAct('c')} className={`${btn} border-slate-300 text-slate-600 hover:bg-slate-50`}>跟注</button>
      <label className="flex items-center gap-1 text-sm text-slate-600">
        加注到
        <input
          type="number" min={1} className="w-24 rounded-lg border border-slate-300 px-2 py-1.5"
          value={raiseTo} onChange={(e) => setRaiseTo(Number(e.target.value))}
        />
      </label>
      <button type="button" disabled={dis} onClick={() => onAct('r', raiseTo)} className={`${btn} border-brand-300 text-brand-600 hover:bg-brand-50`}>加注</button>
      <button type="button" disabled={dis} onClick={() => onAct('all')} className={`${btn} border-brand-300 text-brand-600 hover:bg-brand-50`}>All-in</button>
      {legal && <span className="text-xs text-emerald-600">轮到你</span>}
    </div>
  )
}
