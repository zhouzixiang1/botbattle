/** 对局座位身份：REST/SSE/WS snapshot → SeatInfo[]，供 MatchViewer / HumanPlay / canvas 共用。 */
import type { SeatInfo } from '@/games/canvas-types'

export interface MatchSeatRow {
  match_type?: string
  human_seat?: number | null
  winner?: number | null
  reason?: string
  status?: string
  game_id?: string
  bot_a_id?: number
  bot_b_id?: number
  /** 对局级配置 JSON（如 {hands:70}/{n_dots:6}），游戏无关。 */
  match_config?: Record<string, number>
  /** 对局结果详情 JSON（{hands_played, deltas:[ea,eb], net_bb}）。 */
  result?: { hands_played?: number; deltas?: number[]; net_bb?: number }
  bot_a?: {
    id?: number | null
    name?: string
    display_name?: string
    owner_name?: string
    owner_display?: string
    is_human?: boolean
  }
  bot_b?: {
    id?: number | null
    name?: string
    display_name?: string
    owner_name?: string
    owner_display?: string
    is_human?: boolean
  }
  bot_a_name?: string
  bot_a_display?: string
  bot_b_name?: string
  bot_b_display?: string
  bot_a_owner_name?: string
  bot_a_owner_display?: string
  bot_b_owner_name?: string
  bot_b_owner_display?: string
}

export function botLabel(
  nested?: { name?: string; display_name?: string },
  flatDisplay?: string,
  flatName?: string,
): string {
  return (nested?.display_name || nested?.name || flatDisplay || flatName || '').trim()
}

export function seatInfos(m: MatchSeatRow | null | undefined): SeatInfo[] | undefined {
  if (!m) return undefined
  const a = m.bot_a
  const b = m.bot_b
  return [
    {
      botName: botLabel(a, m.bot_a_display, m.bot_a_name) || undefined,
      ownerName: a?.owner_name ?? m.bot_a_owner_name,
      isHuman: a?.is_human ?? (m.match_type === 'human' && m.human_seat === 0),
    },
    {
      botName: botLabel(b, m.bot_b_display, m.bot_b_name) || undefined,
      ownerName: b?.owner_name ?? m.bot_b_owner_name,
      isHuman: b?.is_human ?? (m.match_type === 'human' && m.human_seat === 1),
    },
  ]
}

/** 顶栏对阵文案：BOT 名（@用户）或人类 */
export function seatHeaderLabel(m: MatchSeatRow, side: 0 | 1): string {
  const nested = side === 0 ? m.bot_a : m.bot_b
  const bot = botLabel(
    nested,
    side === 0 ? m.bot_a_display : m.bot_b_display,
    side === 0 ? m.bot_a_name : m.bot_b_name,
  )
  const owner =
    nested?.owner_name ?? (side === 0 ? m.bot_a_owner_name : m.bot_b_owner_name)
  const isHuman =
    nested?.is_human ?? (m.match_type === 'human' && m.human_seat === side)
  if (isHuman) return owner ? `${owner}（人类）` : '人类'
  if (bot && owner) return `${bot} @${owner}`
  if (bot) return bot
  if (owner) return `@${owner}`
  const id = side === 0 ? m.bot_a_id : m.bot_b_id
  // 显示从 1 起计（后端 0 起计，DB CHECK 约束未变）。
  return id != null ? `Bot #${id}` : `座位 ${side + 1}`
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
