/** 段位映射（后端 engine/tiers.py 镜像，修改需同步） */

export interface Tier {
  level: number
  key: string
  name: string
  color: string
  bg: string
  min_rating: number
}

export const TIERS: Tier[] = [
  { level: 5, key: 'master', name: '大师', color: 'text-violet-700', bg: 'bg-violet-50', min_rating: 2200 },
  { level: 4, key: 'expert', name: '专家', color: 'text-indigo-700', bg: 'bg-indigo-50', min_rating: 2050 },
  { level: 3, key: 'gold', name: '高手', color: 'text-amber-700', bg: 'bg-amber-50', min_rating: 1900 },
  { level: 2, key: 'silver', name: '熟练', color: 'text-slate-700', bg: 'bg-slate-100', min_rating: 1750 },
  { level: 1, key: 'bronze', name: '进阶', color: 'text-emerald-700', bg: 'bg-emerald-50', min_rating: 1600 },
  { level: 0, key: 'novice', name: '新手', color: 'text-sky-700', bg: 'bg-sky-50', min_rating: 0 },
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
