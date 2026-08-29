/** 对局座位身份：REST/SSE/WS snapshot → SeatInfo[]，供 MatchViewer / HumanPlay / canvas 共用。 */
import type { SeatInfo } from '@/games/canvas-types'
import {
  participantHeaderLabel,
  resolveMatchParticipant,
  type MatchParticipantSource,
} from '@/lib/match-participants'
import {
  describeMatchOutcome,
  hasPublicMatchOutcomeField,
  isPublicMatchOutcome,
  type PublicMatchOutcome,
} from '@/lib/match-outcome'

export interface MatchSeatRow extends MatchParticipantSource {
  id?: string
  match_type?: string
  human_seat?: number | null
  winner?: number | null
  reason?: string
  status?: string
  game_id?: string
  /** 创建事务冻结的天梯资格；中性局也会有 settlement marker。 */
  rated?: boolean
  rating_reason?: string
  /** exactly-once marker 真值；rated 只是创建时资格，不能替代本字段。 */
  rating_settled?: boolean
  /** 1 表示 Bot 故障被判负；平台故障的 aborted 对局不设置。 */
  technical_loss?: number
  /** Public, bounded result projection shared by match lists and details. */
  outcome?: PublicMatchOutcome | null
  /** 对局结果唯一公共契约。 */
  result?: {
    rounds_played?: number
    deltas?: number[]
    normalized_delta?: number
    /** Holdem duplicate 每个 leg 独立计分；合并 deltas 只用于破同分。 */
    legs?: Array<{ winner: number | null; deltas: number[] }>
    technical_incidents_by_seat?: Record<number, number>
    technical_incident_samples?: Array<{
      seat: number
      error: string
      code?: string
      reason?: string
      turn?: number | null
      leg?: number | null
    }>
  }
}

export function seatInfos(m: MatchSeatRow | null | undefined): SeatInfo[] | undefined {
  if (!m) return undefined
  return ([0, 1] as const).map((side) => {
    const participant = resolveMatchParticipant(m, side)
    const hasPublicBotLabel = participant.botLabel !== 'Bot 名称不可用'
      && participant.botLabel !== 'Bot 已删除'
    const missingOwnerLabel = participant.isHuman ? '真人用户不可用' : '所属用户不可用'
    return {
      botName: !participant.isHuman && hasPublicBotLabel ? participant.botLabel : undefined,
      ownerName: participant.ownerName || undefined,
      ownerDisplayName: participant.ownerLabel !== missingOwnerLabel
        ? participant.ownerLabel
        : undefined,
      isHuman: participant.isHuman,
    }
  })
}

/** 顶栏对阵文案：BOT 名（@用户）或人类 */
export function seatHeaderLabel(m: MatchParticipantSource, side: 0 | 1): string {
  return participantHeaderLabel(resolveMatchParticipant(m, side))
}

/** Dense result lines use the participant entity only; ownership is already shown beside them. */
export function outcomeSeatLabels(
  m: MatchParticipantSource,
): readonly [string, string] {
  const label = (side: 0 | 1) => {
    const participant = resolveMatchParticipant(m, side)
    if (participant.isHuman) {
      return participant.ownerLabel === '真人用户不可用'
        ? participant.seatLabel
        : participant.ownerLabel
    }
    return participant.botLabel === 'Bot 名称不可用' || participant.botLabel === 'Bot 已删除'
      ? participant.seatLabel
      : participant.botLabel
  }
  return [label(0), label(1)]
}

/** 胜者文案：只有 public outcome 能证明真平局，缺失终局不得从 null 猜测。 */
export function resolveWinnerLabel(
  m: MatchSeatRow | null,
  eventWinner: number | null | undefined,
  finished: boolean,
  colorLabel?: (seat: number) => string,
): string {
  const nameOf = (seat: number) => {
    if (colorLabel) return colorLabel(seat)
    if (m) return seatHeaderLabel(m, seat as 0 | 1)
    // 显示从 1 起计（后端 0 起计，DB CHECK 约束未变）。
    return `座位 ${seat + 1}`
  }
  if (m && isPublicMatchOutcome(m.outcome)) {
    return describeMatchOutcome(
      { status: m.status, outcome: m.outcome },
      { seatLabels: [nameOf(0), nameOf(1)] },
    ).primary
  }
  if (m && hasPublicMatchOutcomeField(m)) {
    return describeMatchOutcome(
      { status: m.status ?? (finished ? 'completed' : undefined), outcome: null },
      { seatLabels: [nameOf(0), nameOf(1)] },
    ).primary
  }
  if (m?.winner === 0 || m?.winner === 1) return `${nameOf(m.winner)}胜`
  if (eventWinner === 0 || eventWinner === 1) return `${nameOf(eventWinner)}胜`
  if (m && finished) {
    return describeMatchOutcome(
      { status: m.status, outcome: m.outcome },
      { seatLabels: [nameOf(0), nameOf(1)] },
    ).primary
  }
  if (!finished) return '进行中'
  return '赛果暂不可用'
}

export function fmtNet(n: number): string {
  return `${n >= 0 ? '+' : ''}${n.toLocaleString('en-US')}`
}
