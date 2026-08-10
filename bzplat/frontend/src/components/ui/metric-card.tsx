import { type ReactNode } from 'react'
import { cn } from '@/lib/utils'
import { Card, CardContent } from '@/components/ui/card'
import { OverflowText } from '@/components/ui/overflow-text'

/* ── 指标卡：用于 Dashboard / Bot 详情 / 用户主页的数值展示 ──
 * plain=true 时不渲染外层 Card（无边框无阴影），用于嵌套进另一个 Card 的指标网格
 * （避免 Card 套 Card 的双边框/双阴影）；顶层独立网格（如 Dashboard）用默认带框样式。 */

export function MetricCard({
  label,
  value,
  hint,
  danger,
  icon,
  plain,
  className,
}: {
  label: string
  value: ReactNode
  hint?: string
  danger?: boolean
  icon?: ReactNode
  /** 嵌套进另一个 Card 时用 plain（无边框，仅弱背景），避免双边框 */
  plain?: boolean
  className?: string
}) {
  const body = (
    <div className="flex items-start justify-between gap-2">
      <div className="min-w-0 flex-1">
        <OverflowText className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
          {label}
        </OverflowText>
        <OverflowText
          className={cn(
            'mt-1 w-full font-mono text-base font-bold tabular-nums sm:text-lg lg:text-xl',
            danger ? 'text-destructive' : 'text-foreground'
          )}
          tooltip={typeof value === 'string' || typeof value === 'number' ? String(value) : false}
        >
          {value}
        </OverflowText>
        {hint && <OverflowText className="mt-0.5 text-xs text-muted-foreground">{hint}</OverflowText>}
      </div>
      {icon && <div aria-hidden="true" className="shrink-0 text-muted-foreground opacity-70">{icon}</div>}
    </div>
  )
  if (plain) {
    return (
      <div className={cn('rounded-lg bg-muted/40 px-3 py-3', className)}>
        {body}
      </div>
    )
  }
  return (
    <Card className={cn('gap-0 py-4', className)}>
      <CardContent>{body}</CardContent>
    </Card>
  )
}
