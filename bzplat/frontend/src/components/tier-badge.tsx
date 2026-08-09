import { cn } from '@/lib/utils'
import { tierFor, tierByKey, useGameTiers, type Tier } from '@/lib/tiers'

/** 段位徽章：评级 → 彩色 badge（浅/暗双色自适应，无紫色）。
 *
 * 支持 per-game 段位曲线。传 gameId 时按该游戏曲线取 badge 配色
 * （后端按游戏返回）；不传游戏时才使用展示默认曲线。
 * 优先用 tierKey（后端已返回 tier_key 时精确匹配），否则按 rating 推导。
 */
export function TierBadge({
  rating,
  label,
  className,
  tier,
  gameId,
  tierKey,
}: {
  rating?: number | null
  label?: string
  className?: string
  tier?: Tier
  /** 游戏id——传则用该游戏 per-game 曲线（段位与游戏挂钩） */
  gameId?: string | null
  /** 后端返回的 tier_key——优先用它精确匹配曲线（无需 rating 推导） */
  tierKey?: string | null
}) {
  const tiers = useGameTiers(gameId)
  const t = tier ?? (tiers
    ? tierKey ? tierByKey(tierKey, tiers) : tierFor(rating, tiers)
    : null)
  if (!t) {
    return (
      <span
        className={cn(
          'inline-flex items-center rounded-full bg-muted px-2 py-0.5 text-[10px] font-medium text-muted-foreground',
          className,
        )}
      >
        {label || '段位不可用'}
      </span>
    )
  }
  return (
    <span
      className={cn(
        'inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-medium',
        t.badge,
        className,
      )}
    >
      {label || t.name}
    </span>
  )
}
