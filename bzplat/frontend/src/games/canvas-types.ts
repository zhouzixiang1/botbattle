/**
 * Canvas↔React 桥接契约（canvas+GSAP 视觉重写基建）。
 *
 * 每款游戏的 canvas 渲染器实现 `GameCanvasRenderer<S>`：把后端 events 归约成
 * 归一化场景、计算两帧差分以决定触发何种动画、按插值 t 在 prev↔next 间绘制。
 *
 * `GameViewSpec.CanvasRenderer` 是可选字段：若提供，GameCanvas 组件优先用它绘制，
 * 替代默认的 DOM Board。无该字段的游戏继续走原有 DOM 渲染路径（gomoku/pencil 等）。
 */
import type { RawEvent } from '@/components/poker/useMatchState'

/** 一个游戏的归一化场景（由 toScene 从 events 归约）。每游戏自定义具体结构。 */
export type Scene = Record<string, unknown>

/** 场景差分：哪些是「新」的需动画。每游戏自定义结构（payload 为松类型）。 */
export type SceneDelta = { animation: 'deal' | 'place' | 'settle' | 'none'; payload?: unknown }

export interface DrawOpts {
  width: number
  height: number
  /** 可选：座位身份（Bot 名/用户名），用于绘制座位标签 */
  seats?: SeatInfo[]
}
export interface SeatInfo {
  botName?: string
  ownerName?: string
  isHuman?: boolean
}

/** 每游戏 canvas 渲染器统一接口（canvas↔React 桥接契约）。 */
export interface GameCanvasRenderer<S extends Scene = Scene> {
  /** events → 归一化场景（通常复用现有 reducer）。 */
  toScene(events: RawEvent[]): S
  /** 两场景差分，决定触发哪种动画。 */
  diff(prev: S | null, next: S): SceneDelta
  /** 每帧绘制：在 prev↔next 间用插值 t(0→1) 画。 */
  draw(ctx: CanvasRenderingContext2D, prev: S | null, next: S, t: number, opts: DrawOpts): void
}
