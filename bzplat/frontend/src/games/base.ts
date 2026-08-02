/**
 * 游戏视图注册表类型（全面解耦 PR6）。
 *
 * 每款游戏一个 GameViewSpec，集中声明其前端表现属性：
 * - id/label/icon：基础元信息
 * - kind：'board'（棋类，步进式）| 'cards'（扑克，手牌式）—— 取代散落的 isBoard 布尔
 * - Board：棋盘/牌桌渲染组件
 * - reduce：事件归约函数（events → view model）
 * - defaultMatchConfig / configFields：对局参数默认值与可调字段（取代散落的 {hands:70} 等）
 *
 * 通用组件（MatchBoard 等）经 getGame(id) 取 spec，不再 if game_id 分支。
 * 新增一款游戏 = 建 src/games/<game>/ 子包 + index.ts 注册一行。
 */
import type { ComponentType } from 'react'
import type { LucideIcon } from 'lucide-react'
import type { GameCanvasRenderer } from './canvas-types'

/** 对局参数字段定义（取代散落的 per-game 配置 UI 分支）。 */
export interface MatchConfigField {
  /** match_config 里的 key（如 'hands' / 'n_dots'） */
  key: string
  /** 显示名 */
  label: string
  /** 默认值 */
  default: number
  /** 最小值 */
  min: number
  /** 最大值 */
  max: number
}

/** 一款游戏的前端视图规格。 */
export interface GameViewSpec {
  /** 游戏 id（与后端 game_id 一致） */
  id: string
  /** 中文显示名 */
  label: string
  /** 图标 */
  icon: LucideIcon
  /** 游戏类型：棋类（步进）/ 扑克（手牌）—— 取代 isBoard */
  kind: 'board' | 'cards'
  /** 棋盘/牌桌渲染组件 */
  Board: ComponentType<BoardProps>
  /** 事件归约：events → view model（传给 Board 的 vm）。events 为宽松类型，各游戏 reducer 内部自行断言。 */
  reduce: (events: Record<string, unknown>[]) => unknown
  // 注：各游戏 reducer（reduceEvents/reduceGomokuEvents/reducePencilEvents）接受更具体
  // 的 RawEvent[]，注册时经类型断言适配（结构兼容，运行时无影响）。
  /** canvas 渲染器（可选）。若提供，GameCanvas 优先用它绘制，替代默认 DOM Board。 */
  CanvasRenderer?: GameCanvasRenderer
  /** 默认 match_config（取代散落 {hands:70}/{n_dots:11}） */
  defaultMatchConfig: Record<string, number>
  /** 可调对局参数字段（取代散落配置 UI 分支） */
  configFields: MatchConfigField[]
}

/** Board 组件统一 props（各游戏 Board 须兼容）。 */
export interface BoardProps {
  vm: unknown
  /** 交互模式：点击落子（棋类人类对战用） */
  onMove?: (x: number, y: number) => void
  /** 扑克：亮牌模式 */
  revealMode?: 'all' | 'showdown'
}
