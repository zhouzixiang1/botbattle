import * as React from "react"
import { useLocation, useNavigationType } from "react-router-dom"

interface ScrollPosition {
  x: number
  y: number
}

const scrollPositions = new Map<string, ScrollPosition>()
const MAX_SAVED_POSITIONS = 100

function remember(key: string, position: ScrollPosition) {
  scrollPositions.delete(key)
  scrollPositions.set(key, position)
  if (scrollPositions.size > MAX_SAVED_POSITIONS) {
    const oldest = scrollPositions.keys().next().value
    if (oldest !== undefined) scrollPositions.delete(oldest)
  }
}

/**
 * HashRouter 路由滚动契约：
 * - PUSH / REPLACE 回到页面顶部并把无障碍焦点移入 main；
 * - POP 恢复该 history entry 的 window 滚动位置；
 * - 懒加载内容尚未撑开页面时，短时观察尺寸后再恢复，用户一旦滚动即停止接管。
 */
function useScrollRestoration({ focusTargetId = "main-content" } = {}) {
  const location = useLocation()
  const navigationType = useNavigationType()
  const previousPathnameRef = React.useRef(location.pathname)

  React.useEffect(() => {
    const previous = window.history.scrollRestoration
    window.history.scrollRestoration = "manual"
    return () => {
      window.history.scrollRestoration = previous
    }
  }, [])

  React.useLayoutEffect(() => {
    const key = location.key
    const pathnameChanged = previousPathnameRef.current !== location.pathname
    previousPathnameRef.current = location.pathname

    // 筛选、分页与后台 Tab 会只更新 search；此时保留用户当前滚动与焦点。
    if (navigationType !== "POP" && !pathnameChanged) {
      return () => remember(key, { x: window.scrollX, y: window.scrollY })
    }

    const target =
      navigationType === "POP"
        ? scrollPositions.get(key) ?? { x: 0, y: 0 }
        : { x: 0, y: 0 }
    let cancelled = false
    let userInterrupted = false
    let resizeObserver: ResizeObserver | undefined
    let timeoutId: number | undefined

    const stopRestoring = () => {
      userInterrupted = true
      resizeObserver?.disconnect()
    }

    const restore = () => {
      if (cancelled || userInterrupted) return
      window.scrollTo({ left: target.x, top: target.y, behavior: "auto" })

      if (navigationType !== "POP") {
        const main = document.getElementById(focusTargetId)
        main?.focus({ preventScroll: true })
      }

      const maxY = Math.max(0, document.documentElement.scrollHeight - window.innerHeight)
      if (target.y <= maxY + 1 || Math.abs(window.scrollY - target.y) <= 1) {
        resizeObserver?.disconnect()
      }
    }

    const interruptEvents = ["wheel", "touchstart", "pointerdown", "keydown"] as const
    interruptEvents.forEach((eventName) =>
      window.addEventListener(eventName, stopRestoring, { passive: true, once: true })
    )

    const animationFrame = window.requestAnimationFrame(restore)
    if (navigationType === "POP" && target.y > 0 && typeof ResizeObserver !== "undefined") {
      resizeObserver = new ResizeObserver(restore)
      resizeObserver.observe(document.documentElement)
      timeoutId = window.setTimeout(() => resizeObserver?.disconnect(), 1500)
    }

    return () => {
      remember(key, { x: window.scrollX, y: window.scrollY })
      cancelled = true
      window.cancelAnimationFrame(animationFrame)
      if (timeoutId !== undefined) window.clearTimeout(timeoutId)
      resizeObserver?.disconnect()
      interruptEvents.forEach((eventName) => window.removeEventListener(eventName, stopRestoring))
    }
  }, [focusTargetId, location.key, location.pathname, navigationType])
}

export { useScrollRestoration }
