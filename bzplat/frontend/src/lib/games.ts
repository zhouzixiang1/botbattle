/** 多游戏常量与标签 */
export type GameId = 'holdem' | 'gomoku' | 'pencil'

export const GAMES: { id: GameId; label: string }[] = [
  { id: 'holdem', label: '德州扑克' },
  { id: 'gomoku', label: '五子棋' },
  { id: 'pencil', label: '点格棋' },
]

export const GAME_LABEL: Record<string, string> = Object.fromEntries(
  GAMES.map((g) => [g.id, g.label]),
)

export function gameLabel(id: string | null | undefined): string {
  if (!id) return GAME_LABEL.holdem
  return GAME_LABEL[id] || id
}

export function normalizeGameId(id: string | null | undefined): GameId {
  if (id === 'gomoku' || id === 'pencil' || id === 'holdem') return id
  return 'holdem'
}
