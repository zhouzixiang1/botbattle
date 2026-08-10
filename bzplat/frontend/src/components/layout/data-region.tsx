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
}) {
  const ownsScroll = overflow !== "none"
  const overflowMarker = overflow === "both" ? "both" : overflow

  return (
    <section
      data-slot="data-region"
      className={cn("min-w-0 rounded-xl border bg-card", className)}
      {...props}
    >
      {(title || description || actions) && (
        <header className="flex min-w-0 flex-col gap-2 border-b px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="min-w-0">
            {title && <h2 className="text-sm font-semibold text-foreground">{title}</h2>}
            {description && <div className="mt-0.5 text-xs text-muted-foreground">{description}</div>}
          </div>
          {actions && <div className="flex min-w-0 flex-wrap items-center gap-2">{actions}</div>}
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
