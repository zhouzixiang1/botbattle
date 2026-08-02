import { useMemo } from 'react'
import { getGame } from '@/games'
import { normalizeGameId } from '@/lib/games'
import GameCanvas from './GameCanvas'
import type { SeatInfo } from '@/games/canvas-types'
import type { RawEvent } from '@/games/base'

type Ev = Record<string, unknown> & { type?: string }

export default function MatchBoard({
  gameId,
  events,
  revealMode = 'all',
  onMove,
  interactive = false,
  seats,
}: {
  gameId?: string | null
  events: Ev[]
  revealMode?: 'all' | 'showdown'
  onMove?: (x: number, y: number) => void
  interactive?: boolean
  /** 座位身份（扑克 canvas 用来绘制座位标签）；DOM Board 路径忽略此 prop */
  seats?: SeatInfo[]
}) {
  const gid = normalizeGameId(gameId)
  const spec = getGame(gid)

  // 该游戏已接入 canvas 渲染器 → 优先用 canvas 绘制（三游戏都走这）
  if (spec.CanvasRenderer) {
    return (
      <GameCanvas
        gameId={gid}
        events={events as unknown as RawEvent[]}
        seats={seats}
        revealMode={revealMode}
        onMove={interactive ? onMove : undefined}
        interactive={interactive}
      />
    )
  }

  // 回退 DOM Board（gomoku/pencil 暂时走这，PR-B/C 加 CanvasRenderer 后自动切）
  // 仅在交互模式且有 onMove 时启用点击
  const handler = interactive && onMove ? onMove : undefined

  // 经注册表归约（消除 per-game if-chain 与各自 useMemo）
  const vm = useMemo(() => (events.length ? spec.reduce(events) : null), [spec, events])

  if (!vm) return null
  const Board = spec.Board
  // 棋类传 onMove（点击落子）；扑克传 revealMode
  return <Board vm={vm} onMove={handler} revealMode={revealMode} />
}
