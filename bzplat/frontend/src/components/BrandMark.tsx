import { cn } from '@/lib/utils'

/**
 * 平台品牌标识：圆角方块「B」+ 文字，与顶栏 Logo 保持一致。
 * 用于 auth 页面等需要品牌引导的场景。
 */
export default function BrandMark({
  className,
  size = 'md',
  withText = true,
}: {
  className?: string
  size?: 'sm' | 'md' | 'lg'
  withText?: boolean
}) {
  const box =
    size === 'lg' ? 'size-12 text-lg' : size === 'sm' ? 'size-7 text-xs' : 'size-8 text-sm'
  const text = size === 'lg' ? 'text-2xl' : size === 'sm' ? 'text-base' : 'text-lg'
  return (
    <span className={cn('flex items-center gap-2 font-semibold tracking-tight text-foreground', className)}>
      <span
        className={cn(
          'flex shrink-0 items-center justify-center rounded-lg bg-primary font-bold text-primary-foreground shadow-soft',
          box,
        )}
      >
        B
      </span>
      {withText && <span className={cn('font-display', text)}>Botbattle</span>}
    </span>
  )
}
