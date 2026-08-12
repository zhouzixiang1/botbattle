import * as React from "react"

import { cn } from "@/lib/utils"

type TableScrollOwner = "parent" | "self"

const TableScrollOwnerContext = React.createContext<TableScrollOwner | null>(null)

/**
 * DataTable 是宽表唯一的显式滚动 owner。
 *
 * 内部 Table 会通过 context 自动关闭自己的 overflow，避免外层区域与 Table
 * 各生成一条横向滚动条。`data-scroll-region` / `data-overflow-allowed` 是截图与
 * 溢出审计的稳定契约。
 */
function DataTable({
  className,
  viewportClassName,
  scrollLabel,
  overflow = "x",
  children,
  ...props
}: React.ComponentProps<"div"> & {
  viewportClassName?: string
  scrollLabel?: string
  /** `both` 适合带 max-height 的局部数据窗；其 sticky 表头应使用 sticky="region"。 */
  overflow?: "x" | "both"
}) {
  return (
    <div
      data-slot="data-table"
      className={cn("min-w-0 overflow-hidden rounded-xl border bg-card", className)}
      {...props}
    >
      <div
        data-slot="data-table-viewport"
        data-scroll-region="table"
        data-overflow-allowed={overflow === "both" ? "both" : "x"}
        role={scrollLabel ? "region" : undefined}
        aria-label={scrollLabel}
        tabIndex={scrollLabel ? 0 : undefined}
        className={cn(
          "w-full min-w-0 overscroll-contain outline-none focus-visible:ring-[3px] focus-visible:ring-inset focus-visible:ring-ring/50",
          overflow === "both" ? "overflow-auto" : "overflow-x-auto",
          viewportClassName
        )}
      >
        <TableScrollOwnerContext.Provider value="parent">
          {children}
        </TableScrollOwnerContext.Provider>
      </div>
    </div>
  )
}

function Table({
  className,
  containerClassName,
  scrollOwner,
  ...props
}: React.ComponentProps<"table"> & {
  containerClassName?: string
  /** 兼容默认由 Table 自己滚动；放入 DataTable 后 context 自动切换为 `parent`。 */
  scrollOwner?: TableScrollOwner
}) {
  const contextOwner = React.useContext(TableScrollOwnerContext)
  const owner = scrollOwner ?? contextOwner ?? "self"
  const ownsScroll = owner === "self"
  const scrollLabel = typeof props["aria-label"] === "string" ? props["aria-label"] : undefined

  return (
    <div
      data-slot="table-container"
      data-scroll-region={ownsScroll ? "table" : undefined}
      data-overflow-allowed={ownsScroll ? "x" : undefined}
      role={ownsScroll && scrollLabel ? "region" : undefined}
      aria-label={ownsScroll ? scrollLabel : undefined}
      tabIndex={ownsScroll && scrollLabel ? 0 : undefined}
      className={cn(
        "relative w-full min-w-0 outline-none",
        ownsScroll
          ? "overflow-x-auto overscroll-x-contain focus-visible:ring-[3px] focus-visible:ring-inset focus-visible:ring-ring/50"
          : "overflow-visible",
        containerClassName
      )}
    >
      <table
        data-slot="table"
        className={cn("w-full caption-bottom text-sm", className)}
        {...props}
      />
    </div>
  )
}

function TableHeader({
  className,
  sticky = false,
  ...props
}: React.ComponentProps<"thead"> & { sticky?: boolean | "page" | "region" }) {
  const stickyMode = sticky === true ? "page" : sticky
  return (
    <thead
      data-slot="table-header"
      data-sticky-region={stickyMode ? "table-header" : undefined}
      className={cn(
        "bg-muted/40 [&_tr]:border-b [&_tr:hover]:bg-transparent",
        stickyMode &&
          "sticky z-[var(--z-sticky-table)] bg-card/95 shadow-[0_1px_0_var(--border)] backdrop-blur-sm",
        stickyMode === "page" && "top-[var(--sticky-table-offset)]",
        stickyMode === "region" && "top-0",
        className
      )}
      {...props}
    />
  )
}

function TableBody({ className, ...props }: React.ComponentProps<"tbody">) {
  return (
    <tbody
      data-slot="table-body"
      className={cn("[&_tr:last-child]:border-0", className)}
      {...props}
    />
  )
}

function TableFooter({ className, ...props }: React.ComponentProps<"tfoot">) {
  return (
    <tfoot
      data-slot="table-footer"
      className={cn(
        "border-t bg-muted/50 font-medium [&>tr]:last:border-b-0",
        className
      )}
      {...props}
    />
  )
}

function TableRow({ className, ...props }: React.ComponentProps<"tr">) {
  return (
    <tr
      data-slot="table-row"
      className={cn(
        "h-[var(--table-row-height)] border-b transition-colors duration-150 hover:bg-muted/50 has-aria-expanded:bg-muted/50 data-[state=selected]:bg-muted",
        className
      )}
      {...props}
    />
  )
}

function TableHead({ className, ...props }: React.ComponentProps<"th">) {
  return (
    <th
      data-slot="table-head"
      className={cn(
        "h-[var(--table-header-height)] px-3 text-left align-middle text-xs font-semibold tracking-wide whitespace-nowrap text-muted-foreground [&:has([role=checkbox])]:pr-0 [&>[role=checkbox]]:translate-y-[2px]",
        className
      )}
      {...props}
    />
  )
}

function TableCell({ className, ...props }: React.ComponentProps<"td">) {
  return (
    <td
      data-slot="table-cell"
      className={cn(
        "h-[var(--table-row-height)] px-3 py-1.5 align-middle whitespace-nowrap [&:has([role=checkbox])]:pr-0 [&>[role=checkbox]]:translate-y-[2px]",
        className
      )}
      {...props}
    />
  )
}

function TableCaption({
  className,
  ...props
}: React.ComponentProps<"caption">) {
  return (
    <caption
      data-slot="table-caption"
      className={cn("mt-3 text-sm text-muted-foreground", className)}
      {...props}
    />
  )
}

export {
  DataTable,
  Table,
  TableHeader,
  TableBody,
  TableFooter,
  TableHead,
  TableRow,
  TableCell,
  TableCaption,
}
export type { TableScrollOwner }
