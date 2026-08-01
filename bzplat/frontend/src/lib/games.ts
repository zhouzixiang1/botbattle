/** 多游戏常量与标签 */
import { Spade, Circle, Grid3x3, type LucideIcon } from 'lucide-react'

export type GameId = 'holdem' | 'gomoku' | 'pencil'

export const GAMES: { id: GameId; label: string; icon: LucideIcon }[] = [
  { id: 'holdem', label: '德州扑克', icon: Spade },
  { id: 'gomoku', label: '五子棋', icon: Grid3x3 },
  { id: 'pencil', label: '点格棋', icon: Circle },
]

export const GAME_LABEL: Record<string, string> = Object.fromEntries(
  GAMES.map((g) => [g.id, g.label]),
)

export function gameLabel(id: string | null | undefined): string {
  if (!id) return GAME_LABEL.holdem
  return GAME_LABEL[id] || id
}

export function gameIcon(id: string | null | undefined): LucideIcon {
  return GAMES.find((g) => g.id === id)?.icon ?? Spade
}

export function normalizeGameId(id: string | null | undefined): GameId {
  if (id === 'gomoku' || id === 'pencil' || id === 'holdem') return id
  return 'holdem'
}

/** 对局类型徽章（统一配色，无散落 sky/violet） */
export function matchTypeBadge(t: string | undefined): { label: string; cls: string } | null {
  switch (t) {
    case 'ladder':
      return { label: '后台', cls: 'bg-sky-100 text-sky-700 dark:bg-sky-500/15 dark:text-sky-300' }
    case 'human':
      return { label: '人类', cls: 'bg-amber-100 text-amber-700 dark:bg-amber-500/15 dark:text-amber-300' }
    case 'contest':
      return { label: '赛事', cls: 'bg-primary/15 text-primary' }
    case 'table':
      return { label: '桌台', cls: 'bg-muted text-muted-foreground' }
    case 'challenge':
      return { label: '挑战', cls: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-500/15 dark:text-emerald-300' }
    default:
      return null
  }
}
