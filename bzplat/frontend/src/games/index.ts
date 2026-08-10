/**
 * 游戏视图注册表（全面解耦 PR6 单一真相）。
 *
 * 注册三款游戏的 GameViewSpec，暴露便捷函数。通用组件（MatchBoard 等）经
 * getGame(id) 取 spec，不再 if game_id 分支。
 *
 * lib/games.ts 降为转发薄层（保 16 个现存 import 可用）。
 * 新增一款游戏 = 建 src/games/<game>/ 子包 + 在此注册一行。
 */
import { CircleAlert, type LucideIcon } from 'lucide-react'
import type { GameViewSpec } from './base'
import { holdemSpec } from './holdem'
import { gomokuSpec } from './gomoku'
import { pencilSpec } from './pencil'

export type {
  GameViewSpec,
  BoardProps,
  GameAuxiliaryProps,
  HumanActionEnvelope,
  HumanActionPanelProps,
  TerminalReasonPresentation,
  TerminalReasonResolver,
  TerminalReasonTone,
} from './base'
export { resolveTerminalReason } from './reasons'

/** 全部已注册游戏规格。 */
export const GAMES: GameViewSpec[] = [holdemSpec, gomokuSpec, pencilSpec]

/** 合法 GameId 联合（从注册表派生）。 */
export type GameId = (typeof GAMES)[number]['id']

const BY_ID: Record<string, GameViewSpec> = Object.fromEntries(
  GAMES.map((g) => [g.id, g]),
)

/** 仅做格式规整，不把缺失/未知 id 改写成任一已注册游戏。 */
export function normalizeGameId(id: string | null | undefined): string {
  return (id ?? '').trim().toLowerCase()
}

/** 安全查询游戏规格；未知 id 的调用方必须渲染明确错误语义。 */
export function findGame(id: string | null | undefined): GameViewSpec | undefined {
  return BY_ID[normalizeGameId(id)]
}

/** 取已注册游戏规格；未知 id 是数据/契约错误，禁止静默伪装成 holdem。 */
export function getGame(id: string | null | undefined): GameViewSpec {
  const gid = normalizeGameId(id)
  const spec = BY_ID[gid]
  if (!spec) {
    throw new Error(gid ? `不支持的游戏：${gid}` : '对局缺少 game_id')
  }
  return spec
}

export function unsupportedGameLabel(id: string | null | undefined): string {
  const gid = normalizeGameId(id)
  return gid ? `不支持的游戏（${gid}）` : '未知游戏（缺少 game_id）'
}

/** 游戏显示名。 */
export function gameLabel(id: string | null | undefined): string {
  return findGame(id)?.label ?? unsupportedGameLabel(id)
}

/** 游戏图标。 */
export function gameIcon(id: string | null | undefined): LucideIcon {
  return findGame(id)?.icon ?? CircleAlert
}

/** GAME_LABEL 映射（向后兼容 lib/games.ts 导出）。 */
export const GAME_LABEL: Record<string, string> = Object.fromEntries(
  GAMES.map((g) => [g.id, g.label]),
)
