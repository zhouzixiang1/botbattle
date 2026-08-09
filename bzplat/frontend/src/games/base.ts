/**
 * 游戏视图注册表类型（全面解耦 PR6）。
 *
 * 每款游戏一个 GameViewSpec，集中声明其前端表现属性：
 * - id/label/icon：基础元信息
 * - kind：基础分类（仅供列表等轻量展示）
 * - Board：棋盘/牌桌渲染组件
 * - reduce：事件归约函数（events → view model）
 * - humanPlay：人类操作 UI、画布点击序列化与布局
 * - replay：胜者/进度/HUD/导航等回放展示能力
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

/** WebSocket 人类动作的唯一平台信封；具体 response 负载由各游戏定义。 */
export interface HumanActionEnvelope {
  response: unknown
}

/** 游戏自有的人类动作面板只通过该契约向平台提交完整信封。 */
export interface HumanActionPanelProps {
  disabled: boolean
  legal: boolean
  request: Record<string, unknown> | null
  onSubmit: (action: HumanActionEnvelope) => void
}

/** HUD/摘要只拿归约结果与通用座位身份，不让页面依赖具体 ViewModel。 */
export interface GameAuxiliaryProps {
  vm: unknown
  seats?: Array<{
    botName?: string
    ownerName?: string
    isHuman?: boolean
  }>
}

export interface HumanPlayViewSpec {
  /** 画面与日志的排布由游戏声明，不能由通用页猜测 board/cards。 */
  layout: 'canvas-with-log' | 'canvas-controls-log'
  turnLabel: string
  revealMode?: 'all' | 'showdown'
  /** 有画布点击动作的游戏必须把坐标封装成自己的完整 WS 动作。 */
  serializeBoardPick?: (x: number, y: number) => HumanActionEnvelope
  /** 非画布动作（如扑克）由游戏包提供完整输入组件及序列化。 */
  ActionPanel?: ComponentType<HumanActionPanelProps>
  /** 终局附加摘要；返回 null 表示该游戏无需额外摘要。 */
  endSummary?: (vm: unknown) => string | null
}

export interface ReplayNavigationSpec {
  unitLabel: string
  boundaries: (events: RawEvent[]) => number[]
}

export interface ReplayViewSpec {
  /** 回放主画面与时序的排布由游戏声明。 */
  layout: 'wide' | 'with-timeline'
  /** 从该游戏 ViewModel 读取当前进度；null 时由持久化结果兜底。 */
  progress: (vm: unknown) => number | null
  /** 可选的对阵摘要（例如德州累计筹码）。 */
  Summary?: ComponentType<GameAuxiliaryProps>
  /** 可选的画面辅助 HUD（例如点格棋比分与棋钟）。 */
  Hud?: ComponentType<GameAuxiliaryProps>
  /** 可选的分段导航（例如德州逐手跳转）。 */
  navigation?: ReplayNavigationSpec
}

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
  /** 画布宽高比；未声明时使用通用 3:2。牌桌可用更紧凑的 16:9。 */
  canvasAspectRatio?: number
  /** 座位着色（如 gomoku=['黑','白'], pencil=['红','蓝']）—— 取代渲染层按游戏名分支 */
  seatColors?: string[]
  /** 进度单位：hand=手数(扑克), move=步数(棋类) —— 取代 Home 等页面的游戏名分支 */
  progressUnit: 'hand' | 'move'
  /** 赛事等列表使用的固定对局规模文案。 */
  matchFormatLabel: string
  /** 从游戏 ViewModel 读取胜者；通用页不得猜测 winner/matchWinner 字段。 */
  winner: (vm: unknown) => number | null | undefined
  /** 游戏事件的人类可读描述；通用页不解释扑克/棋类专属事件字段。 */
  describeEvent: (event: RawEvent) => string
  /** 人类对战输入与布局契约。 */
  humanPlay: HumanPlayViewSpec
  /** 观赛/回放辅助展示契约。 */
  replay: ReplayViewSpec
}

/** Board 组件统一 props（各游戏 Board 须兼容）。 */
export interface BoardProps {
  vm: unknown
  /** 交互模式：点击落子（棋类人类对战用） */
  onMove?: (x: number, y: number) => void
  /** 扑克：亮牌模式 */
  revealMode?: 'all' | 'showdown'
}
