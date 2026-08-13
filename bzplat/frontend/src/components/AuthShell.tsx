import type { ReactNode } from 'react'

import { PageFrame, PageHeader } from '@/components/layout'

/** 认证页共享壳：品牌只由全局顶栏呈现，正文保持标题、说明、表单的单一视觉轴。 */
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
      className="min-h-[calc(100dvh-var(--shell-header-height)-6rem)] justify-center gap-6 py-2 [&_button]:min-h-10 sm:py-4"
    >
      <PageHeader
        title={title}
        description={subtitle}
        className="min-w-0 items-center gap-1 text-center sm:flex-col sm:items-center"
      />
      {children}
    </PageFrame>
  )
}
