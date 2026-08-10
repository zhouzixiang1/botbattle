// bzplat/frontend/src/components/GameCanvas.tsx
import { useEffect, useRef, useState } from 'react'
import gsap from 'gsap'
import { toast } from 'sonner'
import { findGame, unsupportedGameLabel } from '@/games'
import type { RawEvent } from '@/games/base'
import type { DrawOpts, SeatInfo, Scene } from '@/games/canvas-types'

/** 默认设计宽高（位图分辨率基线，宽高比 3:2）。 */
const BASE_W = 900
const BASE_H = 600
const DEFAULT_ASPECT_RATIO = BASE_W / BASE_H

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
  /** renderer.pick 返回 null 时的游戏专属非阻塞提示。 */
  invalidPickMessage?: string
}

export default function GameCanvas({
  gameId, events, seats, revealMode = 'all',
  width: widthProp, height: heightProp, className, onMove, interactive,
  invalidPickMessage,
}: Props) {
  const spec = findGame(gameId)
  const aspectRatio = spec?.canvasAspectRatio ?? DEFAULT_ASPECT_RATIO
  // 响应式位图分辨率：父容器宽度经 ResizeObserver 回填；高度由游戏声明的
  // 宽高比计算。位图分辨率跟随 → 始终清晰。
  const [width, setWidth] = useState(widthProp ?? BASE_W)
  const height = heightProp ?? Math.round(width / aspectRatio)
  const [devicePixelRatio, setDevicePixelRatio] = useState(
    () => (typeof window === 'undefined' ? 1 : window.devicePixelRatio || 1),
  )

  const wrapperRef = useRef<HTMLDivElement>(null)
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const stateRef = useRef<{ prev: Scene | null; next: Scene } | null>(null)
  const tlRef = useRef<gsap.core.Timeline | null>(null)
  const activeSpecRef = useRef(spec)
  const renderedEventsRef = useRef<RawEvent[] | null>(null)
  const hoverPickRef = useRef<{ x: number; y: number } | null>(null)
  /** 当前场景动画进度；hover 重绘必须保持这一帧，不能把未完成动画强制跳到 t=1。 */
  const animationProgressRef = useRef(1)
  const [hoverState, setHoverState] = useState<'idle' | 'valid' | 'invalid'>('idle')
  const [keyboardPick, setKeyboardPick] = useState<{ x: number; y: number } | null>(null)
  const [animationState, setAnimationState] = useState<'running' | 'settled'>('settled')
  // ref 镜像 width，供 ResizeObserver 回调读最新值做去抖（避免把 width 进 deps 导致 observer 反复重建）
  const widthRef = useRef(width)
  widthRef.current = width
  const drawOptsRef = useRef<Omit<DrawOpts, 'hoverPick'>>({ width, height, seats, revealMode })
  drawOptsRef.current = { width, height, seats, revealMode }

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

  // 浏览器窗口跨不同缩放比显示器时，CSS 尺寸可能不变；单独监听 DPR，重新采样位图。
  useEffect(() => {
    const sync = () => {
      const next = window.devicePixelRatio || 1
      setDevicePixelRatio((current) => Math.abs(current - next) > 0.001 ? next : current)
    }
    const media = window.matchMedia(`(resolution: ${devicePixelRatio}dppx)`)
    window.addEventListener('resize', sync)
    if (typeof media.addEventListener === 'function') media.addEventListener('change', sync)
    else media.addListener(sync)
    return () => {
      window.removeEventListener('resize', sync)
      if (typeof media.removeEventListener === 'function') media.removeEventListener('change', sync)
      else media.removeListener(sync)
    }
  }, [devicePixelRatio])

  // Hash 路由可在不卸载 GameCanvas 的情况下切换到另一款游戏。不同 renderer 的
  // Scene 结构不兼容，必须先清掉前一游戏的场景和动画，不能拿 PencilScene 给
  // GomokuRenderer.diff/draw（会在真实跨游戏回放导航时崩溃并清空整页）。
  useEffect(() => {
    if (activeSpecRef.current === spec) return
    tlRef.current?.kill()
    stateRef.current = null
    renderedEventsRef.current = null
    animationProgressRef.current = 1
    setAnimationState('settled')
    hoverPickRef.current = null
    activeSpecRef.current = spec
  }, [spec])

  // 尺寸/DPR 适配 effect —— 仅在 width/height/gameId 变化时重跑。
  // canvas.width/height 赋值会清空位图（HTML 规范），故只在尺寸真正变化时才做，
  // 并立即静态重绘上一帧（若已有），避免尺寸变化导致空白。
  // NOTE: deps 故意不含 seats —— seats 引用变化（调用方每次渲染传新数组）
  // 由下方主绘制 effect 的静态重绘分支处理（不清位图），否则会重现 I1：
  // 每次 seats 引用变化都清空位图。这是修复的核心：位图永不在无即时重绘时被清。
  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas || !spec?.CanvasRenderer) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return
    const dpr = devicePixelRatio
    canvas.width = width * dpr
    canvas.height = height * dpr
    ctx.scale(dpr, dpr)
    // 尺寸变化不应清空画面：若有上一帧则立即静态重绘
    const last = stateRef.current?.next
    if (last) {
      spec.CanvasRenderer.draw(ctx, stateRef.current?.prev ?? null, last, animationProgressRef.current, {
        ...drawOptsRef.current,
        hoverPick: hoverPickRef.current,
      })
    }
  }, [width, height, spec, devicePixelRatio])

  // 主绘制 effect —— 事件身份变化时重算场景并跑 GSAP 动画。MatchViewer 会在
  // 每次普通 render 重新 slice 出数组，故不能只依赖数组引用；逐项比较对象身份
  // 可忽略这种无意义重建，也能识别任意位置发生变化的同长度权威 snapshot。
  // seats/倒计时/主题等父 render 不属于场景变化，不得 cleanup 正在运行的 timeline。
  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const renderer = spec?.CanvasRenderer
    if (!renderer) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    const renderedEvents = renderedEventsRef.current
    const sameEventSequence = renderedEvents?.length === events.length
      && renderedEvents.every((event, index) => event === events[index])
    if (sameEventSequence && stateRef.current) return
    renderedEventsRef.current = events

    const prev = stateRef.current?.next ?? null
    const next = renderer.toScene(events)
    const delta = renderer.diff(prev, next)
    stateRef.current = { prev, next }
    hoverPickRef.current = null
    setHoverState('idle')
    setKeyboardPick(null)
    const drawAt = (progress: number) => renderer.draw(ctx, prev, next, progress, {
      ...drawOptsRef.current,
      // timeline 每帧读取 ref；pointer move 后不会继续用创建 timeline 时捕获的旧 hover。
      hoverPick: hoverPickRef.current,
    })

    // 杀掉旧 timeline，建新的（同 botzone：每个状态变化一个 tl）
    tlRef.current?.kill()
    if (delta.animation === 'none') {
      animationProgressRef.current = 1
      setAnimationState('settled')
      renderer.draw(ctx, prev, next, 1, {
        ...drawOptsRef.current,
        hoverPick: hoverPickRef.current,
      })
      return
    }
    const animdata = { t: 0 }
    animationProgressRef.current = 0
    setAnimationState('running')
    const dur = delta.animation === 'settle' ? 1.0 : 0.5
    tlRef.current = gsap.timeline()
    tlRef.current.to(animdata, {
      t: 1,
      duration: dur,
      ease: 'power2.out',
      onUpdate: () => {
        animationProgressRef.current = animdata.t
        drawAt(animdata.t)
      },
      onComplete: () => {
        animationProgressRef.current = 1
        setAnimationState('settled')
      },
    })
  }, [spec, events])

  // 座位文本/revealMode 变化只以当前进度重绘；不拥有、不清理事件动画。
  // HumanPlay 的 500ms 倒计时会产生新 seats 数组，这里必须保持 timeline 继续推进。
  useEffect(() => {
    const canvas = canvasRef.current
    const renderer = spec?.CanvasRenderer
    const state = stateRef.current
    if (!canvas || !renderer || !state) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return
    renderer.draw(ctx, state.prev, state.next, animationProgressRef.current, {
      ...drawOptsRef.current,
      hoverPick: hoverPickRef.current,
    })
  }, [spec, seats, revealMode])

  // 卸载清理（belt-and-suspenders）
  useEffect(() => () => { tlRef.current?.kill() }, [])

  /** 把 CSS 指针坐标映射回 renderer 使用的内部设计坐标。 */
  const pointerPosition = (clientX: number, clientY: number) => {
    const canvas = canvasRef.current
    if (!canvas) return null
    const rect = canvas.getBoundingClientRect()
    if (rect.width <= 0 || rect.height <= 0) return null
    return {
      x: (clientX - rect.left) * (width / rect.width),
      y: (clientY - rect.top) * (height / rect.height),
    }
  }

  /** hover 不进入事件/React 场景，只静态重绘当前权威帧，避免重启 GSAP 动画。 */
  const redrawHover = (hoverPick: { x: number; y: number } | null) => {
    const canvas = canvasRef.current
    const renderer = spec?.CanvasRenderer
    const state = stateRef.current
    if (!canvas || !renderer || !state) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return
    const opts: DrawOpts = { width, height, seats, revealMode, hoverPick }
    renderer.draw(ctx, state.prev, state.next, animationProgressRef.current, opts)
  }

  const pickAt = (clientX: number, clientY: number) => {
    if (!interactive || !onMove) return null
    const renderer = spec?.CanvasRenderer
    const scene = stateRef.current?.next
    const point = pointerPosition(clientX, clientY)
    if (!renderer?.pick || !scene || !point) return null
    return renderer.pick(point.x, point.y, scene, {
      width, height, seats, revealMode,
    })
  }

  const handlePointerMove = (e: React.PointerEvent<HTMLCanvasElement>) => {
    if (!interactive || !onMove) return
    const picked = pickAt(e.clientX, e.clientY)
    hoverPickRef.current = picked
    setKeyboardPick(null)
    setHoverState(picked ? 'valid' : 'invalid')
    redrawHover(picked)
  }

  const clearHover = () => {
    hoverPickRef.current = null
    setHoverState('idle')
    setKeyboardPick(null)
    redrawHover(null)
  }

  useEffect(() => {
    if (interactive && onMove) return
    hoverPickRef.current = null
    setHoverState('idle')
    setKeyboardPick(null)
    redrawHover(null)
  // redrawHover intentionally reads the current scene; changing its function identity
  // must not turn this cleanup into a per-render effect.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [interactive, onMove])

  // 人类对战落子：canvas 像素坐标 → 游戏坐标（经 renderer.pick），仅在 interactive 时启用
  const handleClick = (e: React.MouseEvent<HTMLCanvasElement>) => {
    if (!interactive || !onMove) return
    const picked = pickAt(e.clientX, e.clientY)
    if (picked) {
      onMove(picked.x, picked.y)
      return
    }
    if (invalidPickMessage) {
      toast.info(invalidPickMessage, { id: `invalid-board-pick-${gameId ?? 'unknown'}` })
    }
  }

  const keyboardPicks = () => {
    const renderer = spec?.CanvasRenderer
    const scene = stateRef.current?.next
    return renderer?.keyboardPicks && scene ? renderer.keyboardPicks(scene) : []
  }

  const selectKeyboardPick = (pick: { x: number; y: number }) => {
    hoverPickRef.current = pick
    setKeyboardPick(pick)
    setHoverState('valid')
    redrawHover(pick)
  }

  const handleKeyDown = (e: React.KeyboardEvent<HTMLCanvasElement>) => {
    if (!interactive || !onMove || !spec?.CanvasRenderer?.keyboardPicks) return
    const picks = keyboardPicks()
    if (!picks.length) return
    const currentIndex = keyboardPick
      ? picks.findIndex((pick) => pick.x === keyboardPick.x && pick.y === keyboardPick.y)
      : -1
    if (['ArrowRight', 'ArrowDown', 'ArrowLeft', 'ArrowUp'].includes(e.key)) {
      e.preventDefault()
      const delta = e.key === 'ArrowRight' || e.key === 'ArrowDown' ? 1 : -1
      const nextIndex = currentIndex < 0
        ? (delta > 0 ? 0 : picks.length - 1)
        : (currentIndex + delta + picks.length) % picks.length
      selectKeyboardPick(picks[nextIndex])
      return
    }
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault()
      const selected = currentIndex >= 0 ? picks[currentIndex] : picks[0]
      selectKeyboardPick(selected)
      onMove(selected.x, selected.y)
    }
  }

  if (!findGame(gameId)) {
    return (
      <div role="alert" className="rounded-lg border border-destructive/40 bg-destructive/10 p-4 text-sm text-destructive">
        无法显示对局：{unsupportedGameLabel(gameId)}
      </div>
    )
  }

  const keyboardInteractive = Boolean(
    interactive && onMove && spec?.CanvasRenderer?.keyboardPicks,
  )
  const ariaLabel = keyboardInteractive
    ? `${gameId ?? ''} 对局画面，方向键选择可用位置，回车提交${keyboardPick ? `，当前位置 (${keyboardPick.x},${keyboardPick.y})` : ''}`
    : `${gameId ?? ''} 对局画面${interactive && onMove ? '，点击棋盘选择动作' : ''}`

  return (
    <div ref={wrapperRef} style={{ width: '100%' }}>
      <canvas
        ref={canvasRef}
        // 宽度撑满父容器；高度按游戏声明的宽高比自动撑开。
        // 位图分辨率由上方 DPR effect 按 width/height 设定，不再用 maxWidth 封顶。
        style={{ width: '100%', height: 'auto', aspectRatio: String(aspectRatio), display: 'block' }}
        className={`${className ?? ''}${interactive && onMove
          ? hoverState === 'invalid' ? ' cursor-not-allowed' : ' cursor-crosshair'
          : ''}`}
        data-pick-state={interactive && onMove ? hoverState : 'inactive'}
        data-animation-state={animationState}
        role={keyboardInteractive ? 'button' : 'img'}
        tabIndex={keyboardInteractive ? 0 : undefined}
        aria-label={ariaLabel}
        onClick={handleClick}
        onPointerMove={handlePointerMove}
        onPointerLeave={clearHover}
        onBlur={clearHover}
        onKeyDown={handleKeyDown}
      />
    </div>
  )
}
