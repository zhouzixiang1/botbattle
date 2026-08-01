import { cn } from '@/lib/utils'
import { tierFor, type Tier } from '@/lib/tiers'

/** 段位徽章：评级 → 彩色 badge（浅/暗双色自适应，无紫色） */
export function TierBadge({
  rating,
  label,
  className,
  tier,
}: {
  rating?: number | null
  label?: string
  className?: string
  tier?: Tier
}) {
  const t = tier ?? tierFor(rating)
  return (
    <span
      className={cn(
        'inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-medium',
        t.badge,
        className
      )}
    >
      {label || t.name}
    </span>
  )
}
