/**
 * 游戏视图注册表类型（全面解耦 PR6）。
 *
 * 每款游戏一个 GameViewSpec，集中声明其前端表现属性：
 * - id/label/icon：基础元信息
 * - kind：'board'（棋类，步进式）| 'cards'（扑克，手牌式）—— 取代散落的 isBoard 布尔
 * - Board：棋盘/牌桌渲染组件
 * - reduce：事件归约函数（events → view model）
 *
 * 注：游戏规则参数（手数/棋盘/点阵）已钉死固定值，前端不再提供配置 UI
 * （原 defaultMatchConfig/configFields 字段已移除）。
 *
 * 通用组件（MatchBoard 等）经 getGame(id) 取 spec，不再 if game_id 分支。
 * 新增一款游戏 = 建 src/games/<game>/ 子包 + index.ts 注册一行。
 */
import type { ComponentType } from 'react'
import type { LucideIcon } from 'lucide-react'
import type { GameCanvasRenderer } from './canvas-types'

/**
 * 平台事件流的最小公共类型（对标后端 games/_board_protocol.py 的公共契约层）。
 *
 * 各游戏 reducer（games/<game>/reducer.ts）接受 RawEvent[]，内部按需断言字段。
 * 此类型上提到 games/base.ts，避免每个 reducer 各自定义一份（曾有三处重复 +
 * poker 版 type: string 必填 vs 棋类 type? 可选的不兼容）。统一为可选 type?。
 */
export type RawEvent = Record<string, unknown> & { type?: string }

/**
 * Canvas 渲染共享工具（holdem/gomoku/pencil 统一用）。
 */

/** canvas 渲染基线宽（与各游戏 layout 的 W0 一致）。W 变化时按 W/W0 缩放固定像素常量。 */
export const CANVAS_W0 = 900

/** 缩放因子：当前 canvas 宽 / 基线宽。用于把固定像素的字体/偏移/线宽等比放大。 */
export const scaleFactor = (canvasWidth: number): number => canvasWidth / CANVAS_W0

/**
 * 按当前 ctx 字体测量文本宽度，超出 maxWidth 时尾部加「…」截断。
 * 防止长 bot 名/胜负原因/比分越出 canvas 边界（三游戏 HUD 共用）。
 */
export function fitText(ctx: CanvasRenderingContext2D, text: string, maxWidth: number): string {
  if (ctx.measureText(text).width <= maxWidth) return text
  // 二分找最长前缀（保留 1 字符给「…」）
  let lo = 1, hi = text.length, ans = 1
  while (lo <= hi) {
    const mid = (lo + hi) >> 1
    if (ctx.measureText(text.slice(0, mid) + '…').width <= maxWidth) { ans = mid; lo = mid + 1 }
    else hi = mid - 1
  }
  return text.slice(0, ans) + '…'
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
  /** 座位着色（如 gomoku=['黑','白'], pencil=['红','蓝']）—— 取代渲染层按游戏名分支 */
  seatColors?: string[]
  /** 进度单位：hand=手数(扑克), move=步数(棋类) —— 取代 Home 等页面的游戏名分支 */
  progressUnit: 'hand' | 'move'
  /** 是否在顶栏显示比分（如 pencil=true，其余 false）—— 取代 MatchViewer 游戏名分支 */
  showScores?: boolean
}

/** Board 组件统一 props（各游戏 Board 须兼容）。 */
export interface BoardProps {
  vm: unknown
  /** 交互模式：点击落子（棋类人类对战用） */
  onMove?: (x: number, y: number) => void
  /** 扑克：亮牌模式 */
  revealMode?: 'all' | 'showdown'
}
