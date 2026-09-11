import * as React from "react"

import { cn } from "@/lib/utils"

type DataRegionOverflow = "none" | "x" | "y" | "both"

const OVERFLOW_CLASS: Record<DataRegionOverflow, string> = {
  none: "overflow-visible",
  x: "overflow-x-auto overscroll-x-contain",
  y: "overflow-y-auto overscroll-y-contain",
  both: "overflow-auto overscroll-contain",
}

function DataRegion({
  className,
  contentClassName,
  title,
  description,
  actions,
  regionLabel,
  overflow = "none",
  density = "default",
  children,
  ...props
}: Omit<React.ComponentProps<"section">, "title"> & {
  title?: React.ReactNode
  description?: React.ReactNode
  actions?: React.ReactNode
  contentClassName?: string
  regionLabel?: string
  /** 表格请优先用 DataTable；该选项用于日志、源码等非表格区域。 */
  overflow?: DataRegionOverflow
  /** compact 收紧头部留白（px-3/py-2），供高密度页面替代父级任意选择器。 */
  density?: "default" | "compact"
}) {
  const ownsScroll = overflow !== "none"
  const overflowMarker = overflow === "both" ? "both" : overflow

  return (
    <section
      data-slot="data-region"
      data-density={density}
      className={cn("min-w-0 rounded-xl border bg-card", className)}
      {...props}
    >
      {(title || description || actions) && (
        <header
          className={cn(
            // flex-wrap：长 description 与宽 actions 同排放不下时 actions 换行到
            // 下一行，而不是两列对半挤压；ml-auto 保证换行后 actions 仍右对齐。
            "flex min-w-0 flex-col gap-2 border-b sm:flex-row sm:flex-wrap sm:items-center sm:justify-between",
            density === "compact" ? "px-3 py-2" : "px-4 py-3",
          )}
        >
          <div className="min-w-0">
            {title && <h2 className="text-sm font-semibold text-foreground">{title}</h2>}
            {description && <div className="mt-0.5 text-xs text-muted-foreground">{description}</div>}
          </div>
          {actions && <div className="ml-auto flex min-w-0 flex-wrap items-center gap-2 sm:justify-end">{actions}</div>}
        </header>
      )}
      <div
        data-slot="data-region-content"
        data-scroll-region={ownsScroll ? "data-region" : undefined}
        data-overflow-allowed={ownsScroll ? overflowMarker : undefined}
        role={ownsScroll && (regionLabel || typeof title === "string") ? "region" : undefined}
        aria-label={ownsScroll ? (regionLabel ?? (typeof title === "string" ? title : undefined)) : undefined}
        tabIndex={ownsScroll && (regionLabel || typeof title === "string") ? 0 : undefined}
        className={cn(
          "min-w-0 outline-none focus-visible:ring-[3px] focus-visible:ring-inset focus-visible:ring-ring/50",
          OVERFLOW_CLASS[overflow],
          contentClassName
        )}
      >
        {children}
      </div>
    </section>
  )
}

export { DataRegion }
export type { DataRegionOverflow }
