/**
 * 公开对局参与者适配层。
 *
 * 后端的新契约使用嵌套 bot_a / bot_b；扁平字段只保留为旧快照和渐进部署
 * 的兼容输入。展示层不得把数据库 id 当成名称兜底。
 */

export interface PublicMatchParticipant {
  id?: number | null
  name?: string
  display_name?: string
  owner_name?: string
  owner_display?: string
  is_human?: boolean
}

export interface MatchParticipantSource {
  match_type?: string
  human_seat?: number | null
  bot_a_environment?: string | null
  bot_b_environment?: string | null
  bot_a_id?: number | null
  bot_b_id?: number | null
  bot_a?: PublicMatchParticipant
  bot_b?: PublicMatchParticipant
  bot_a_name?: string
  bot_a_display?: string
  bot_b_name?: string
  bot_b_display?: string
  bot_a_owner_name?: string
  bot_a_owner_display?: string
  bot_b_owner_name?: string
  bot_b_owner_display?: string
  human_user_name?: string
  human_user_display?: string
  /** 赛事 pairing 使用的公开 owner 字段。 */
  owner_a_name?: string
  owner_a_display?: string
  owner_b_name?: string
  owner_b_display?: string
}

export function matchParticipantEnvironment(
  source: MatchParticipantSource,
  side: 0 | 1,
): string | null {
  return side === 0
    ? source.bot_a_environment ?? null
    : source.bot_b_environment ?? null
}

export interface ResolvedMatchParticipant {
  side: 0 | 1
  seatLabel: string
  isHuman: boolean
  botId: number | null
  botLabel: string
  ownerName: string
  ownerLabel: string
}

function text(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

export function resolveMatchParticipant(
  source: MatchParticipantSource,
  side: 0 | 1,
): ResolvedMatchParticipant {
  const nested = side === 0 ? source.bot_a : source.bot_b
  const isHuman = nested?.is_human ?? (
    source.match_type === 'human' && source.human_seat === side
  )
  const flatId = side === 0 ? source.bot_a_id : source.bot_b_id
  const flatName = side === 0 ? source.bot_a_name : source.bot_b_name
  const flatDisplay = side === 0 ? source.bot_a_display : source.bot_b_display
  const flatOwnerName = side === 0
    ? source.bot_a_owner_name || source.owner_a_name
    : source.bot_b_owner_name || source.owner_b_name
  const flatOwnerDisplay = side === 0
    ? source.bot_a_owner_display || source.owner_a_display
    : source.bot_b_owner_display || source.owner_b_display

  // 扁平旧契约里的 bot_*_owner_* 永远属于 Bot 主人，不能在真人座复用。
  // 旧响应若没有单独的人类公开姓名就 fail closed；新响应走 nested seat。
  const ownerName = isHuman
    ? text(nested?.owner_name) || text(source.human_user_name)
    : text(nested?.owner_name) || text(flatOwnerName)
  const ownerLabel = isHuman
    ? text(nested?.owner_display)
      || text(source.human_user_display)
      || text(nested?.display_name)
      || text(nested?.name)
      || ownerName
    : text(nested?.owner_display) || text(flatOwnerDisplay) || ownerName
  const nestedBotId = nested?.id
  const botId = isHuman ? null : (nestedBotId ?? flatId ?? null)
  const resolvedBotLabel = text(nested?.display_name)
    || text(nested?.name)
    || text(flatDisplay)
    || text(flatName)

  return {
    side,
    seatLabel: `座位 ${side + 1}`,
    isHuman,
    botId,
    botLabel: isHuman
      ? '真人'
      : resolvedBotLabel || (botId == null ? 'Bot 已删除' : 'Bot 名称不可用'),
    ownerName,
    ownerLabel: ownerLabel || (isHuman ? '真人用户不可用' : '所属用户不可用'),
  }
}

/** 公开用户名一律显式带 @；展示名存在时同时保留两者。 */
export function participantOwnerText(participant: ResolvedMatchParticipant): string {
  if (!participant.ownerName) return participant.ownerLabel
  if (!participant.ownerLabel || participant.ownerLabel === participant.ownerName) {
    return `@${participant.ownerName}`
  }
  return `${participant.ownerLabel} · @${participant.ownerName}`
}

/** 详情页/画布的单行标题。任何缺失信息都回退到公开座位号，不暴露数据库 id。 */
export function participantHeaderLabel(participant: ResolvedMatchParticipant): string {
  const owner = participantOwnerText(participant)
  if (participant.isHuman) {
    return participant.ownerName || participant.ownerLabel !== '真人用户不可用'
      ? `${owner}（真人）`
      : `${participant.seatLabel}（真人信息不可用）`
  }
  if (participant.botLabel === 'Bot 名称不可用' || participant.botLabel === 'Bot 已删除') {
    if (participant.ownerName) return `${participant.seatLabel}（Bot 信息不可用 · ${owner}）`
    return `${participant.seatLabel}（信息不可用）`
  }
  return participant.ownerName || participant.ownerLabel !== '所属用户不可用'
    ? `${participant.botLabel}（${owner}）`
    : `${participant.botLabel}（所属用户不可用）`
}

/** 同一 Bot 占两个非真人座位；物理胜负仍属于座位，不应归成 Bot 自身胜/负。 */
export function isBotSelfPlay(source: MatchParticipantSource): boolean {
  if (source.match_type === 'human') return false
  const seatA = resolveMatchParticipant(source, 0)
  const seatB = resolveMatchParticipant(source, 1)
  return !seatA.isHuman
    && !seatB.isHuman
    && seatA.botId != null
    && seatA.botId === seatB.botId
}

export function matchParticipantSearchText(source: MatchParticipantSource): string {
  return ([0, 1] as const)
    .map((side) => {
      const participant = resolveMatchParticipant(source, side)
      return `${participant.botLabel} ${participant.ownerLabel} ${participant.ownerName}`
    })
    .join(' ')
}
