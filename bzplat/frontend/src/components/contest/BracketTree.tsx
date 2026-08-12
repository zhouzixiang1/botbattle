/**
 * 淘汰赛对阵树（single_elimination 专用赛程表展示）。
 *
 * 读 contest_pairings 的 bracket_slot（standard seeding 槽位）+ match_winner，
 * 递归画树状对阵图：每轮一列，胜者高亮、连接线、轮次标题。
 * 大规模（如 500 人 = 9 轮 512 槽）→ 横向滚动 + 可选「折叠到指定轮次」只显示关注轮。
 *
 * bracket_slot 语义（来自 stages.py _seed_bracket）：
 *   首轮对阵 (slot0 vs slot1)、(slot2 vs slot3)…；slot 是 standard seed 顺序
 *   （1v种子 vs 末种子 等）。胜者按 slot//2 进入下一轮槽位。
 *   bye（None bot）不生成 pairing，该种子自动晋级。
 */
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { ChevronRight, ChevronDown } from 'lucide-react'
import { MatchNatureBadge, MatchParticipants } from '@/components/MatchParticipants'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { StatusBadge } from '@/components/ui/status'
import type { MatchParticipantSource } from '@/lib/match-participants'

export interface BracketPairing extends MatchParticipantSource {
  id: number
  round_num?: number
  bracket_slot?: number | null
  bot_a_id: number | null
  bot_b_id: number | null
  is_bye?: boolean
  match_winner?: number | null
  match_id?: string | null
  status?: string
  scheduled_at?: string | null
}

interface Props {
  pairings: BracketPairing[]
  /** 已完成轮数（用于默认展开到哪轮）；不传则展开全部 */
  completedRounds?: number
}

