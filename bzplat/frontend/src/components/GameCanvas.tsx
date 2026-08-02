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
  width?: number
  height?: number
  className?: string
}

export default function GameCanvas({ gameId, events, seats, width = 900, height = 600, className }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const stateRef = useRef<{ prev: Scene | null; next: Scene } | null>(null)
  const tlRef = useRef<gsap.core.Timeline | null>(null)
  const lastEventsLenRef = useRef(0)

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const spec = getGame(gameId)
    const renderer = spec.CanvasRenderer
    if (!renderer) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    // 适配 devicePixelRatio（高清屏不糊）
    const dpr = window.devicePixelRatio || 1
    canvas.width = width * dpr
    canvas.height = height * dpr
    ctx.scale(dpr, dpr)

    // 首次或 events 增长 → 重算场景 + 跑动画
    if (events.length === lastEventsLenRef.current) return
    lastEventsLenRef.current = events.length

    const next = renderer.toScene(events)
    const prev = stateRef.current?.next ?? null
    const delta = renderer.diff(prev, next)
    stateRef.current = { prev, next }

    // 杀掉旧 timeline，建新的（同 botzone：每个状态变化一个 tl）
    tlRef.current?.kill()
    if (delta.animation === 'none') {
      renderer.draw(ctx, prev, next, 1, { width, height, seats })
      return
    }
    const animdata = { t: 0 }
    const dur = delta.animation === 'settle' ? 1.0 : 0.5
    tlRef.current = gsap.timeline()
    tlRef.current.to(animdata, {
      t: 1,
      duration: dur,
      ease: 'power2.out',
      onUpdate: () => renderer.draw(ctx, prev, next, animdata.t, { width, height, seats }),
    })
  }, [gameId, events, seats, width, height])

  // 卸载清理
  useEffect(() => () => { tlRef.current?.kill() }, [])

  return (
    <canvas
      ref={canvasRef}
      style={{ width: '100%', height: 'auto', maxWidth: width }}
      className={className}
      role="img"
      aria-label={`${gameId ?? ''} 对局画面`}
    />
  )
}
