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
}: {
  gameId?: string | null
  events: Ev[]
  revealMode?: 'all' | 'showdown'
}) {
  const gid = normalizeGameId(gameId)

  if (gid === 'gomoku') {
    return <GomokuBoard vm={reduceGomokuEvents(events as BoardRaw[])} />
  }
  if (gid === 'pencil') {
    return <PencilBoard vm={reducePencilEvents(events as BoardRaw[])} />
  }
  const vm = reduceEvents(events as PokerRaw[])
  return <PokerTable vm={vm} revealMode={revealMode} />
}