/** 排期时间格式化为紧凑的 MM-DD HH:mm（仅在展示空间有限的对阵卡里用）。 */
function fmtScheduled(iso: string | null | undefined): string | null {
  if (!iso) return null
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return null
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

export default function BracketTree({ pairings, completedRounds }: Props) {
  // 按轮次分组
  const rounds = useMemo(() => {
    const byRound = new Map<number, BracketPairing[]>()
    for (const p of pairings) {
      const r = p.round_num ?? 1
      if (!byRound.has(r)) byRound.set(r, [])
      byRound.get(r)!.push(p)
    }
    // 每轮内按 bracket_slot 排序（保证树结构对齐）
    return Array.from(byRound.entries())
      .sort(([a], [b]) => a - b)
      .map(([r, ps]) => ({
        round: r,
        pairings: ps.sort((a, b) => (a.bracket_slot ?? 0) - (b.bracket_slot ?? 0)),
      }))
  }, [pairings])

  const maxRound = rounds.length
  const [expandTo, setExpandTo] = useState<number>(
    completedRounds != null ? Math.min(completedRounds + 1, maxRound) : maxRound,
  )
  const [collapsed, setCollapsed] = useState<Set<number>>(new Set())
  const visibleRounds = rounds.filter((r) => r.round <= expandTo)

  // ── 连接线（SVG overlay）──────────────────────────────────────
  // 拓扑：(round R, bracket_slot s) → 下一轮 bracket_slot = s // 2
  const successorLookup = useMemo(() => {
    const map = new Map<string, BracketPairing>()
    for (const p of pairings) {
      const key = `${p.round_num ?? 1}:${p.bracket_slot ?? 0}`
      map.set(key, p)
    }
    return map
  }, [pairings])

  // pairing 卡片 ref map（key=pairing.id），供 getBoundingClientRect 量像素位置
  const cardRefs = useRef<Map<number, HTMLDivElement>>(new Map())
  const containerRef = useRef<HTMLDivElement>(null)
  const [paths, setPaths] = useState<{ d: string }[]>([])
  // 渲染后触发重量的计数器（依赖 expandTo / collapsed / pairings / resize）
  const [measureTick, setMeasureTick] = useState(0)

  const measure = () => {
    const container = containerRef.current
    if (!container) {
      setPaths([])
      return
    }
    const cRect = container.getBoundingClientRect()
    const next: { d: string }[] = []
    for (const p of pairings) {
      if (p.round_num == null || p.bracket_slot == null) continue
      const nextSlot = Math.floor(p.bracket_slot / 2)
      const target = successorLookup.get(`${p.round_num + 1}:${nextSlot}`)
      if (!target) continue
      const src = cardRefs.current.get(p.id)
      const dst = cardRefs.current.get(target.id)
      if (!src || !dst) continue
      const s = src.getBoundingClientRect()
      const d = dst.getBoundingClientRect()
      // 卡片相对容器坐标：RIGHT-center → LEFT-center
      const sx = s.right - cRect.left
      const sy = s.top + s.height / 2 - cRect.top
      const tx = d.left - cRect.left
      const ty = d.top + d.height / 2 - cRect.top
      // 三次贝塞尔：水平向右出发、从左侧水平进入目标（平滑的「S/L」连接）
      const cx = (sx + tx) / 2
      next.push({ d: `M ${sx} ${sy} C ${cx} ${sy}, ${cx} ${ty}, ${tx} ${ty}` })
    }
    setPaths(next)
  }

  // 首次渲染 + 依赖变化后重量（useLayoutEffect 避免闪烁）
  useLayoutEffect(() => {
    measure()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pairings, expandTo, collapsed, measureTick, successorLookup])

  // 浏览器窗口尺寸变化 → 卡片位置变了，重新量
  useEffect(() => {
    const onResize = () => setMeasureTick((t) => t + 1)
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])

  if (rounds.length === 0) {
    return <p className="py-6 text-center text-sm text-muted-foreground">暂无对阵</p>
  }

  const toggleCollapse = (round: number) => {
    setCollapsed((prev) => {
      const next = new Set(prev)
      if (next.has(round)) next.delete(round)
      else next.add(round)
      return next
    })
  }

  return (
    <div className="space-y-3">
      {/* 轮次跳转/折叠控制（大规模时有用） */}
      {maxRound > 3 && (
        <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
          <span>显示到轮次：</span>
          {rounds.map((r) => (
            <Button
              key={r.round}
              variant={r.round <= expandTo ? 'default' : 'outline'}
              size="xs"
              onClick={() => setExpandTo(r.round)}
            >
              R{r.round}
            </Button>
          ))}
          <Button variant="ghost" size="xs" onClick={() => setExpandTo(maxRound)}>
            全部
          </Button>
        </div>
      )}

      {/* 树状图：每轮一列，横向滚动；外层 relative 供 SVG overlay 定位 */}
      <div
        data-scroll-region="contest-bracket"
        data-overflow-allowed="x"
        role="region"
        aria-label="淘汰赛对阵树"
        tabIndex={0}
        className="min-w-0 overflow-x-auto overscroll-x-contain pb-2 outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50"
      >
        <div ref={containerRef} className="relative flex min-w-max gap-6">
          {/* 连接线层：覆盖整片对阵区，不拦截鼠标事件（点击穿透到卡片） */}
          <svg
            aria-hidden="true"
            className="text-muted-foreground/40 pointer-events-none absolute inset-0 h-full w-full"
            fill="none"
            stroke="currentColor"
            strokeWidth={1.5}
          >
            {paths.map((p, i) => (
              <path key={i} d={p.d} />
            ))}
          </svg>
          {visibleRounds.map((r) => {
            const isCollapsed = collapsed.has(r.round)
            const roundPanelId = `bracket-round-${r.round}`
            return (
              <div key={r.round} className="relative flex min-w-[220px] flex-col">
                <Button
                  type="button"
                  variant="ghost"
                  size="xs"
                  onClick={() => toggleCollapse(r.round)}
                  aria-expanded={!isCollapsed}
                  aria-controls={roundPanelId}
                  className="mb-2 w-full justify-start text-foreground hover:text-primary"
                >
                  {isCollapsed ? <ChevronRight aria-hidden="true" className="size-3.5" /> : <ChevronDown aria-hidden="true" className="size-3.5" />}
                  第 {r.round} 轮
                  <Badge variant="secondary" className="text-[10px]">{r.pairings.length}</Badge>
                </Button>
                {!isCollapsed && (
                  <div id={roundPanelId} className="space-y-2">
                    {r.pairings.map((p) => {
                      const w = p.match_winner
                      // 轮空由后端在裁掉 entry id 前显式派生；bot 被删也会令
                      // bot_b_id=NULL，不能据此猜成 bye。
                      const isBye = p.is_bye === true && p.status === 'completed'
                      const aWin = isBye || w === 0
                      const bWin = !isBye && w === 1
                      const scheduled = fmtScheduled(p.scheduled_at)
                      return (
                        <div
                          key={p.id}
                          ref={(el) => {
                            if (el) cardRefs.current.set(p.id, el)
                            else cardRefs.current.delete(p.id)
                          }}
                          className={`rounded-lg border bg-card p-2 text-xs shadow-sm ${
                            w != null ? 'border-primary/30' : 'border-border'
                          }`}
                        >
                          <MatchParticipants
                            source={p}
                            states={[
                              aWin ? 'winner' : bWin && !isBye ? 'loser' : 'neutral',
                              bWin ? 'winner' : aWin && !isBye ? 'loser' : 'neutral',
                            ]}
                            secondEmptyLabel={p.is_bye === true ? '轮空 (bye)' : undefined}
                            className="gap-1"
                          />
                          {/* 排期时间 + 状态徽章（紧凑展示） */}
                          <div className="mt-1 flex flex-wrap items-center justify-center gap-1.5">
                            <MatchNatureBadge matchType="contest" />
                            {scheduled && (
                              <span className="text-[10px] text-muted-foreground">{scheduled}</span>
                            )}
                            {p.status && p.status !== 'completed' && (
                              <StatusBadge status={p.status} />
                            )}
                          </div>
                          {p.match_id && p.status === 'completed' && (
                            <Link
                              to={`/match/${p.match_id}`}
                              className="mt-1 block text-center text-[10px] text-primary hover:underline"
                            >
                              查看
                            </Link>
                          )}
                        </div>
                      )
                    })}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      </div>
    </div>
  )
}
