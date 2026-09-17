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

/** 匿名兜底文案族：棋类按行棋次序称呼，通用场景按玩家次序称呼。 */
export type SeatFallbackKind = 'board' | 'generic'

const ANONYMOUS_FALLBACKS: Record<SeatFallbackKind, readonly [string, string]> = {
  board: ['先手', '后手'],
  generic: ['玩家一', '玩家二'],
}

/** 座位是否持有可展示的公开身份（Bot 名或用户名）；仅匿名兜底才出现座位号。 */
export function seatHasIdentity(info: SeatInfo | undefined): boolean {
  return Boolean(clean(info?.botName) || clean(info?.ownerName) || clean(info?.ownerDisplayName))
}

/**
 * Canvas、HUD 与事件描述共用的参与者语言契约。
 *
 * 名字永远是主语；缺少公开身份时才回退到先手/后手或玩家一/玩家二，内部
 * 0/1 座位不再直接变成文案。这样同一局在观赛、人机、访客和有权限角色下
 * 保持同一套骨架，权限只决定是否额外出现私有 debug。
 */
export function seatDisplay(
  info: SeatInfo | undefined,
  index: number,
  fallbackKind: SeatFallbackKind = 'generic',
): SeatDisplay {
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
  const anonymous = ANONYMOUS_FALLBACKS[fallbackKind][index] ?? `座位 ${index + 1}`

  return {
    subject: isHuman
      ? publicOwner || anonymous
      : botName || publicOwner || anonymous,
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
  fallbackKind: SeatFallbackKind = 'generic',
): string {
  const index = Number(value)
  if (index !== 0 && index !== 1) return fallback
  const identity = seatDisplay(seats?.[index], index, fallbackKind)
  return identity.owner
    ? `${identity.subject}（${identity.owner}）`
    : identity.subject
}
