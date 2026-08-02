/**
 * 游戏视图注册表（全面解耦 PR6 单一真相）。
 *
 * 注册三款游戏的 GameViewSpec，暴露便捷函数。通用组件（MatchBoard 等）经
 * getGame(id) 取 spec，不再 if game_id 分支。
 *
 * lib/games.ts 降为转发薄层（保 16 个现存 import 可用）。
 * 新增一款游戏 = 建 src/games/<game>/ 子包 + 在此注册一行。
 */
import type { LucideIcon } from 'lucide-react'
import type { GameViewSpec } from './base'
import { holdemSpec } from './holdem'
import { gomokuSpec } from './gomoku'
import { pencilSpec } from './pencil'

export type { GameViewSpec, MatchConfigField, BoardProps } from './base'

/** 全部已注册游戏规格。 */
export const GAMES: GameViewSpec[] = [holdemSpec, gomokuSpec, pencilSpec]

/** 合法 GameId 联合（从注册表派生）。 */
export type GameId = (typeof GAMES)[number]['id']

const BY_ID: Record<string, GameViewSpec> = Object.fromEntries(
  GAMES.map((g) => [g.id, g]),
)

/** 取游戏规格；未知 id 回退 holdem（前端容错，不抛）。 */
export function getGame(id: string | null | undefined): GameViewSpec {
  const gid = normalizeGameId(id)
  return BY_ID[gid] ?? holdemSpec
}

/** 规整 game_id（小写）；未知/空回退 holdem（保旧 normalizeGameId 语义）。 */
export function normalizeGameId(id: string | null | undefined): GameId {
  const gid = (id || '').trim().toLowerCase()
  if (gid === 'gomoku' || gid === 'pencil' || gid === 'holdem') return gid
  return 'holdem'
}

/** 游戏显示名。 */
export function gameLabel(id: string | null | undefined): string {
  return getGame(id).label
}

/** 游戏图标。 */
export function gameIcon(id: string | null | undefined): LucideIcon {
  return getGame(id).icon
}

/** 该游戏是否棋类（步进式，取代散落的 isBoard 布尔）。 */
export function isBoardGame(id: string | null | undefined): boolean {
  return getGame(id).kind === 'board'
}

/** 该游戏的默认 match_config（深拷贝，取代散落 {hands:70}）。 */
export function defaultMatchConfig(id: string | null | undefined): Record<string, number> {
  return { ...getGame(id).defaultMatchConfig }
}

/** GAME_LABEL 映射（向后兼容 lib/games.ts 导出）。 */
export const GAME_LABEL: Record<string, string> = Object.fromEntries(
  GAMES.map((g) => [g.id, g.label]),
)
