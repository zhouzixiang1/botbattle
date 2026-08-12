import * as React from "react"

import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { cn } from "@/lib/utils"

type OverflowTextProps = React.ComponentProps<"span"> & {
  /** 多行截断；默认单行。 */
  lines?: 1 | 2 | 3
  /** false 禁用 tooltip；字符串可覆盖 children 的完整文本。 */
  tooltip?: string | false
  /** 独立文本默认可用键盘聚焦查看 tooltip；嵌在 Link/Button 内时应设为 false。 */
  tooltipFocusable?: boolean
}

function OverflowText({
  className,
  children,
  lines = 1,
  tooltip,
  tooltipFocusable = true,
  style,
  tabIndex,
  ...props
}: OverflowTextProps) {
  const ref = React.useRef<HTMLSpanElement>(null)
  const [overflowing, setOverflowing] = React.useState(false)

  React.useLayoutEffect(() => {
    const element = ref.current
    if (!element) return

    const measure = () => {
      setOverflowing(
        element.scrollWidth > element.clientWidth + 1 ||
          element.scrollHeight > element.clientHeight + 1
      )
    }

    measure()
    if (typeof ResizeObserver === "undefined") return
    const observer = new ResizeObserver(measure)
    observer.observe(element)
    return () => observer.disconnect()
  }, [children, lines, tooltip])

  const fullText =
    tooltip === false
      ? undefined
      : typeof tooltip === "string"
        ? tooltip
        : typeof children === "string" || typeof children === "number"
          ? String(children)
          : undefined

  const text = (
    <span
      ref={ref}
      data-slot="overflow-text"
      data-overflowing={overflowing ? "true" : "false"}
      tabIndex={tooltipFocusable && overflowing && fullText ? (tabIndex ?? 0) : tabIndex}
      className={cn(
        "block min-w-0 max-w-full",
        lines === 1 ? "truncate" : "overflow-hidden [display:-webkit-box] [-webkit-box-orient:vertical]",
        className
      )}
      style={lines === 1 ? style : { ...style, WebkitLineClamp: lines }}
      {...props}
    >
      {children}
    </span>
  )

  if (!fullText) return text

  return (
    <Tooltip>
      <TooltipTrigger asChild>{text}</TooltipTrigger>
      {overflowing && <TooltipContent className="max-w-sm break-all">{fullText}</TooltipContent>}
    </Tooltip>
  )
}

function EntityName({
  className,
  children,
  fallback = "—",
  ...props
}: OverflowTextProps & { fallback?: React.ReactNode }) {
  const content = children === null || children === undefined || children === "" ? fallback : children
  return (
    <OverflowText
      data-slot="entity-name"
      className={cn("font-medium text-foreground", className)}
      {...props}
    >
      {content}
    </OverflowText>
  )
}

function Identifier({
  className,
  children,
  fallback = "—",
  ...props
}: OverflowTextProps & { fallback?: React.ReactNode }) {
  const content = children === null || children === undefined || children === "" ? fallback : children
  return (
    <OverflowText
      data-slot="identifier"
      dir="ltr"
      className={cn("font-mono text-xs tabular-nums text-muted-foreground", className)}
      {...props}
    >
      {content}
    </OverflowText>
  )
}

export { EntityName, Identifier, OverflowText }
export type { OverflowTextProps }
