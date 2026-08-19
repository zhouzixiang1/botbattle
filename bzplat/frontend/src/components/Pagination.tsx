/**
 * Pagination —— 通用分页组件。
 *
 * 显示页码 + 上一页/下一页 + 总条数。用于列表页分页（后端 page/per_page）。
 */
import { ChevronLeft, ChevronRight } from 'lucide-react'
import { Button } from '@/components/ui/button'

interface Props {
  page: number
  perPage: number
  total: number
  onPageChange: (page: number) => void
  ariaLabel?: string
  disabled?: boolean
}

export default function Pagination({
  page,
  perPage,
  total,
  onPageChange,
  ariaLabel = '分页导航',
  disabled = false,
}: Props) {
  const totalPages = Math.max(1, Math.ceil(total / perPage))
  if (totalPages <= 1) return null

  // 显示当前页 ±2 的页码 + 首尾 + 省略号
  const pages: (number | string)[] = []
  const add = (p: number | string) => pages.push(p)
  add(1)
  if (page - 2 > 2) add('...')
  for (let p = Math.max(2, page - 1); p <= Math.min(totalPages - 1, page + 1); p++) {
    add(p)
  }
  if (page + 2 < totalPages - 1) add('...')
  if (totalPages > 1) add(totalPages)

  return (
    <nav
      aria-label={ariaLabel}
      className="flex flex-wrap items-center justify-center gap-2 py-3 text-sm text-muted-foreground"
    >
      <span className="mr-2 max-sm:mr-0 max-sm:basis-full max-sm:text-center">共 {total} 条</span>
      <Button
        type="button"
        variant="outline"
        size="sm"
        aria-label="上一页"
        disabled={disabled || page <= 1}
        onClick={() => onPageChange(page - 1)}
        className="h-8 px-2 max-md:h-11 max-md:min-w-11"
      >
        <ChevronLeft aria-hidden="true" className="size-4" />
      </Button>
      {pages.map((p, i) =>
        typeof p === 'number' ? (
          <Button
            key={`${p}-${i}`}
            type="button"
            variant={p === page ? 'default' : 'outline'}
            size="sm"
            aria-label={`第 ${p} 页`}
            aria-current={p === page ? 'page' : undefined}
            disabled={disabled}
            onClick={() => onPageChange(p)}
            className="h-8 min-w-8 px-2 font-mono max-md:h-11 max-md:min-w-11"
          >
            {p}
          </Button>
        ) : (
          <span key={`ellipsis-${i}`} aria-hidden="true" className="px-1">…</span>
        ),
      )}
      <Button
        type="button"
        variant="outline"
        size="sm"
        aria-label="下一页"
        disabled={disabled || page >= totalPages}
        onClick={() => onPageChange(page + 1)}
        className="h-8 px-2 max-md:h-11 max-md:min-w-11"
      >
        <ChevronRight aria-hidden="true" className="size-4" />
      </Button>
    </nav>
  )
}
