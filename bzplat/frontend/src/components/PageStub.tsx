import type { ReactNode } from 'react'

/** 页面占位壳：标题 + 简短说明。 */
export default function PageStub({
  title,
  children,
}: {
  title: string
  children?: ReactNode
}) {
  return (
    <div className="px-4 py-8 lg:px-6">
      <h1 className="page-title text-2xl text-slate-900 sm:text-3xl">{title}</h1>
      <div className="mt-4 text-sm leading-relaxed text-slate-600">{children}</div>
    </div>
  )
}
