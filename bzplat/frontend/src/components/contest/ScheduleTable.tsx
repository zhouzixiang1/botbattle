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
import { MatchParticipantIdentity } from '@/components/MatchParticipants'
import { effectivePairingStatus, PairingResult } from '@/components/contest/pairing-result'
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
import { outcomeParticipantStates, type PublicMatchOutcome } from '@/lib/match-outcome'
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
  display_status?: string | null
  match_winner?: number | null
  outcome?: PublicMatchOutcome | null
  scheduled_at?: string | null
  group_id?: string | null
  series_index?: number | null
  series_size?: number | null
}

function pairingSeriesKey(pairing: SchedulePairing, stageType?: string): string {
  if (pairing.is_bye || !pairing.series_size || pairing.series_size <= 1) return `match:${pairing.id}`
  const players = [
    pairing.bot_a_id ?? pairing.owner_a_name ?? pairing.bot_a_name ?? `unknown-a-${pairing.id}`,
    pairing.bot_b_id ?? pairing.owner_b_name ?? pairing.bot_b_name ?? `unknown-b-${pairing.id}`,
  ].sort().join(':')
  const round = stageType === 'swiss' ? pairing.round_num ?? 1 : 0
  return `${round}:${pairing.group_id || ''}:${players}`
}

interface Props {
  pairings: SchedulePairing[]
  stageType?: string
  duplicate?: boolean
  legacyAggregate?: boolean
}

const PER_PAGE = 30

