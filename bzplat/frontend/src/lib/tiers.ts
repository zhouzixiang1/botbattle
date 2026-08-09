/**
 * 段位映射（后端 games/<game>/tiers.py 镜像）。
 *
 * 全面解耦 PR-C：段位 per-game。后端 /api/tiers?game_id= 返回各游戏曲线。
 * 本模块提供：
 * - 默认全局曲线 TIERS（向后兼容 + 首次加载兜底）
 * - fetchTiers(gameId)：按游戏拉曲线（带缓存）
 * - tierFor(rating, gameTier?)：在指定曲线里查段位
 * - useGameTiers(gameId)：React hook，按游戏缓存曲线
 *
 * 配色统一到 emerald + amber + teal + slate 有限调色板（无紫色，对齐设计原则），
 * 每档提供浅/暗双色 badge 类名。
 */
import { useEffect, useState } from 'react'
import { findGame } from '@/games'

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

/** 后端 /api/tiers 返回的段位定义（无 badge/accent 类名——前端本地配色）。 */
interface ServerTier {
  level: number
  key: string
  name: string
  min_rating: number
}

/** 按 tier.key 配 badge/accent 类名（前端本地配色，统一调色板）。 */
const BADGE_BY_KEY: Record<string, { badge: string; accent: string }> = {
  master: {
    badge: 'bg-amber-100 text-amber-800 dark:bg-amber-500/15 dark:text-amber-300',
    accent: 'text-amber-600 dark:text-amber-400',
  },
  expert: {
    badge: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-500/15 dark:text-emerald-300',
    accent: 'text-emerald-600 dark:text-emerald-400',
  },
  gold: {
    badge: 'bg-primary/15 text-primary dark:bg-primary/20 dark:text-primary',
    accent: 'text-primary',
  },
  silver: {
    badge: 'bg-slate-200 text-slate-700 dark:bg-slate-700/50 dark:text-slate-300',
    accent: 'text-slate-600 dark:text-slate-400',
  },
  bronze: {
    badge: 'bg-teal-100 text-teal-800 dark:bg-teal-500/15 dark:text-teal-300',
    accent: 'text-teal-600 dark:text-teal-400',
  },
  novice: {
    badge: 'bg-muted text-muted-foreground',
    accent: 'text-muted-foreground',
  },
}

/** 把后端 ServerTier 转成前端 Tier（补 badge/accent）。 */
function toTier(s: ServerTier): Tier {
  const cls = BADGE_BY_KEY[s.key] ?? BADGE_BY_KEY.novice
  return { ...s, badge: cls.badge, accent: cls.accent }
}

/** 默认全局曲线（向后兼容 + 首次加载兜底；与后端 holdem 初始阈值一致）。 */
export const TIERS: Tier[] = [
  { level: 5, key: 'master', name: '大师', ...BADGE_BY_KEY.master, min_rating: 2200 },
  { level: 4, key: 'expert', name: '专家', ...BADGE_BY_KEY.expert, min_rating: 2050 },
  { level: 3, key: 'gold', name: '高手', ...BADGE_BY_KEY.gold, min_rating: 1900 },
  { level: 2, key: 'silver', name: '熟练', ...BADGE_BY_KEY.silver, min_rating: 1750 },
  { level: 1, key: 'bronze', name: '进阶', ...BADGE_BY_KEY.bronze, min_rating: 1600 },
  { level: 0, key: 'novice', name: '新手', ...BADGE_BY_KEY.novice, min_rating: 0 },
]

/** 在指定曲线里按 rating 查段位。 */
export function tierFor(rating: number | null | undefined, tiers: Tier[] = TIERS): Tier {
  if (rating == null) return tiers[tiers.length - 1]
  const r = Number(rating)
  for (const t of tiers) {
    if (r >= t.min_rating) return t
  }
  return tiers[tiers.length - 1]
}

/** 按 tier.key 在指定曲线里精确取段位（后端已返回 key 时用，无需 rating 推导）。 */
export function tierByKey(key: string | undefined, tiers: Tier[] = TIERS): Tier {
  if (!key) return tiers[tiers.length - 1]
  return tiers.find((t) => t.key === key) ?? tiers[tiers.length - 1]
}

export function trendDelta(delta: number | null | undefined): { up: boolean; abs: number } | null {
  if (delta == null || delta === 0) return null
  return { up: delta > 0, abs: Math.abs(delta) }
}

// ── per-game 曲线拉取（带缓存）────────────────────────────────
const _tierCache: Record<string, Tier[]> = {}

/** 拉取并缓存某游戏的段位曲线（从 /api/tiers?game_id=）。失败回退全局曲线。 */
export async function fetchTiers(gameId: string): Promise<Tier[]> {
  if (_tierCache[gameId]) return _tierCache[gameId]
  try {
    const r = await fetch(`/api/tiers?game_id=${encodeURIComponent(gameId)}`)
    if (!r.ok) throw new Error(`tiers ${r.status}`)
    const data = (await r.json()) as { tiers: ServerTier[] }
    const tiers = (data.tiers || []).map(toTier)
    _tierCache[gameId] = tiers
    return tiers
  } catch {
    _tierCache[gameId] = TIERS
    return TIERS
  }
}

/** React hook：按游戏取段位曲线（异步拉取 + 缓存）。 */
export function useGameTiers(gameId: string | null | undefined): Tier[] {
  const [tiers, setTiers] = useState<Tier[]>(TIERS)
  const gid = findGame(gameId)?.id
  useEffect(() => {
    if (!gid) {
      setTiers(TIERS)
      return
    }
    let cancelled = false
    void fetchTiers(gid).then((t) => {
      if (!cancelled) setTiers(t)
    })
    return () => {
      cancelled = true
    }
  }, [gid])
  return tiers
}
