/**
 * 赛程一览表：所有对阵的扁平表格视图（对阵树的互补呈现）。
 *
 * 列：轮次 · 座位1 Bot · 座位2 Bot · 排期时间 · 状态 · 查看。
 * 行按 round_num 分组排序（同轮内按 bracket_slot/id）。淘汰赛/瑞士/循环通用。
 * 与 BracketTree 共享 Pairing 形状（bye 支持：bot_b_id 可为 null）。
 */
import { Link } from 'react-router-dom'
import {
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

export interface SchedulePairing {
  id: number
  round_num?: number
  bracket_slot?: number | null
  bot_a_id: number
  bot_b_id: number | null
  bot_a_name?: string
  bot_a_display?: string
  bot_b_name?: string
  bot_b_display?: string
  match_id?: string | null
  status?: string
  match_winner?: number | null
  scheduled_at?: string | null
}

interface Props {
  pairings: SchedulePairing[]
}

export default function ScheduleTable({ pairings }: Props) {
  // 按 round_num 分组（缺省归 1 轮），组内按 bracket_slot（淘汰）或 id（其他）排序
  const rounds = (() => {
    const byRound = new Map<number, SchedulePairing[]>()
    for (const p of pairings) {
      const r = p.round_num ?? 1
      if (!byRound.has(r)) byRound.set(r, [])
      byRound.get(r)!.push(p)
    }
    return Array.from(byRound.entries())
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
  })()

  if (pairings.length === 0) {
    return <p className="py-6 text-center text-sm text-muted-foreground">暂无对阵</p>
  }

  return (
    <div className="overflow-x-auto">
      <Table className="min-w-[40rem]">
        <TableHeader>
          <TableRow>
            <TableHead className="w-16">轮次</TableHead>
            <TableHead className="min-w-[8rem]">座位 1</TableHead>
            <TableHead className="min-w-[8rem]">座位 2</TableHead>
            <TableHead className="w-36">排期时间</TableHead>
            <TableHead className="w-24">状态</TableHead>
            <TableHead className="w-16 text-right">查看</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {rounds.map((r) =>
            r.pairings.map((p, idx) => {
              const w = p.match_winner
              const isBye = p.bot_b_id == null
              // 胜者着色：a 胜 → 座位1 高亮；b 胜 → 座位2 高亮（bye 时 a 自动晋级）
              const aWin = (isBye && p.status === 'completed') || w === 0
              const bWin = !isBye && w === 1
              return (
                <TableRow key={p.id}>
                  {/* 仅每轮首行显示轮次徽章，避免重复噪音 */}
                  <TableCell className="font-mono text-xs text-muted-foreground">
                    {idx === 0 ? `R${r.round}` : ''}
                  </TableCell>
                  <TableCell className="max-w-[12rem]">
                    <Link
                      to={`/bot/${p.bot_a_id}`}
                      title={p.bot_a_display || p.bot_a_name || `#${p.bot_a_id}`}
                      className={`block truncate hover:text-primary ${
                        aWin ? 'font-semibold text-success' : w === 1 ? 'text-muted-foreground' : 'text-foreground'
                      }`}
                    >
                      {p.bot_a_display || p.bot_a_name || `#${p.bot_a_id}`}
                    </Link>
                  </TableCell>
                  <TableCell className="max-w-[12rem]">
                    {isBye ? (
                      <span className="block truncate italic text-muted-foreground">轮空 (bye)</span>
                    ) : (
                      <Link
                        to={`/bot/${p.bot_b_id}`}
                        title={p.bot_b_display || p.bot_b_name || `#${p.bot_b_id}`}
                        className={`block truncate hover:text-primary ${
                          bWin ? 'font-semibold text-success' : w === 0 ? 'text-muted-foreground' : 'text-foreground'
                        }`}
                      >
                        {p.bot_b_display || p.bot_b_name || `#${p.bot_b_id}`}
                      </Link>
                    )}
                  </TableCell>
                  <TableCell className="whitespace-nowrap text-xs text-muted-foreground">
                    {p.scheduled_at ? fmtTime(p.scheduled_at) : '—'}
                  </TableCell>
                  <TableCell>
                    <StatusBadge status={p.status || 'pending'} />
                  </TableCell>
                  <TableCell className="text-right">
                    {p.match_id ? (
                      <Button asChild variant="ghost" size="sm" className="h-7 gap-1 px-2 text-xs text-primary">
                        <Link to={`/match/${p.match_id}`}>查看</Link>
                      </Button>
                    ) : (
                      <span className="text-xs text-muted-foreground">—</span>
                    )}
                  </TableCell>
                </TableRow>
              )
            }),
          )}
        </TableBody>
      </Table>
    </div>
  )
}