export default function ScheduleTable({
  pairings,
  stageType,
  duplicate = false,
  legacyAggregate = false,
}: Props) {
  const [page, setPage] = useState(1)

  // 扁平化为有序行：先按 round_num 分组排序，组内按 bracket_slot（淘汰）或 id（其他）排序。
  // 预计算每行是否为「本轮首行」isRoundStart，使分页切片后轮次徽章显示仍正确。
  const rows = useMemo(() => {
    const hasSeries = pairings.some((pairing) => (pairing.series_size ?? 1) > 1)
    if (hasSeries) {
      const groups = new Map<string, SchedulePairing[]>()
      for (const pairing of pairings) {
        const key = pairingSeriesKey(pairing, stageType)
        const group = groups.get(key) || []
        group.push(pairing)
        groups.set(key, group)
      }
      const ordered = Array.from(groups.values()).sort((a, b) => {
        const round = (a[0]?.round_num ?? 1) - (b[0]?.round_num ?? 1)
        return round !== 0 ? round : (a[0]?.id ?? 0) - (b[0]?.id ?? 0)
      })
      return ordered.flatMap((group) => group
        .sort((a, b) => (a.series_index ?? 1) - (b.series_index ?? 1))
        .map((pairing, index) => ({
          pairing,
          round: pairing.round_num ?? 1,
          isRoundStart: index === 0,
          isSeriesStart: index === 0,
        })))
    }
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
    const out: Array<{ pairing: SchedulePairing; round: number; isRoundStart: boolean; isSeriesStart: boolean }> = []
    for (const r of sortedRounds) {
      r.pairings.forEach((p, idx) => out.push({ pairing: p, round: r.round, isRoundStart: idx === 0, isSeriesStart: true }))
    }
    return out
  }, [pairings, stageType])

  // 客户端分页（越界回退：对阵总数缩短到当前页之外时夹紧到末页）
  const totalPages = Math.max(1, Math.ceil(rows.length / PER_PAGE))
  const safePage = Math.min(page, totalPages)
  const pageRows = rows.slice((safePage - 1) * PER_PAGE, safePage * PER_PAGE)

  if (pairings.length === 0) {
    return <p className="py-6 text-center text-sm text-muted-foreground">暂无对阵</p>
  }

  return (
    <div className="min-w-0 space-y-2">
      <div className="divide-y overflow-hidden rounded-lg border md:hidden" aria-label="赛事对阵一览表移动视图">
        {pageRows.map(({ pairing: p, round, isSeriesStart }) => {
          const status = effectivePairingStatus(p)
          const isBye = p.is_bye === true
          const outcomeStates = outcomeParticipantStates(p.outcome)
          const states = isBye && status === 'completed'
            ? ['winner', 'neutral'] as const
            : outcomeStates
          return (
            <article
              key={p.id}
              data-testid="contest-schedule-mobile-card"
              data-series-start={isSeriesStart || undefined}
              className={isSeriesStart && (p.series_size ?? 1) > 1 ? 'space-y-2.5 border-t-2 border-primary/20 p-3 first:border-t-0' : 'space-y-2.5 p-3'}
            >
              <header className="flex min-w-0 flex-wrap items-center gap-2 text-xs">
                <span className="font-mono font-semibold text-foreground">
                  {p.group_id ? `${p.group_id} · ` : ''}R{round}
                  {p.series_size && p.series_size > 1
                    ? duplicate
                      ? ` · 本对 ${p.series_size} 组复式 · 第 ${p.series_index ?? 1}/${p.series_size} 组`
                      : legacyAggregate
                        ? ` · 本对 ${p.series_size} 场历史系列对局 · 旧版系列第 ${p.series_index ?? 1}/${p.series_size} 场`
                      : ` · 本对 ${p.series_size} 场计分 · 第 ${p.series_index ?? 1}/${p.series_size} 场`
                    : ''}
                </span>
                <span className="ml-auto text-muted-foreground">{p.scheduled_at ? fmtTime(p.scheduled_at) : '未定排期'}</span>
              </header>
              <div data-match-participants="true" className="grid min-w-0 gap-2">
                <MatchParticipantIdentity source={p} side={0} variant="panel" state={states[0]} textLines={2} />
                <MatchParticipantIdentity source={p} side={1} variant="panel" state={states[1]} emptyLabel={isBye ? '轮空 (bye)' : undefined} textLines={2} />
              </div>
              <div className="flex min-w-0 flex-wrap items-center gap-2">
                <StatusBadge status={status || 'pending'} />
                <PairingResult pairing={p} primaryOnly={legacyAggregate} />
              </div>
              {p.match_id ? (
                <Button asChild variant="outline" size="sm" className="min-h-11 w-full text-primary">
                  <Link to={`/match/${p.match_id}`}>{duplicate ? '查看复式回放' : legacyAggregate ? '查看历史对局' : '查看计分场'}</Link>
                </Button>
              ) : (
                <div className="flex min-h-11 items-center justify-center rounded-md bg-muted/40 text-xs text-muted-foreground">
                  {duplicate ? '尚未生成复式交锋' : legacyAggregate ? '尚未生成历史系列对局' : '尚未生成计分场'}
                </div>
              )}
            </article>
          )
        })}
      </div>
      <div className="hidden md:block">
        <DataTable scrollLabel="赛事对阵一览表">
          <Table className="min-w-[40rem]" aria-label="赛事对阵一览表">
        <TableHeader>
          <TableRow>
            <TableHead className="w-16">轮次</TableHead>
            <TableHead className="min-w-[8rem]">座位 1</TableHead>
            <TableHead className="min-w-[8rem]">座位 2</TableHead>
            <TableHead className="w-36">排期时间</TableHead>
            <TableHead className="w-32">状态 / 赛果</TableHead>
            <TableHead className="w-16 text-right">查看</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {pageRows.map(({ pairing: p, round, isRoundStart, isSeriesStart }) => {
            const status = effectivePairingStatus(p)
            const isBye = p.is_bye === true
            const outcomeStates = outcomeParticipantStates(p.outcome)
            // 复式不存在组级胜者；只有单场 outcome 或轮空才着色。
            const states = isBye && status === 'completed'
              ? ['winner', 'neutral'] as const
              : outcomeStates
            return (
              <TableRow key={p.id} data-series-start={isSeriesStart || undefined} className={isSeriesStart && (p.series_size ?? 1) > 1 ? 'border-t-2 border-primary/20' : undefined}>
                {/* 仅每轮首行显示轮次徽章，避免重复噪音 */}
                <TableCell className="font-mono text-xs text-muted-foreground">
                  {isRoundStart ? `R${round}` : ''}
                  {p.series_size && p.series_size > 1 && (
                    <span className="block whitespace-nowrap text-xs">
                      {isSeriesStart
                        ? duplicate
                          ? `本对 ${p.series_size} 组复式 · `
                          : legacyAggregate
                            ? `本对 ${p.series_size} 场历史系列对局 · `
                            : `本对 ${p.series_size} 场计分 · `
                        : ''}
                      {legacyAggregate && !duplicate ? '旧版系列' : '第'} {legacyAggregate && !duplicate ? `第 ${p.series_index ?? 1}/${p.series_size} 场` : `${p.series_index ?? 1}/${p.series_size}${duplicate ? ' 组' : ' 场'}`}
                    </span>
                  )}
                </TableCell>
                <TableCell className="max-w-[12rem]">
                  <MatchParticipantIdentity
                    source={p}
                    side={0}
                    state={states[0]}
                  />
                </TableCell>
                <TableCell className="max-w-[12rem]">
                  <MatchParticipantIdentity
                    source={p}
                    side={1}
                    state={states[1]}
                    emptyLabel={isBye ? '轮空 (bye)' : undefined}
                  />
                </TableCell>
                <TableCell className="whitespace-nowrap text-xs text-muted-foreground">
                  {p.scheduled_at ? fmtTime(p.scheduled_at) : '—'}
                </TableCell>
                <TableCell>
                  <div className="flex flex-col items-start gap-1">
                    <StatusBadge status={status || 'pending'} />
                    <PairingResult pairing={p} primaryOnly={legacyAggregate} />
                  </div>
                </TableCell>
                <TableCell className="text-right">
                  {p.match_id ? (
                    <Button asChild variant="ghost" size="xs" className="min-h-8 text-primary">
                      <Link to={`/match/${p.match_id}`} aria-label={duplicate ? '查看复式回放' : legacyAggregate ? '查看历史对局' : '查看计分场'}>查看</Link>
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
      </div>
      <Pagination page={safePage} perPage={PER_PAGE} total={rows.length} onPageChange={setPage} />
    </div>
  )
}
