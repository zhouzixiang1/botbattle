import type { ReactNode } from 'react'

/**
 * 页面壳：紧凑标题区 + 正文。
 *
 * - `title`：页面主标题（h1）。
 * - `subtitle`：标题下方的一行说明（可选；替代旧的 children 文案）。
 * - `actions`：标题行右侧的操作槽（按钮/筛选器等，可选）。
 * - `children`：页面正文（保留向后兼容；若只用作标题下说明文案，推荐改用 `subtitle`）。
 *
 * 注意：垂直 padding 由全局 `<main>` 统一提供（app-shell.tsx 的 main 有 py-6），
 * 这里只设水平 padding，避免与 main 叠加成双倍顶部留白。
 *
 * 全站宽度约定：水平 padding 与 main/header/footer 的 `px-4 lg:px-8` 对齐
 * （由 app-shell 的 <main> 统一收口，此处不再叠加 max-w 居中容器，避免双重包裹
 * 导致标题缩进与 header/footer 错位）。各页若需限宽（如表单卡片），在 children
 * 内自行 `mx-auto max-w-*`；双栏布局在 children 内自行 `lg:grid`。
 */
export default function PageStub({
  title,
  subtitle,
  actions,
  children,
}: {
  title: string
  subtitle?: ReactNode
  actions?: ReactNode
  children?: ReactNode
}) {
  return (
    <div className="px-4 lg:px-8">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div className="space-y-1">
          <h1 className="page-title text-2xl text-foreground sm:text-3xl">{title}</h1>
          {subtitle && (
            <p className="text-sm leading-relaxed text-muted-foreground">{subtitle}</p>
          )}
        </div>
        {actions && <div className="flex flex-wrap items-center gap-2 sm:justify-end">{actions}</div>}
      </div>
      {/* 兼容旧用法：children 既可能是正文（默认 mt-6），也可能只是说明文案 */}
      <div className={subtitle ? 'mt-6' : 'mt-4'}>{children}</div>
    </div>
  )
}
