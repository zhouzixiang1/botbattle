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
import { useState, useMemo } from 'react'
import { Link } from 'react-router-dom'
import { ChevronRight, ChevronDown } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'

export interface BracketPairing {
  id: number
  round_num?: number
  bracket_slot?: number | null
  bot_a_id: number
  bot_b_id: number
  bot_a_name?: string
  bot_a_display?: string
  bot_b_name?: string
  bot_b_display?: string
  match_winner?: number | null
  match_id?: string | null
  status?: string
}

interface Props {
  pairings: BracketPairing[]
  /** 已完成轮数（用于默认展开到哪轮）；不传则展开全部 */
  completedRounds?: number
}

function botLabel(p: BracketPairing, side: 0 | 1): string {
  if (side === 0) return p.bot_a_display || p.bot_a_name || `#${p.bot_a_id}`
  return p.bot_b_display || p.bot_b_name || `#${p.bot_b_id}`
}

function botId(p: BracketPairing, side: 0 | 1): number {
  return side === 0 ? p.bot_a_id : p.bot_b_id
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
              size="sm"
              className="h-6 px-2 text-[11px]"
              onClick={() => setExpandTo(r.round)}
            >
              R{r.round}
            </Button>
          ))}
          <Button variant="ghost" size="sm" className="h-6 text-[11px]" onClick={() => setExpandTo(maxRound)}>
            全部
          </Button>
        </div>
      )}

      {/* 树状图：每轮一列，横向滚动 */}
      <div className="overflow-x-auto pb-2">
        <div className="flex min-w-max gap-6">
          {visibleRounds.map((r) => {
            const isCollapsed = collapsed.has(r.round)
            return (
              <div key={r.round} className="flex min-w-[180px] flex-col">
                <button
                  type="button"
                  onClick={() => toggleCollapse(r.round)}
                  className="mb-2 flex items-center gap-1 text-xs font-semibold text-foreground hover:text-primary"
                >
                  {isCollapsed ? <ChevronRight className="size-3.5" /> : <ChevronDown className="size-3.5" />}
                  第 {r.round} 轮
                  <Badge variant="secondary" className="text-[10px]">{r.pairings.length}</Badge>
                </button>
                {!isCollapsed && (
                  <div className="space-y-2">
                    {r.pairings.map((p) => {
                      const w = p.match_winner
                      const aWin = w === 0
                      const bWin = w === 1
                      return (
                        <div
                          key={p.id}
                          className={`rounded-lg border bg-card p-2 text-xs shadow-sm ${
                            w != null ? 'border-primary/30' : 'border-border'
                          }`}
                        >
                          <SlotRow
                            label={botLabel(p, 0)}
                            botId={botId(p, 0)}
                            win={aWin}
                            lose={w === 1}
                          />
                          <div className="my-0.5 text-center text-[10px] text-muted-foreground">vs</div>
                          <SlotRow
                            label={botLabel(p, 1)}
                            botId={botId(p, 1)}
                            win={bWin}
                            lose={w === 0}
                          />
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

function SlotRow({
  label,
  botId,
  win,
  lose,
}: {
  label: string
  botId: number
  win: boolean
  lose: boolean
}) {
  return (
    <div
      className={`flex items-center gap-1 rounded px-1.5 py-0.5 ${
        win ? 'bg-success/10 font-semibold text-success' : lose ? 'text-muted-foreground line-through' : 'text-foreground'
      }`}
    >
      <Link to={`/bot/${botId}`} className="min-w-0 flex-1 truncate hover:text-primary">
        {label || '—'}
      </Link>
      {win && <span className="text-[10px]">✓</span>}
    </div>
  )
}
