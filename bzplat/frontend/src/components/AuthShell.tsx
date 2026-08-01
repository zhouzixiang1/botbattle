import type { ReactNode } from 'react'
import BrandMark from '@/components/BrandMark'

/**
 * auth 页面（登录/注册/重置/验证）的共享壳：
 * - 垂直水平居中，自适应高度（min-h 扣掉顶栏，避免整页空旷）；
 * - 顶部品牌标识 + 标题/副标题，提供上下文引导；
 * - children 放置表单 Card。
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
    <div className="flex min-h-[calc(100vh-4rem)] flex-col items-center justify-center px-4 py-10">
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
