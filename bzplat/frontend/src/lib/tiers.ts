/**
 * 段位映射（后端 games/<game>/tiers.py 镜像）。
 *
 * 段位按游戏独立。后端 /api/tiers?game_id= 返回对应曲线。
 * 本模块提供：
 * - 未指定游戏时使用展示默认曲线 TIERS
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

/** 未指定游戏上下文时的展示默认曲线。 */
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
const _tierRequests: Record<string, Promise<Tier[]> | undefined> = {}

/**
 * 拉取并缓存某游戏的段位曲线。同一 game_id 的并发首次请求共用一个
 * in-flight Promise；失败后立即清除 singleflight，下次调用可重试。失败必须
 * 显式上抛，不能伪装成另一游戏。
 */
export function fetchTiers(gameId: string): Promise<Tier[]> {
  if (_tierCache[gameId]) return Promise.resolve(_tierCache[gameId])
  const inFlight = _tierRequests[gameId]
  if (inFlight) return inFlight

  const request = (async () => {
    const r = await fetch(`/api/tiers?game_id=${encodeURIComponent(gameId)}`)
    if (!r.ok) throw new Error(`tiers ${r.status}`)
    const data = (await r.json()) as { tiers?: ServerTier[]; game_id?: string }
    if (data.game_id !== gameId || !Array.isArray(data.tiers) || data.tiers.length === 0) {
      throw new Error('tiers response does not match requested game')
    }
    const tiers = data.tiers.map(toTier)
    _tierCache[gameId] = tiers
    return tiers
  })()
  _tierRequests[gameId] = request
  request.then(
    () => {
      if (_tierRequests[gameId] === request) delete _tierRequests[gameId]
    },
    () => {
      if (_tierRequests[gameId] === request) delete _tierRequests[gameId]
    },
  )
  return request
}

/** React hook：按游戏取段位曲线（异步拉取 + 缓存）。 */
export function useGameTiers(gameId: string | null | undefined): Tier[] | null {
  const gid = findGame(gameId)?.id
  const hasGameContext = gameId !== null && gameId !== undefined && gameId !== ''
  const [tiers, setTiers] = useState<Tier[] | null>(() => (
    gid ? _tierCache[gid] ?? null : hasGameContext ? null : TIERS
  ))
  useEffect(() => {
    if (!gid) {
      setTiers(hasGameContext ? null : TIERS)
      return
    }
    setTiers(_tierCache[gid] ?? null)
    let cancelled = false
    void fetchTiers(gid)
      .then((t) => {
        if (!cancelled) setTiers(t)
      })
      .catch(() => {
        if (!cancelled) setTiers(null)
      })
    return () => {
      cancelled = true
    }
  }, [gid, hasGameContext])
  return tiers
}
