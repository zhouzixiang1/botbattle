import { useCallback, useEffect, useState } from 'react'

// 相对路径 + 显式 .ts 后缀：本模块被 node --test 直接导入（tsconfig 的
// `@/` 别名只对 Vite/tsc 生效），与 games/gomoku/reducer.ts 引 time-controls
// 同一先例。
import { ApiError, retryAfterMs } from '../api.ts'

/** 服务端明确要求等待后重试的 503 错误码（code 在 detail 字典内）。 */
const RETRY_LATER_DETAIL_CODES = new Set([
  'upload_busy',
  'build_busy',
  'deployment_maintenance',
  'sandbox_unavailable',
])

const MIN_COOLDOWN_MS = 1_000
const MAX_COOLDOWN_MS = 10 * 60 * 1_000

/** 503 繁忙错误的机器码位于 detail 字典内（detail 已物化为 message），
 * 从 rawDetail 原始串还原；非字典形状返回 null。 */
export function retryLaterDetailCode(err: ApiError): string | null {
  if (!err.rawDetail.trim().startsWith('{')) return null
  try {
    const parsed = JSON.parse(err.rawDetail) as { code?: unknown }
    return typeof parsed?.code === 'string' && parsed.code.trim() ? parsed.code : null
  } catch {
    return null
  }
}

/**
 * 提交失败的冷却判定：429 限流或服务端明确要求等待的 503 才冷却；
 * 400 等用户可就地修正的错误返回 null（不冷却）。冷却时长优先取
 * Retry-After，缺失用 fallbackMs，并夹在 [1s, 10min]。
 */
export function submitCooldownMs(err: unknown, fallbackMs = 5_000): number | null {
  if (!(err instanceof ApiError)) return null
  if (err.status !== 429 && err.status !== 503) return null
  if (err.status === 503) {
    const code = retryLaterDetailCode(err)
    if (code === null || !RETRY_LATER_DETAIL_CODES.has(code)) return null
  }
  return Math.min(MAX_COOLDOWN_MS, Math.max(MIN_COOLDOWN_MS, Math.round(retryAfterMs(err, fallbackMs))))
}

export interface SubmitCooldownView {
  active: boolean
  remainingSeconds: number
}

/** 冷却剩余视图：整秒向上取整；到期即 inactive/0（注入 now 便于测试）。 */
export function submitCooldownView(cooldownUntil: number, now: number): SubmitCooldownView {
  if (!Number.isFinite(cooldownUntil) || now >= cooldownUntil) {
    return { active: false, remainingSeconds: 0 }
  }
  return {
    active: true,
    remainingSeconds: Math.max(1, Math.ceil((cooldownUntil - now) / 1000)),
  }
}

/**
 * 上传提交失败的冷却门：进入冷却后提交按钮禁用并显示倒计时文案，
 * 剩余秒数归零时自动解除。时钟经 nowFn 注入（默认 Date.now）。
 */
export function useSubmitCooldown(nowFn: () => number = Date.now) {
  const [cooldownUntil, setCooldownUntil] = useState(0)
  const [now, setNow] = useState(() => nowFn())
  const active = now < cooldownUntil

  // 冷却期间每 250ms 对齐一次时钟；到期翻转 active 后 effect 清理定时器。
  useEffect(() => {
    if (!active) return
    const timer = window.setInterval(() => setNow(nowFn()), 250)
    return () => window.clearInterval(timer)
  }, [active, nowFn])

  /** 提交失败时调用：可冷却的错误返回 true 并启动倒计时，其余返回 false。 */
  const beginCooldown = useCallback((err: unknown, fallbackMs = 5_000): boolean => {
    const ms = submitCooldownMs(err, fallbackMs)
    if (ms === null) return false
    const startedAt = nowFn()
    setCooldownUntil(startedAt + ms)
    setNow(startedAt)
    return true
  }, [nowFn])

  const clearCooldown = useCallback(() => {
    setCooldownUntil(0)
  }, [])

  return { ...submitCooldownView(cooldownUntil, now), beginCooldown, clearCooldown }
}
