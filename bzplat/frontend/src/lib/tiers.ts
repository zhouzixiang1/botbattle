/** 段位映射（后端 engine/tiers.py 镜像，修改需同步）。
 * 配色统一到 emerald + amber + teal + slate 有限调色板（无紫色，对齐设计原则），
 * 每档提供浅/暗双色 badge 类名。 */

export interface Tier {
  level: number
  key: string
  name: string
  /** 徽章 className（浅/暗自适应，用 token） */
  badge: string
  /** 强调点色（用于趋势/图标） */
  accent: string
  min_rating: number
}

export const TIERS: Tier[] = [
  {
    level: 5,
    key: 'master',
    name: '大师',
    badge: 'bg-amber-100 text-amber-800 dark:bg-amber-500/15 dark:text-amber-300',
    accent: 'text-amber-600 dark:text-amber-400',
    min_rating: 2200,
  },
  {
    level: 4,
    key: 'expert',
    name: '专家',
    badge: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-500/15 dark:text-emerald-300',
    accent: 'text-emerald-600 dark:text-emerald-400',
    min_rating: 2050,
  },
  {
    level: 3,
    key: 'gold',
    name: '高手',
    badge: 'bg-primary/15 text-primary dark:bg-primary/20 dark:text-primary',
    accent: 'text-primary',
    min_rating: 1900,
  },
  {
    level: 2,
    key: 'silver',
    name: '熟练',
    badge: 'bg-slate-200 text-slate-700 dark:bg-slate-700/50 dark:text-slate-300',
    accent: 'text-slate-600 dark:text-slate-400',
    min_rating: 1750,
  },
  {
    level: 1,
    key: 'bronze',
    name: '进阶',
    badge: 'bg-teal-100 text-teal-800 dark:bg-teal-500/15 dark:text-teal-300',
    accent: 'text-teal-600 dark:text-teal-400',
    min_rating: 1600,
  },
  {
    level: 0,
    key: 'novice',
    name: '新手',
    badge: 'bg-muted text-muted-foreground',
    accent: 'text-muted-foreground',
    min_rating: 0,
  },
]

export function tierFor(rating: number | null | undefined): Tier {
  if (rating == null) return TIERS[TIERS.length - 1]
  const r = Number(rating)
  for (const t of TIERS) {
    if (r >= t.min_rating) return t
  }
  return TIERS[TIERS.length - 1]
}

export function trendDelta(delta: number | null | undefined): { up: boolean; abs: number } | null {
  if (delta == null || delta === 0) return null
  return { up: delta > 0, abs: Math.abs(delta) }
}
