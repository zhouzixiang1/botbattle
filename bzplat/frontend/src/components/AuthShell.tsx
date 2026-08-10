import type { ReactNode } from 'react'

import BrandMark from '@/components/BrandMark'
import { PageFrame, PageHeader } from '@/components/layout'

/** 认证页共享壳：沿用全局 main 的单一 gutter/滚动 owner，只压缩品牌与表单间距。 */
export default function AuthShell({
  layout,
  title,
  subtitle,
  children,
}: {
  layout: string
  title: string
  subtitle?: string
  children?: ReactNode
}) {
  return (
    <PageFrame
      width="readable"
      layout={layout}
      className="min-h-[calc(100dvh-var(--shell-header-height)-6rem)] justify-center py-2 sm:py-4"
    >
      <div className="flex min-w-0 flex-col items-center gap-2 text-center sm:flex-row sm:justify-center sm:text-left">
        <BrandMark size="md" />
        <PageHeader
          title={title}
          description={subtitle}
          className="min-w-0 items-center gap-1 text-center sm:flex-col sm:items-start sm:text-left"
        />
      </div>
      {children}
    </PageFrame>
  )
}
