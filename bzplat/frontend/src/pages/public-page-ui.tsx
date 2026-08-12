import { Copy } from 'lucide-react'
import { toast } from 'sonner'

import { Button } from '@/components/ui/button'
import { Identifier } from '@/components/ui/overflow-text'
import { cn } from '@/lib/utils'

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

export { CopyIdentifier }
