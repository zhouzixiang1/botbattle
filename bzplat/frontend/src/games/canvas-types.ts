/**
 * Canvas↔React 桥接契约（canvas+GSAP 视觉重写基建）。
 *
 * 每款游戏的 canvas 渲染器实现 `GameCanvasRenderer<S>`：把后端 events 归约成
 * 归一化场景、计算两帧差分以决定触发何种动画、按插值 t 在 prev↔next 间绘制。
 *
 * `GameViewSpec.CanvasRenderer` 是可选字段：若提供，GameCanvas 组件优先用它绘制，
 * 替代默认的 DOM Board。无该字段的游戏继续走原有 DOM 渲染路径（gomoku/pencil 等）。
 */
import type { RawEvent } from '@/games/base'

/** 一个游戏的归一化场景（由 toScene 从 events 归约）。每游戏自定义具体结构。 */
export type Scene = Record<string, unknown>

/** 场景差分：哪些是「新」的需动画。每游戏自定义结构（payload 为松类型）。 */
export type SceneDelta = { animation: 'deal' | 'place' | 'settle' | 'none'; payload?: unknown }

export interface DrawOpts {
  width: number
  height: number
  /** 可选：座位身份（Bot 名/用户名），用于绘制座位标签 */
  seats?: SeatInfo[]
  /**
   * 底牌揭示：
   * - all：有牌就亮（Bot 观赛）
   * - showdown：仅摊牌/对局结束/人类己方亮（人类对战防透视）
   */
  revealMode?: 'all' | 'showdown'
  /**
   * 当前鼠标命中的合法画布动作。仅交互态存在；游戏 renderer 可用它绘制
   * hover 预览，但不得据此改变权威场景。
   */
  hoverPick?: { x: number; y: number } | null
}
export interface SeatInfo {
  botName?: string
  ownerName?: string
  ownerDisplayName?: string
  isHuman?: boolean
}

/** canvas 坐标 → 游戏落子坐标（棋类人类对战用）。扑克无此方法。
 * scene 形参为宽 Scene 类型，实现内部按需 cast（规避 GameCanvasRenderer<Scene> 赋值时的逆变不兼容）。 */
export type PickFn = (canvasX: number, canvasY: number, scene: Scene, opts: DrawOpts) => { x: number; y: number } | null

/** 当前场景中可由键盘选择的合法动作；顺序即方向键轮转顺序。 */
export type KeyboardPicksFn = (scene: Scene) => Array<{ x: number; y: number }>

/** 每游戏 canvas 渲染器统一接口（canvas↔React 桥接契约）。 */
export interface GameCanvasRenderer<S extends Scene = Scene> {
  /** events → 归一化场景（通常复用现有 reducer）。 */
  toScene(events: RawEvent[]): S
  /** 两场景差分，决定触发何种动画。 */
  diff(prev: S | null, next: S): SceneDelta
  /** 每帧绘制：在 prev↔next 间用插值 t(0→1) 画。 */
  draw(ctx: CanvasRenderingContext2D, prev: S | null, next: S, t: number, opts: DrawOpts): void
  /** 可选：canvas 坐标 → 落子坐标（棋类人类对战点击用；扑克不实现）。 */
  pick?: PickFn
  /** 可选：键盘/读屏可遍历的合法动作（交互 canvas 需与 pick 使用同一规则）。 */
  keyboardPicks?: KeyboardPicksFn
}
