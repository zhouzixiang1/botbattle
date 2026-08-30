import { useEffect, useState } from 'react'

/**
 * 直播边缘的累计棋钟本地走秒。
 *
 * 事件流只在每次决策后下发一条 time_used，Bot 长考期间没有新事件，
 * 棋钟若只靠事件驱动就会静止。此 hook 以最近一次权威剩余时间为锚点，
 * 每秒对行动方递减显示值，并在权威值变化（新事件到达）时重新锚定。
 * 回放、回退与暂停历史画面由调用方将 enabled 置 false，显示保持权威值。
 * 走秒显示是客户端近似值，始终以事件流中的权威 remaining 为准。
 */

/** 纯函数：按已流逝秒数对行动方递减，非行动方与空值原样保留。 */
export function tickRemaining(
  values: readonly (number | null)[],
  actingSeat: number | null,
  elapsedSec: number,
): (number | null)[] {
  const out = values.slice() as (number | null)[]
  if (actingSeat === null || actingSeat < 0 || actingSeat >= out.length) return out
  if (elapsedSec <= 0) return out
  const current = out[actingSeat]
  if (current == null || !Number.isFinite(current)) return out
  out[actingSeat] = Math.max(0, current - elapsedSec)
  return out
}

export function useTickingRemaining(
  enabled: boolean,
  remaining: readonly (number | null)[],
  actingSeat: number | null,
): (number | null)[] {
  // 权威值锚点：值串变化（新 time_used 事件）即重置计时起点。
  const key = remaining.map((v) => (v == null ? 'n' : v.toFixed(3))).join(',')
  const [anchor, setAnchor] = useState(() => ({
    values: remaining.slice() as (number | null)[],
    at: Date.now(),
  }))
  const [, setBeat] = useState(0)
  useEffect(() => {
    setAnchor({ values: remaining.slice() as (number | null)[], at: Date.now() })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key])
  useEffect(() => {
    if (!enabled) return
    const timer = setInterval(() => setBeat((v) => v + 1), 1000)
    return () => clearInterval(timer)
  }, [enabled])
  if (!enabled) return anchor.values
  const elapsed = Math.floor((Date.now() - anchor.at) / 1000)
  return tickRemaining(anchor.values, actingSeat, elapsed)
}
