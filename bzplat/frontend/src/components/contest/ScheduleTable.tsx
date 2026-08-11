/**
 * 赛程一览表：所有对阵的扁平表格视图（对阵树的互补呈现）。
 *
 * 列：轮次 · 座位1 Bot · 座位2 Bot · 排期时间 · 状态 · 查看。
 * 行按 round_num 分组排序（同轮内按 bracket_slot/id）。淘汰赛/瑞士/循环通用。
 * 与 BracketTree 共享 Pairing 形状（bye 支持：bot_b_id 可为 null）。
 * 客户端分页（per_page=30）：大规模对阵（如瑞士轮 60+ 场）避免一次性渲染过长表格。
 */
import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { MatchNatureBadge, MatchParticipantIdentity } from '@/components/MatchParticipants'
import {
  DataTable,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { Button } from '@/components/ui/button'
import { StatusBadge } from '@/components/ui/status'
import { fmtTime } from '@/lib/format'
import type { MatchParticipantSource } from '@/lib/match-participants'
import Pagination from '@/components/Pagination'

export interface SchedulePairing extends MatchParticipantSource {
  id: number
  round_num?: number
  bracket_slot?: number | null
  bot_a_id: number | null
  bot_b_id: number | null
  is_bye?: boolean
  match_id?: string | null
  status?: string
  match_winner?: number | null
  scheduled_at?: string | null
}

interface Props {
  pairings: SchedulePairing[]
}

const PER_PAGE = 30

export default function ScheduleTable({ pairings }: Props) {
  const [page, setPage] = useState(1)

  // 扁平化为有序行：先按 round_num 分组排序，组内按 bracket_slot（淘汰）或 id（其他）排序。
  // 预计算每行是否为「本轮首行」isRoundStart，使分页切片后轮次徽章显示仍正确。
  const rows = useMemo(() => {
    const byRound = new Map<number, SchedulePairing[]>()
    for (const p of pairings) {
      const r = p.round_num ?? 1
      if (!byRound.has(r)) byRound.set(r, [])
      byRound.get(r)!.push(p)
    }
    const sortedRounds = Array.from(byRound.entries())
      .sort(([a], [b]) => a - b)
      .map(([round, ps]) => ({
        round,
        pairings: ps.sort((a, b) => {
          const sa = a.bracket_slot ?? 0
          const sb = b.bracket_slot ?? 0
          if (sa !== sb) return sa - sb
          return a.id - b.id
        }),
      }))
    const out: Array<{ pairing: SchedulePairing; round: number; isRoundStart: boolean }> = []
    for (const r of sortedRounds) {
      r.pairings.forEach((p, idx) => out.push({ pairing: p, round: r.round, isRoundStart: idx === 0 }))
    }
    return out
  }, [pairings])

  // 客户端分页（越界回退：对阵总数缩短到当前页之外时夹紧到末页）
  const totalPages = Math.max(1, Math.ceil(rows.length / PER_PAGE))
  const safePage = Math.min(page, totalPages)
  const pageRows = rows.slice((safePage - 1) * PER_PAGE, safePage * PER_PAGE)

  if (pairings.length === 0) {
    return <p className="py-6 text-center text-sm text-muted-foreground">暂无对阵</p>
  }

  return (
    <div className="min-w-0 space-y-2">
      <DataTable scrollLabel="赛事对阵一览表">
      <Table className="min-w-[40rem]" aria-label="赛事对阵一览表">
        <TableHeader>
          <TableRow>
            <TableHead className="w-16">轮次</TableHead>
            <TableHead className="min-w-[8rem]">座位 1</TableHead>
            <TableHead className="min-w-[8rem]">座位 2</TableHead>
            <TableHead className="w-36">排期时间</TableHead>
            <TableHead className="w-28">状态 / 性质</TableHead>
            <TableHead className="w-16 text-right">查看</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {pageRows.map(({ pairing: p, round, isRoundStart }) => {
            const w = p.match_winner
            const isBye = p.is_bye === true
            // 胜者着色：a 胜 → 座位1 高亮；b 胜 → 座位2 高亮（bye 时 a 自动晋级）
            const aWin = (isBye && p.status === 'completed') || w === 0
            const bWin = !isBye && w === 1
            return (
              <TableRow key={p.id}>
                {/* 仅每轮首行显示轮次徽章，避免重复噪音 */}
                <TableCell className="font-mono text-xs text-muted-foreground">
                  {isRoundStart ? `R${round}` : ''}
                </TableCell>
                <TableCell className="max-w-[12rem]">
                  <MatchParticipantIdentity
                    source={p}
                    side={0}
                    state={aWin ? 'winner' : bWin ? 'loser' : 'neutral'}
                  />
                </TableCell>
                <TableCell className="max-w-[12rem]">
                  <MatchParticipantIdentity
                    source={p}
                    side={1}
                    state={bWin ? 'winner' : aWin && !isBye ? 'loser' : 'neutral'}
                    emptyLabel={isBye ? '轮空 (bye)' : undefined}
                  />
                </TableCell>
                <TableCell className="whitespace-nowrap text-xs text-muted-foreground">
                  {p.scheduled_at ? fmtTime(p.scheduled_at) : '—'}
                </TableCell>
                <TableCell>
                  <div className="flex flex-col items-start gap-1">
                    <StatusBadge status={p.status || 'pending'} />
                    <MatchNatureBadge matchType="contest" />
                  </div>
                </TableCell>
                <TableCell className="text-right">
                  {p.match_id ? (
                    <Button asChild variant="ghost" size="xs" className="text-primary">
                      <Link to={`/match/${p.match_id}`}>查看</Link>
                    </Button>
                  ) : (
                    <span className="text-xs text-muted-foreground">—</span>
                  )}
                </TableCell>
              </TableRow>
            )
          })}
        </TableBody>
      </Table>
      </DataTable>
      <Pagination page={safePage} perPage={PER_PAGE} total={rows.length} onPageChange={setPage} />
    </div>
  )
}
