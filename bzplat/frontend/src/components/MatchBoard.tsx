import { useMemo } from 'react'
import PokerTable from './poker/PokerTable'
import { reduceEvents, type RawEvent as PokerRaw } from './poker/useMatchState'
import GomokuBoard from './gomoku/GomokuBoard'
import { reduceGomokuEvents, type RawEvent as BoardRaw } from './gomoku/useGomokuState'
import PencilBoard from './pencil/PencilBoard'
import { reducePencilEvents } from './pencil/usePencilState'
import { normalizeGameId } from '../lib/games'

type Ev = Record<string, unknown> & { type?: string }

export default function MatchBoard({
  gameId,
  events,
  revealMode = 'all',
  onMove,
  interactive = false,
}: {
  gameId?: string | null
  events: Ev[]
  revealMode?: 'all' | 'showdown'
  onMove?: (x: number, y: number) => void
  interactive?: boolean
}) {
  const gid = normalizeGameId(gameId)
  // 仅在交互模式且有 onMove 时启用点击
  const handler = interactive && onMove ? onMove : undefined

  // 缓存归约结果：定速回放/实时观赛时每步都会重渲染，避免对全部历史事件重复 reduce（O(n)/帧）。
  // 依赖 events（已切片到当前步）+ revealMode。
  const gomokuVm = useMemo(
    () => (gid === 'gomoku' ? reduceGomokuEvents(events as BoardRaw[]) : null),
    [gid, events],
  )
  const pencilVm = useMemo(
    () => (gid === 'pencil' ? reducePencilEvents(events as BoardRaw[]) : null),
    [gid, events],
  )
  const pokerVm = useMemo(
    () => (gid === 'holdem' || (!gid && events.length) ? reduceEvents(events as PokerRaw[]) : null),
    [gid, events],
  )

  if (gid === 'gomoku') {
    return <GomokuBoard vm={gomokuVm!} onMove={handler} />
  }
  if (gid === 'pencil') {
    return <PencilBoard vm={pencilVm!} onMove={handler} />
  }
  return <PokerTable vm={pokerVm!} revealMode={revealMode} />
}

