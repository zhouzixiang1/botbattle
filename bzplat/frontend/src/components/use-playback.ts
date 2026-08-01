import { useEffect, useRef, useState, useCallback } from 'react'

/** 播放速度档（ms = 每步间隔毫秒） */
export const SPEEDS = [
  { label: '0.5x', ms: 1400 },
  { label: '1x', ms: 700 },
  { label: '2x', ms: 350 },
  { label: '4x', ms: 175 },
] as const

/**
 * 缓冲区上限：实时观赛长对局（如 70 手德州，单局事件可达数百到上千条）时，
 * 防止 buffer 无界增长导致 append 的 O(n) 全量复制累积成 O(n²)、最终 OOM。
 * 超过时丢弃头部最旧事件并相应平移 stepIdx（直播跟随态 stepIdx=-1 不受影响）。
 */
const MAX_BUFFER = 4000

/**
 * 定速回放/直播缓冲 hook。
 *
 * - `buffer`：事件缓冲区（调用方追加，如 SSE 推送或一次性 replay）；超过 MAX_BUFFER 丢最旧。
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

  /** 追加事件到缓冲区（直播模式自动跟随）；超过 MAX_BUFFER 丢最旧并平移游标。 */
  const append = useCallback((ev: unknown | unknown[]) => {
    const inc = Array.isArray(ev) ? ev : [ev]
    setBuffer((prev) => {
      const next = prev.length + inc.length > MAX_BUFFER
        ? [...prev.slice(-(MAX_BUFFER - inc.length)), ...inc]
        : [...prev, ...inc]
      return next
    })
  }, [])

  /** 一次性设置全部缓冲（回放模式）；同样受 MAX_BUFFER 约束（取末尾）。 */
  const setAll = useCallback((evs: unknown[]) => {
    setBuffer(evs.length > MAX_BUFFER ? evs.slice(-MAX_BUFFER) : evs)
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

  /** 步进 ±n（直播跟随态时先锚定到当前末尾再退/进） */
  const step = useCallback((delta: number) => {
    setPlaying(false)
    setStepIdx((s) => {
      const base = s < 0 ? Math.max(0, total - 1) : s
      return Math.max(0, Math.min(Math.max(0, total - 1), base + delta))
    })
  }, [total])

  /** 切换播放/暂停。直播跟随态（atLive）点播放视为 no-op（不跳回开头，避免观赛灾难）。 */
  const togglePlay = useCallback(() => {
    if (!playing && cur >= total - 1 && !atLive) {
      // 已到末尾（非直播跟随），重新从头播
      setStepIdx(total > 1 ? 0 : -1)
    }
    setPlaying((p) => !p)
  }, [playing, cur, total, atLive])

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

  return {
    buffer, total, cur, visible, atLive, lag,
    stepIdx, playing, speedIdx,
    setSpeedIdx, append, setAll, clear, pause, seek, step, togglePlay,
  }
}
