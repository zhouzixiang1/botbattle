import { useMemo } from 'react'
import { getGame } from '@/games'
import { normalizeGameId } from '@/lib/games'

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
  const spec = getGame(gid)
  // 仅在交互模式且有 onMove 时启用点击
  const handler = interactive && onMove ? onMove : undefined

  // 经注册表归约（消除 per-game if-chain 与各自 useMemo）
  const vm = useMemo(() => (events.length ? spec.reduce(events) : null), [spec, events])

  if (!vm) return null
  const Board = spec.Board
  // 棋类传 onMove（点击落子）；扑克传 revealMode
  return <Board vm={vm} onMove={handler} revealMode={revealMode} />
}
