// bzplat/frontend/src/components/GameCanvas.tsx
import { useEffect, useRef, useState } from 'react'
import gsap from 'gsap'
import { getGame } from '@/games'
import type { RawEvent } from '@/games/base'
import type { SeatInfo, Scene } from '@/games/canvas-types'

/** 默认设计宽高（位图分辨率基线，宽高比 3:2）。 */
const BASE_W = 900
const BASE_H = 600
const ASPECT = BASE_H / BASE_W // 2/3

interface Props {
  gameId?: string | null
  events: RawEvent[]
  seats?: SeatInfo[]
  revealMode?: 'all' | 'showdown'
  /**
   * 设计坐标系宽高（canvas 内部绘制基准，宽高比决定位图比例）。
   * 不再作为 CSS 最大宽度封顶；父容器宽度经 ResizeObserver 动态回填到 width，
   * 位图分辨率跟随以保持清晰（DPR 缩放）。
   */
  width?: number
  height?: number
  className?: string
  /** 可选：人类对战棋类落子回调（canvas 坐标 → 游戏坐标，经 renderer.pick）。 */
  onMove?: (x: number, y: number) => void
  interactive?: boolean
}

export default function GameCanvas({
  gameId, events, seats, revealMode = 'all',
  width: widthProp, height: heightProp, className, onMove, interactive,
}: Props) {
  // 响应式位图分辨率：默认沿用设计基线；父容器宽度经 ResizeObserver 回填，
  // 保持宽高比 3:2（height = width * 2/3）。位图分辨率跟随 → 始终清晰。
  const [width, setWidth] = useState(widthProp ?? BASE_W)
  const height = heightProp ?? Math.round(width * ASPECT)

  const wrapperRef = useRef<HTMLDivElement>(null)
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const stateRef = useRef<{ prev: Scene | null; next: Scene } | null>(null)
  const tlRef = useRef<gsap.core.Timeline | null>(null)
  const lastEventsLenRef = useRef(0)
  // ref 镜像 width，供 ResizeObserver 回调读最新值做去抖（避免把 width 进 deps 导致 observer 反复重建）
  const widthRef = useRef(width)
  widthRef.current = width

  // ResizeObserver：测量父容器实际宽度，动态设置位图分辨率 width（保持 3:2）。
  // 仅当调用方未显式传 width 时启用（显式 width 视为固定尺寸，不响应式）。
  // 节流：只在宽度变化超过 1px 时更新，避免微小抖动频繁重置位图。
  useEffect(() => {
    if (widthProp != null) return // 显式 width → 固定，不响应式
    const el = wrapperRef.current
    if (!el) return
    const ro = new ResizeObserver((entries) => {
      for (const e of entries) {
        const w = Math.round(e.contentRect.width)
        if (w > 0 && Math.abs(w - widthRef.current) > 1) {
          widthRef.current = w
          setWidth(w)
        }
      }
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [widthProp])

  // 尺寸/DPR 适配 effect —— 仅在 width/height/gameId 变化时重跑。
  // canvas.width/height 赋值会清空位图（HTML 规范），故只在尺寸真正变化时才做，
  // 并立即静态重绘上一帧（若已有），避免尺寸变化导致空白。
  // NOTE: deps 故意不含 seats —— seats 引用变化（调用方每次渲染传新数组）
  // 由下方主绘制 effect 的静态重绘分支处理（不清位图），否则会重现 I1：
  // 每次 seats 引用变化都清空位图。这是修复的核心：位图永不在无即时重绘时被清。
  useEffect(() => {
    const canvas = canvasRef.current
    const spec = getGame(gameId)
    if (!canvas || !spec?.CanvasRenderer) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return
    const dpr = window.devicePixelRatio || 1
    canvas.width = width * dpr
    canvas.height = height * dpr
    ctx.scale(dpr, dpr)
    // 尺寸变化不应清空画面：若有上一帧则立即静态重绘
    const last = stateRef.current?.next
    if (last) {
      spec.CanvasRenderer.draw(ctx, stateRef.current?.prev ?? null, last, 1, {
        width, height, seats, revealMode,
      })
    }
  }, [width, height, gameId])

  // 主绘制 effect —— events 增长时重算场景并跑 GSAP 动画；
  // events 长度不变（父组件因无关状态重渲染、pause/resume、StrictMode 双调用）时
  // 做廉价静态重绘而非裸 return，确保位图始终有内容（修复 I1：清位图后早返导致空白）。
  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const spec = getGame(gameId)
    const renderer = spec.CanvasRenderer
    if (!renderer) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    const st = stateRef.current
    const prev = st?.next ?? null
    // events 长度未变 且 已有场景 → 静态重绘上一帧（用最新 seats），不跑动画
    const drawOpts = { width, height, seats, revealMode }
    if (events.length === lastEventsLenRef.current && st && prev) {
      renderer.draw(ctx, st.prev, st.next, 1, drawOpts)
      return
    }

    lastEventsLenRef.current = events.length

    const next = renderer.toScene(events)
    const delta = renderer.diff(prev, next)
    stateRef.current = { prev, next }

    // 杀掉旧 timeline，建新的（同 botzone：每个状态变化一个 tl）
    tlRef.current?.kill()
    if (delta.animation === 'none') {
      renderer.draw(ctx, prev, next, 1, drawOpts)
      return
    }
    const animdata = { t: 0 }
    const dur = delta.animation === 'settle' ? 1.0 : 0.5
    tlRef.current = gsap.timeline()
    tlRef.current.to(animdata, {
      t: 1,
      duration: dur,
      ease: 'power2.out',
      onUpdate: () => renderer.draw(ctx, prev, next, animdata.t, drawOpts),
    })
    // 每次运行拥有自己的 timeline：cleanup 杀掉本次创建的 tl（StrictMode 双调用安全）
    return () => { tlRef.current?.kill() }
  }, [gameId, events, seats, revealMode, width, height])

  // 卸载清理（belt-and-suspenders）
  useEffect(() => () => { tlRef.current?.kill() }, [])

  // 人类对战落子：canvas 像素坐标 → 游戏坐标（经 renderer.pick），仅在 interactive 时启用
  const handleClick = (e: React.MouseEvent<HTMLCanvasElement>) => {
    if (!interactive || !onMove) return
    const spec = getGame(gameId)
    const renderer = spec.CanvasRenderer
    if (!renderer?.pick || !stateRef.current?.next) return
    const canvas = canvasRef.current
    if (!canvas) return
    const rect = canvas.getBoundingClientRect()
    // canvas 内部坐标系是 width×height（CSS 缩放后映射回内部坐标）
    const scale = width / rect.width
    const canvasX = (e.clientX - rect.left) * scale
    const canvasY = (e.clientY - rect.top) * (height / rect.height)
    const picked = renderer.pick(canvasX, canvasY, stateRef.current.next, {
      width, height, seats, revealMode,
    })
    if (picked) onMove(picked.x, picked.y)
  }

  return (
    <div ref={wrapperRef} style={{ width: '100%' }}>
      <canvas
        ref={canvasRef}
        // 宽度撑满父容器；高度按 3:2 宽高比自动撑开（aspect-ratio + height: auto）。
        // 位图分辨率由上方 DPR effect 按 width/height 设定，不再用 maxWidth 封顶。
        style={{ width: '100%', height: 'auto', aspectRatio: '3 / 2', display: 'block' }}
        className={className + (interactive && onMove ? ' cursor-pointer' : '')}
        role="img"
        aria-label={`${gameId ?? ''} 对局画面`}
        onClick={handleClick}
      />
    </div>
  )
}
