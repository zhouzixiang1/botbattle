import { useCallback, useEffect, useRef, useState } from 'react'

interface SingleFlightPollingOptions {
  task: (signal: AbortSignal) => Promise<void>
  onError?: (error: unknown) => void
  onSuccess?: () => void
  enabled?: boolean
  intervalMs: number
  /** Delay before the first request in this polling scope. Defaults to immediate. */
  initialDelayMs?: number
  maxIntervalMs?: number
  /** Changing the scope aborts the old request even when the interval is unchanged. */
  scopeKey?: string | number | null
}

function isAbortError(error: unknown): boolean {
  return error instanceof Error && error.name === 'AbortError'
}

/**
 * A visibility-aware polling loop that never overlaps requests.
 *
 * The next request is scheduled only after the previous one settles. Manual
 * refreshes abort an obsolete request first, network failures back off, and an
 * online/visible transition resumes immediately.
 */
export function useSingleFlightPolling({
  task,
  onError,
  onSuccess,
  enabled = true,
  intervalMs,
  initialDelayMs = 0,
  maxIntervalMs = intervalMs * 8,
  scopeKey = null,
}: SingleFlightPollingOptions) {
  const taskRef = useRef(task)
  const onErrorRef = useRef(onError)
  const onSuccessRef = useRef(onSuccess)
  const refreshRef = useRef<() => void>(() => undefined)
  const [polling, setPolling] = useState(false)
  const [offline, setOffline] = useState(() => !navigator.onLine)

  taskRef.current = task
  onErrorRef.current = onError
  onSuccessRef.current = onSuccess

  const refresh = useCallback(() => refreshRef.current(), [])

  useEffect(() => {
    let disposed = false
    let timer: number | null = null
    let controller: AbortController | null = null
    let inFlight = false
    let runAgain = false
    let failureCount = 0

    const clearTimer = () => {
      if (timer !== null) window.clearTimeout(timer)
      timer = null
    }

    const canRun = () => enabled && navigator.onLine && document.visibilityState !== 'hidden'

    const schedule = (delay: number) => {
      clearTimer()
      if (disposed || !enabled) return
      timer = window.setTimeout(() => { void run() }, delay)
    }

    const run = async () => {
      timer = null
      if (disposed || !enabled) return
      if (!canRun()) return
      if (inFlight) {
        runAgain = true
        return
      }

      inFlight = true
      runAgain = false
      controller = new AbortController()
      const current = controller
      setPolling(true)
      try {
        await taskRef.current(current.signal)
        if (disposed || current.signal.aborted) return
        failureCount = 0
        onSuccessRef.current?.()
      } catch (error) {
        if (disposed || current.signal.aborted || isAbortError(error)) return
        failureCount += 1
        onErrorRef.current?.(error)
      } finally {
        if (controller === current) controller = null
        inFlight = false
        if (!disposed) setPolling(false)
        if (disposed || !enabled) return
        if (runAgain && canRun()) {
          runAgain = false
          schedule(0)
          return
        }
        const delay = Math.min(
          maxIntervalMs,
          intervalMs * Math.max(1, 2 ** failureCount),
        )
        schedule(delay)
      }
    }

    const requestRefresh = () => {
      if (disposed || !enabled) return
      runAgain = true
      clearTimer()
      if (inFlight) {
        controller?.abort()
      } else if (canRun()) {
        runAgain = false
        schedule(0)
      }
    }
    refreshRef.current = requestRefresh

    const handleOnline = () => {
      setOffline(false)
      requestRefresh()
    }
    const handleOffline = () => {
      setOffline(true)
      runAgain = false
      clearTimer()
      controller?.abort()
    }
    const handleVisibility = () => {
      if (document.visibilityState === 'hidden') {
        runAgain = false
        clearTimer()
        controller?.abort()
      } else {
        requestRefresh()
      }
    }

    window.addEventListener('online', handleOnline)
    window.addEventListener('offline', handleOffline)
    document.addEventListener('visibilitychange', handleVisibility)
    setOffline(!navigator.onLine)
    schedule(initialDelayMs)

    return () => {
      disposed = true
      clearTimer()
      controller?.abort()
      refreshRef.current = () => undefined
      window.removeEventListener('online', handleOnline)
      window.removeEventListener('offline', handleOffline)
      document.removeEventListener('visibilitychange', handleVisibility)
    }
  }, [enabled, initialDelayMs, intervalMs, maxIntervalMs, scopeKey])

  return { refresh, polling, offline }
}
