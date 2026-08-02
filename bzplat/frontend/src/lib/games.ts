/** 多游戏常量与标签（全面解耦 PR6：转发到 src/games/ 注册表）。

本文件降为转发薄层——真相在 src/games/index.ts。保 16 个现存 import 可用：
GAMES / GAME_LABEL / gameLabel / gameIcon / normalizeGameId / matchTypeBadge / GameId。
新代码应直接 import from '@/games'。
*/
import type { LucideIcon } from 'lucide-react'
export type { GameId } from '@/games'
export { GAMES, GAME_LABEL, gameLabel, gameIcon, normalizeGameId } from '@/games'
import { GAMES as _GAMES } from '@/games'

// re-export GAMES 的形状（{id,label,icon}）兼容旧解构——注册表的 GAMES 是 GameViewSpec[]
// 含更多字段，旧代码只取 id/label/icon，结构兼容。

/** 对局类型徽章（统一配色，无散落 sky/violet）—— 通用赛事概念，留在此处。 */
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

// 保旧 default export 形状（如有）：导出图标类型供 type-only import
export type { LucideIcon }
// 标记 _GAMES 已用（避免 tree-shake 移除转发）
void _GAMES
