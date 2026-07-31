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

  if (gid === 'gomoku') {
    return <GomokuBoard vm={reduceGomokuEvents(events as BoardRaw[])} onMove={handler} />
  }
  if (gid === 'pencil') {
    return <PencilBoard vm={reducePencilEvents(events as BoardRaw[])} onMove={handler} />
  }
  const vm = reduceEvents(events as PokerRaw[])
  return <PokerTable vm={vm} revealMode={revealMode} />
}
