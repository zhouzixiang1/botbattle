import type { ReactNode } from 'react'

/**
 * 页面壳：紧凑标题区 + 正文。
 *
 * - `title`：页面主标题（h1）。
 * - `subtitle`：标题下方的一行说明（可选；替代旧的 children 文案）。
 * - `actions`：标题行右侧的操作槽（按钮/筛选器等，可选）。
 * - `children`：页面正文（保留向后兼容；若只用作标题下说明文案，推荐改用 `subtitle`）。
 *
 * 注意：横向与纵向 padding 都由全局 `<main>` 统一提供。页面壳只负责内容
 * 最大宽度；若这里再次设置 `px-*`，所有页面都会形成双层 gutter，牌桌/表格
 * 在笔记本视口尤其明显地被压窄。
 *
 * 全站宽度约定：main/header/footer 的 `px-4 lg:px-8` 是唯一外层 gutter。
 * PageStub 统一以 1536px 为业务内容上限，避免 2K/4K 屏上表格、卡片与文字
 * 被拉成大面积空白边框。普通视口下宽度仍为 100%；各页若需更窄的表单卡片，
 * 再在 children 内使用 `mx-auto max-w-*`。
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
    <div className="mx-auto w-full max-w-screen-2xl">
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
