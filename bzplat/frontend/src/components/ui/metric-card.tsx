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
        <div className="min-w-0">
          <div className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            {label}
          </div>
          <div
            className={cn(
              'mt-1 break-all font-mono text-lg font-bold tabular-nums sm:text-xl',
              danger ? 'text-destructive' : 'text-foreground'
            )}
          >
            {value}
          </div>
          {hint && <div className="mt-0.5 text-xs text-muted-foreground">{hint}</div>}
        </div>
        {icon && <div className="shrink-0 text-muted-foreground opacity-70">{icon}</div>}
      </CardContent>
    </Card>
  )
}
