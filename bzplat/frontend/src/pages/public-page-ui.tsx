import type { ReactNode } from 'react'
import { Copy } from 'lucide-react'
import { toast } from 'sonner'

import { Button } from '@/components/ui/button'
import { Identifier, OverflowText } from '@/components/ui/overflow-text'
import { cn } from '@/lib/utils'

function SummaryMetric({
  label,
  value,
  detail,
  icon,
  mono = true,
  className,
}: {
  label: string
  value: ReactNode
  detail?: ReactNode
  icon?: ReactNode
  mono?: boolean
  className?: string
}) {
  return (
    <div className={cn('flex min-w-0 items-start gap-2 rounded-lg px-2 py-1.5', className)}>
      {icon && (
        <span aria-hidden="true" className="mt-0.5 shrink-0 text-muted-foreground">
          {icon}
        </span>
      )}
      <div className="min-w-0 flex-1">
        <OverflowText className="text-[11px] font-medium tracking-wide text-muted-foreground uppercase">
          {label}
        </OverflowText>
        <OverflowText
          lines={2}
          tooltip={typeof value === 'string' || typeof value === 'number' ? String(value) : false}
          className={cn(
            'text-sm font-semibold text-foreground tabular-nums',
            mono && 'font-mono',
          )}
        >
          {value}
        </OverflowText>
        {detail && (
          <OverflowText lines={2} className="mt-0.5 text-xs text-muted-foreground">
            {detail}
          </OverflowText>
        )}
      </div>
    </div>
  )
}

function CopyIdentifier({
  value,
  label = '内部 ID',
  className,
}: {
  value: string | number | null | undefined
  label?: string
  className?: string
}) {
  if (value === null || value === undefined || value === '') return null
  const text = String(value)

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text)
      toast.success(`${label}已复制`)
    } catch {
      toast.error('复制失败，请手动选择标识')
    }
  }

  return (
    <span className={cn('inline-flex min-w-0 max-w-full items-center gap-1', className)}>
      <span className="shrink-0 text-[11px] text-muted-foreground">{label}</span>
      <Identifier tooltip={text} className="max-w-40">
        {text}
      </Identifier>
      <Button
        type="button"
        variant="ghost"
        size="icon-xs"
        onClick={() => void copy()}
        aria-label={`复制${label}`}
        className="text-muted-foreground"
      >
        <Copy aria-hidden="true" className="size-3" />
      </Button>
    </span>
  )
}

export { CopyIdentifier, SummaryMetric }
