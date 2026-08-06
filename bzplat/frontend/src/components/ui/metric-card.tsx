import { type ReactNode } from 'react'
import { cn } from '@/lib/utils'
import { Card, CardContent } from '@/components/ui/card'

/* ── 指标卡：用于 Dashboard / Bot 详情 / 用户主页的数值展示 ── */

export function MetricCard({
  label,
  value,
  hint,
  danger,
  icon,
  className,
}: {
  label: string
  value: ReactNode
  hint?: string
  danger?: boolean
  icon?: ReactNode
  className?: string
}) {
  return (
    <Card className={cn('gap-0 py-4', className)}>
      <CardContent className="flex items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          <div className="truncate text-xs font-medium uppercase tracking-wide text-muted-foreground">
            {label}
          </div>
          <div
            className={cn(
              'mt-1 w-full truncate font-mono text-base font-bold tabular-nums sm:text-lg lg:text-xl',
              danger ? 'text-destructive' : 'text-foreground'
            )}
            title={typeof value === 'string' || typeof value === 'number' ? String(value) : undefined}
          >
            {value}
          </div>
          {hint && <div className="mt-0.5 truncate text-xs text-muted-foreground" title={hint}>{hint}</div>}
        </div>
        {icon && <div className="shrink-0 text-muted-foreground opacity-70">{icon}</div>}
      </CardContent>
    </Card>
  )
}
