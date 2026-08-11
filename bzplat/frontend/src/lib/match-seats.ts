/** 对局座位身份：REST/SSE/WS snapshot → SeatInfo[]，供 MatchViewer / HumanPlay / canvas 共用。 */
import type { SeatInfo } from '@/games/canvas-types'
import {
  participantHeaderLabel,
  resolveMatchParticipant,
  type MatchParticipantSource,
} from '@/lib/match-participants'

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
    return {
      botName: !participant.isHuman && hasPublicBotLabel ? participant.botLabel : undefined,
      ownerName: participant.ownerName || undefined,
      isHuman: participant.isHuman,
    }
  })
}

/** 顶栏对阵文案：BOT 名（@用户）或人类 */
export function seatHeaderLabel(m: MatchSeatRow, side: 0 | 1): string {
  return participantHeaderLabel(resolveMatchParticipant(m, side))
}

/** 胜者文案：优先名字，回退座位号 / 平局 / 进行中 */
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
  if (m?.winner === 0 || m?.winner === 1) return nameOf(m.winner)
  if (eventWinner === 0 || eventWinner === 1) return nameOf(eventWinner)
  // Duplicate 没有单一整场胜者：两个 leg 独立计分，合并
  // deltas 仅供赛事破同分。不得在此把它误判成 Bot A/B 获胜。
  if (finished && (m?.result?.legs?.length ?? 0) > 1) return '复式赛按分局计分'
  if (m && m.winner === null && finished) {
    const ea = m.result?.deltas?.[0]
    const eb = m.result?.deltas?.[1]
    if (typeof ea === 'number' && typeof eb === 'number') {
      if (ea > eb) return nameOf(0)
      if (eb > ea) return nameOf(1)
    }
    return '平局'
  }
  if (Array.isArray(m?.result?.deltas) && finished) {
    const ea = m!.result!.deltas![0]
    const eb = m!.result!.deltas![1]
    if (typeof ea === 'number' && typeof eb === 'number') {
      if (ea > eb) return nameOf(0)
      if (eb > ea) return nameOf(1)
      return '平局'
    }
  }
  if (eventWinner === null && finished) return '平局'
  if (!finished) return '进行中'
  return '—'
}

export function fmtNet(n: number): string {
  return `${n >= 0 ? '+' : ''}${n.toLocaleString('en-US')}`
}
