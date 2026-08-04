/**
 * Countdown —— 通用倒计时组件。
 *
 * 泛化自 ContestDetail 的 RestCountdown，支持天/时/分/秒智能显示：
 * - >1 天：显示「X天 Y时」
 * - >1 时：显示「Y时 Z分」
 * - <1 时：显示「Z分 S秒」（font-mono）
 * 到点显示「已到时」（可选自定义文案）。
 *
 * 复用于：报名窗口倒计时 / 开赛倒计时 / 休息恢复倒计时 / 逐场排期。
 */
import { useEffect, useState } from 'react'

interface Props {
  /** 目标时刻（ISO 字符串或 Date） */
  endsAt: string | Date
  /** 到点显示文案（默认「已到时」） */
  expiredText?: string
  className?: string
}

function format(ms: number): string {
  if (ms <= 0) return ''
  const totalSec = Math.floor(ms / 1000)
  const days = Math.floor(totalSec / 86400)
  const hours = Math.floor((totalSec % 86400) / 3600)
  const mins = Math.floor((totalSec % 3600) / 60)
  const secs = totalSec % 60
  if (days > 0) return `${days}天 ${hours}时`
  if (hours > 0) return `${hours}时 ${mins}分`
  return `${mins}:${secs.toString().padStart(2, '0')}`
}

export default function Countdown({ endsAt, expiredText = '已到时', className }: Props) {
  const target = typeof endsAt === 'string' ? new Date(endsAt).getTime() : endsAt.getTime()
  const [left, setLeft] = useState('')

  useEffect(() => {
    // 非法 ISO 字符串 → target=NaN，直接显示 expiredText（不渲染 NaN:NaN）
    if (!Number.isFinite(target)) {
      setLeft('')
      return
    }
    const tick = () => {
      const ms = target - Date.now()
      setLeft(!Number.isFinite(ms) || ms <= 0 ? '' : format(ms))
    }
    tick()
    const id = setInterval(tick, 1000)
    return () => clearInterval(id)
  }, [target])

  if (!left) {
    return <span className={className}>{expiredText}</span>
  }
  return <span className={`font-mono font-semibold ${className || ''}`}>{left}</span>
}
