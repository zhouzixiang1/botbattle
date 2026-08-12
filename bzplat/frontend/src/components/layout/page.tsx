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
      // --sticky-table-offset 在 :root 计算时只看得到初始的 0px 高度；
      // 必须在同一 PageFrame 重新声明派生值，表头才能继承本工具栏的真实高度。
      page.style.setProperty(
        "--sticky-table-offset",
        "calc(var(--sticky-page-offset) + var(--sticky-toolbar-height) + var(--sticky-toolbar-gap))",
      )
    }

    updateHeight()
    const observer =
      typeof ResizeObserver === "undefined" ? undefined : new ResizeObserver(updateHeight)
    observer?.observe(toolbar)
    return () => {
      observer?.disconnect()
      page.style.removeProperty("--sticky-toolbar-height")
      page.style.removeProperty("--sticky-toolbar-gap")
      page.style.removeProperty("--sticky-table-offset")
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
        "sticky top-[var(--sticky-page-offset)] z-[var(--z-sticky-toolbar)] flex min-w-0 flex-wrap items-center gap-2 rounded-lg border bg-background/95 px-2 py-2 shadow-sm backdrop-blur-sm before:pointer-events-none before:absolute before:inset-x-0 before:bottom-full before:h-[var(--sticky-page-gap)] before:bg-background before:content-[''] after:pointer-events-none after:absolute after:inset-x-0 after:top-full after:h-[var(--sticky-toolbar-gap)] after:bg-background after:content-[''] supports-[backdrop-filter]:bg-background/85",
        className
      )}
      {...props}
    >
      {children}
    </div>
  )
}

export { PageFrame, PageHeader, StickyToolbar }
export type { PageFrameWidth }
