import type { ReactNode } from 'react'
import BrandMark from '@/components/BrandMark'

/**
 * auth 页面（登录/注册/重置/验证）的共享壳：
 * - 垂直水平居中，自适应高度（填满 <main> 可用区，避免整页空旷）；
 * - 顶部品牌标识 + 标题/副标题，提供上下文引导；
 * - children 放置表单 Card。
 *
 * 高度策略：用 min-h-[60vh] 作为下限保证视觉居中，但不强占整屏——避免内容多时
 * （如 Register 4 字段 + 验证码）因 calc 高度算错（顶栏实际 h-14=3.5rem + 页脚）
 * 导致整页滚动条或内容被截断。
 */
export default function AuthShell({
  title,
  subtitle,
  children,
}: {
  title: string
  subtitle?: string
  children?: ReactNode
}) {
  return (
    <div className="flex min-h-[60vh] flex-col items-center justify-center px-4 py-10">
      <div className="mb-6 flex flex-col items-center gap-3 text-center">
        <BrandMark size="lg" />
        <div className="space-y-1">
          <h1 className="page-title text-2xl text-foreground sm:text-3xl">{title}</h1>
          {subtitle && <p className="text-sm text-muted-foreground">{subtitle}</p>}
        </div>
      </div>
      {children}
    </div>
  )
}
