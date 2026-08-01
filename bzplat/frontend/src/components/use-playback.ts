import { useEffect, useRef, useState, useCallback } from 'react'

/** 播放速度档（ms = 每步间隔毫秒） */
export const SPEEDS = [
  { label: '0.5x', ms: 1400 },
  { label: '1x', ms: 700 },
  { label: '2x', ms: 350 },
  { label: '4x', ms: 175 },
] as const

/**
 * 定速回放/直播缓冲 hook。
 *
 * - `buffer`：事件缓冲区（调用方追加，如 SSE 推送或一次性 replay）。
 * - `stepIdx`：渲染游标（-1 = 跟随末尾/直播）。
 * - `playing`：是否自动播放（按 SPEEDS[speedIdx].ms 定时步进）。
 *
 * 直播场景：buffer 持续增长，stepIdx=-1 时游标始终指向 buffer 末尾（直播跟随）；
 * 用户点暂停/步进后 stepIdx 固定，可手动拖动；点播放后定速追赶至末尾再转直播跟随。
 */
export function usePlayback(initialSpeedIdx: number = 1) {
  const [buffer, setBuffer] = useState<unknown[]>([])
  const [stepIdx, setStepIdx] = useState(-1) // -1 = 跟随末尾
  const [playing, setPlaying] = useState(false)
  const [speedIdx, setSpeedIdx] = useState(initialSpeedIdx)

  const total = buffer.length
  // 当前显示到第几步（-1 表示末尾）
  const cur = stepIdx < 0 ? Math.max(0, total - 1) : Math.min(stepIdx, total - 1)
  // 渲染区切片
  const visible = total > 0 ? buffer.slice(0, cur + 1) : []
  // 是否在直播跟随（游标在末尾且非手动暂停）
  const atLive = stepIdx < 0
  // 落后步数（直播场景，buffer 比游标多多少）
  const lag = atLive ? 0 : Math.max(0, total - 1 - cur)

  /** 追加事件到缓冲区（直播模式自动跟随） */
  const append = useCallback((ev: unknown | unknown[]) => {
    setBuffer((prev) => {
      const inc = Array.isArray(ev) ? ev : [ev]
      return [...prev, ...inc]
    })
  }, [])

  /** 一次性设置全部缓冲（回放模式） */
  const setAll = useCallback((evs: unknown[]) => {
    setBuffer(evs)
  }, [])

  /** 清空 */
  const clear = useCallback(() => {
    setBuffer([])
    setStepIdx(-1)
    setPlaying(false)
  }, [])

  const pause = useCallback(() => setPlaying(false), [])

  /** 跳到指定步（0-based，-1=末尾） */
  const seek = useCallback((idx: number) => {
    setPlaying(false)
    setStepIdx(idx)
  }, [])

  /** 步进 ±n */
  const step = useCallback((delta: number) => {
    setPlaying(false)
    setStepIdx((s) => {
      const base = s < 0 ? total - 1 : s
      return Math.max(0, Math.min(total - 1, base + delta))
    })
  }, [total])

  /** 切换播放/暂停；播到末尾时再点回到开头 */
  const togglePlay = useCallback(() => {
    if (!playing && cur >= total - 1) {
      // 已到末尾，重新从头播
      setStepIdx(total > 1 ? 0 : -1)
    }
    setPlaying((p) => !p)
  }, [playing, cur, total])

  // 定速播放定时器
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => {
    if (!playing || total === 0) return
    if (cur >= total - 1) {
      // 追上末尾 → 转直播跟随
      setPlaying(false)
      setStepIdx(-1)
      return
    }
    timerRef.current = setTimeout(() => {
      setStepIdx((s) => {
        const next = (s < 0 ? total - 1 : s) + 1
        return next >= total - 1 ? -1 : next
      })
    }, SPEEDS[speedIdx].ms)
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current)
    }
  }, [playing, cur, total, speedIdx])

  // buffer 增长时若在直播跟随，保持游标在末尾（stepIdx 仍 -1，cur 自动算）
  // 无需额外 effect，因为 cur = stepIdx<0 ? total-1 : ...

  return {
    buffer, total, cur, visible, atLive, lag,
    stepIdx, playing, speedIdx,
    setSpeedIdx, append, setAll, clear, pause, seek, step, togglePlay,
  }
}
