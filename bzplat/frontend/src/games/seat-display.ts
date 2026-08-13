import type { SeatInfo } from '@/games/canvas-types'

export interface SeatDisplay {
  /** 画面中的主语：Bot 名，或真人公开姓名。 */
  subject: string
  /** Bot 所有者；真人姓名已经是主语，不在次级信息中重复。 */
  owner: string | null
  kind: 'Bot' | '真人'
  seat: string
}

function clean(value: string | undefined): string {
  return (value || '').trim()
}

/**
 * Canvas、HUD 与事件描述共用的参与者语言契约。
 *
 * 名字永远是主语，内部 0/1 座位只转换成次级的 1-based 位置文案；缺少公开
 * 身份时才回退到座位号。这样同一局在观赛、人机、访客和有权限角色下保持
 * 同一套骨架，权限只决定是否额外出现私有 debug。
 */
export function seatDisplay(info: SeatInfo | undefined, index: number): SeatDisplay {
  const botName = clean(info?.botName)
  const ownerName = clean(info?.ownerName)
  const ownerDisplayName = clean(info?.ownerDisplayName)
  const publicOwner = ownerDisplayName || ownerName
  const isHuman = Boolean(info?.isHuman)
  const owner = !isHuman && botName && publicOwner
    ? ownerName && ownerDisplayName && ownerDisplayName !== ownerName
      ? `${ownerDisplayName} · @${ownerName}`
      : ownerName
        ? `@${ownerName}`
        : ownerDisplayName
    : null

  return {
    subject: isHuman
      ? publicOwner || `座位 ${index + 1} 的真人`
      : botName || publicOwner || `座位 ${index + 1}`,
    owner,
    kind: isHuman ? '真人' : 'Bot',
    seat: `座位 ${index + 1}`,
  }
}

/** 事件行把 Bot 名与 owner 作为完整主语；无效 seat 不猜身份。 */
export function eventSeatSubject(
  seats: SeatInfo[] | undefined,
  value: unknown,
  fallback = 'Bot',
): string {
  const index = Number(value)
  if (index !== 0 && index !== 1) return fallback
  const identity = seatDisplay(seats?.[index], index)
  return identity.owner
    ? `${identity.subject}（${identity.owner}）`
    : identity.subject
}
