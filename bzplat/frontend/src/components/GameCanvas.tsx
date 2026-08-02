// bzplat/frontend/src/components/GameCanvas.tsx
import { useEffect, useRef } from 'react'
import gsap from 'gsap'
import { getGame } from '@/games'
import type { RawEvent } from '@/components/poker/useMatchState'
import type { SeatInfo, Scene } from '@/games/canvas-types'

interface Props {
  gameId?: string | null
  events: RawEvent[]
  seats?: SeatInfo[]
  revealMode?: 'all' | 'showdown'
  width?: number
  height?: number
  className?: string
  /** 可选：人类对战棋类落子回调（canvas 坐标 → 游戏坐标，经 renderer.pick）。 */
  onMove?: (x: number, y: number) => void
  interactive?: boolean
}

export default function GameCanvas({
  gameId, events, seats, revealMode = 'all',
  width = 900, height = 600, className, onMove, interactive,
}: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const stateRef = useRef<{ prev: Scene | null; next: Scene } | null>(null)
  const tlRef = useRef<gsap.core.Timeline | null>(null)
  const lastEventsLenRef = useRef(0)

  // 尺寸/DPR 适配 effect —— 仅在 width/height/gameId 变化时重跑。
  // canvas.width/height 赋值会清空位图（HTML 规范），故只在尺寸真正变化时才做，
  // 并立即静态重绘上一帧（若已有），避免尺寸变化导致空白。
  // NOTE: deps 故意不含 seats —— seats 引用变化（如 ArenaWatch 每次渲染传新数组）
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
    <canvas
      ref={canvasRef}
      style={{ width: '100%', height: 'auto', maxWidth: width }}
      className={className + (interactive && onMove ? ' cursor-pointer' : '')}
      role="img"
      aria-label={`${gameId ?? ''} 对局画面`}
      onClick={handleClick}
    />
  )
}
