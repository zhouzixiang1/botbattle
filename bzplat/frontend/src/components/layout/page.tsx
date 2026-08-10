import * as React from "react"

import { cn } from "@/lib/utils"

type PageFrameWidth = "full" | "wide" | "default" | "narrow" | "readable"

const PAGE_WIDTH: Record<PageFrameWidth, string> = {
  full: "max-w-none",
  wide: "max-w-screen-2xl",
  default: "max-w-7xl",
  narrow: "max-w-5xl",
  readable: "max-w-3xl",
}

function PageFrame({
  className,
  width = "wide",
  layout = "default",
  children,
  ...props
}: React.ComponentProps<"div"> & {
  width?: PageFrameWidth
  /** 截图/溢出审计使用的稳定页面布局名称。 */
  layout?: string
}) {
  return (
    <div
      data-slot="page-frame"
      data-page-layout={layout}
      className={cn(
        "mx-auto flex w-full min-w-0 flex-col gap-[var(--page-section-gap)]",
        PAGE_WIDTH[width],
        className
      )}
      {...props}
    >
      {children}
    </div>
  )
}

function PageHeader({
  className,
  title,
  description,
  eyebrow,
  actions,
  children,
  ...props
}: Omit<React.ComponentProps<"header">, "title"> & {
  title: React.ReactNode
  description?: React.ReactNode
  eyebrow?: React.ReactNode
  actions?: React.ReactNode
}) {
  return (
    <header
      data-slot="page-header"
      className={cn(
        "flex min-w-0 flex-col gap-3 sm:flex-row sm:items-end sm:justify-between",
        className
      )}
      {...props}
    >
      <div className="min-w-0 space-y-1">
        {eyebrow && (
          <div className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">
            {eyebrow}
          </div>
        )}
        <h1 className="page-title text-2xl text-foreground sm:text-[1.75rem]">{title}</h1>
        {description && (
          <div className="max-w-3xl text-sm leading-relaxed text-muted-foreground">
            {description}
          </div>
        )}
        {children}
      </div>
      {actions && (
        <div data-slot="page-header-actions" className="flex min-w-0 flex-wrap items-center gap-2 sm:justify-end">
          {actions}
        </div>
      )}
    </header>
  )
}

function StickyToolbar({
  className,
  children,
  label = "页面工具栏",
  ...props
}: React.ComponentProps<"div"> & { label?: string }) {
  const ref = React.useRef<HTMLDivElement>(null)

  React.useLayoutEffect(() => {
    const toolbar = ref.current
    const page = toolbar?.closest<HTMLElement>("[data-page-layout]")
    if (!toolbar || !page) return

    const updateHeight = () => {
      page.style.setProperty("--sticky-toolbar-height", `${toolbar.offsetHeight}px`)
      page.style.setProperty("--sticky-toolbar-gap", "0.5rem")
    }

    updateHeight()
    const observer =
      typeof ResizeObserver === "undefined" ? undefined : new ResizeObserver(updateHeight)
    observer?.observe(toolbar)
    return () => {
      observer?.disconnect()
      page.style.removeProperty("--sticky-toolbar-height")
      page.style.removeProperty("--sticky-toolbar-gap")
    }
  }, [])

  return (
    <div
      ref={ref}
      data-slot="sticky-toolbar"
      data-sticky-region="toolbar"
      role="region"
      aria-label={label}
      className={cn(
        "sticky top-[var(--sticky-page-offset)] z-[var(--z-sticky)] flex min-w-0 flex-wrap items-center gap-2 rounded-lg border bg-background/95 px-2 py-2 shadow-sm backdrop-blur-sm supports-[backdrop-filter]:bg-background/85",
        className
      )}
      {...props}
    >
      {children}
    </div>
  )
}

const SUMMARY_COLUMNS = {
  2: "sm:grid-cols-2",
  3: "sm:grid-cols-3",
  4: "sm:grid-cols-2 xl:grid-cols-4",
  5: "sm:grid-cols-2 lg:grid-cols-5",
  auto: "[grid-template-columns:repeat(auto-fit,minmax(min(11rem,100%),1fr))]",
} as const

function SummaryStrip({
  className,
  columns = "auto",
  label = "数据概览",
  ...props
}: React.ComponentProps<"section"> & {
  columns?: keyof typeof SUMMARY_COLUMNS
  label?: string
}) {
  return (
    <section
      data-slot="summary-strip"
      aria-label={label}
      className={cn(
        "grid min-w-0 gap-2 rounded-xl border bg-muted/25 p-3",
        SUMMARY_COLUMNS[columns],
        className
      )}
      {...props}
    />
  )
}

export { PageFrame, PageHeader, StickyToolbar, SummaryStrip }
export type { PageFrameWidth }
