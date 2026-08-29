import { Badge } from '@/components/ui/badge'
import { cn } from '@/lib/utils'

export interface EliminationTiebreakGroup {
  group: number
  state: 'awaiting_results' | 'tied' | 'decided' | 'invalid'
  completed_games: number
  planned_games: number
  points_a: number
  points_b: number
}

export interface EliminationTiebreakEncounter {
  round_num: number
  bracket_slot: number
  state: 'decided' | 'append_tiebreak' | 'awaiting_results' | 'legacy_draw' | 'legacy_draw_blocked' | 'invalid' | 'bye'
  entry_a_id?: number | null
  entry_b_id?: number | null
  entry_a_label?: string | null
  entry_b_label?: string | null
  winner_entry_id?: number | null
  next_tiebreak_group?: number | null
  current_tiebreak_group?: number
  completed_tiebreak_games?: number
  tiebreak_games_in_group?: number
  groups?: EliminationTiebreakGroup[]
}

export interface EliminationTiebreakProjection {
  mode: 'paired_swap_until_decided' | 'legacy_draw_blocked'
  unbounded: boolean
  state: 'active' | 'decided' | 'invalid' | 'legacy_draw_blocked'
  encounters: EliminationTiebreakEncounter[]
}

function participantLabel(
  encounter: EliminationTiebreakEncounter,
  side: 'a' | 'b',
): string {
  return (side === 'a' ? encounter.entry_a_label : encounter.entry_b_label)?.trim()
    || (side === 'a' ? '选手 A' : '选手 B')
}

function encounterStatus(encounter: EliminationTiebreakEncounter): string {
  if (encounter.state === 'invalid') return '决胜状态暂不可用'
  if (encounter.state === 'append_tiebreak') {
    return `本组同分，将继续决胜组 ${encounter.next_tiebreak_group ?? (encounter.current_tiebreak_group ?? 0) + 1}`
  }
  if (encounter.state === 'awaiting_results') return '等待本组剩余结果'
  if (encounter.state === 'decided') {
    const winner = encounter.winner_entry_id === encounter.entry_a_id
      ? participantLabel(encounter, 'a')
      : encounter.winner_entry_id === encounter.entry_b_id
        ? participantLabel(encounter, 'b')
        : '晋级者待确认'
    return `已决出晋级者：${winner}`
  }
  return '等待决胜'
}

export function EliminationTiebreakStatus({
  value,
  className,
}: {
  value?: EliminationTiebreakProjection | null
  className?: string
}) {
  if (!value) return null
  if (value.mode === 'legacy_draw_blocked') {
    return (
      <section
        role="alert"
        aria-label="淘汰赛阻断状态"
        className={cn('min-w-0 rounded-xl border border-warning/40 bg-warning/10 px-4 py-3', className)}
      >
        <h3 className="text-sm font-semibold text-warning-foreground">赛事已阻断</h3>
        <p className="mt-1 break-words text-xs leading-relaxed text-warning-foreground">
          历史赛制无决胜策略，赛事已阻断
        </p>
      </section>
    )
  }
  const relevant = value.encounters.filter((encounter) => (
    (encounter.groups?.length ?? 0) > 0
    || encounter.state === 'append_tiebreak'
    || encounter.state === 'awaiting_results'
    || encounter.state === 'invalid'
  ))
  if (relevant.length === 0 && value.state !== 'invalid') return null

  return (
    <section aria-label="淘汰赛决胜状态" className={cn('bg-muted/25 px-4 py-3', className)}>
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-sm font-semibold text-foreground">淘汰赛决胜状态</h3>
        <Badge variant="outline" className="text-[10px]">两场换边一组 · 不封顶</Badge>
      </div>
      {relevant.length === 0 ? (
        <p className="mt-2 text-xs text-warning-foreground">决胜状态暂不可用</p>
      ) : (
        <div className="mt-2 divide-y divide-border/70">
          {relevant.map((encounter) => (
            <div key={`${encounter.round_num}:${encounter.bracket_slot}`} className="py-2 first:pt-0 last:pb-0">
              <p className="text-xs font-medium text-foreground">
                第 {encounter.round_num} 轮 · 对阵 {encounter.bracket_slot + 1} · {participantLabel(encounter, 'a')} vs {participantLabel(encounter, 'b')}
              </p>
              {(encounter.groups?.length ?? 0) === 0 ? (
                <p className="mt-1 text-xs text-muted-foreground">主赛平局 · {encounterStatus(encounter)}</p>
              ) : (
                <div className="mt-1 space-y-1">
                  {encounter.groups?.map((group) => (
                    <p key={group.group} className="text-xs text-muted-foreground">
                      决胜组 {group.group} · {group.completed_games}/{group.planned_games} 场 · {participantLabel(encounter, 'a')} {group.points_a}–{group.points_b} {participantLabel(encounter, 'b')}
                    </p>
                  ))}
                  <p className="text-xs font-medium text-foreground">{encounterStatus(encounter)}</p>
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </section>
  )
}
