/**
 * labels —— 全站「内部枚举 → 用户语言」唯一映射层。
 *
 * 页面禁止直接渲染后端枚举/ID；需要展示时一律 import 本模块。
 * 新增映射先来这里，再让页面消费，避免同义映射散落多页漂移。
 */

/** Bot 对战流程（runtime_mode） */
const RUNTIME_MODE_LABELS: Record<string, string> = {
  traditional: '标准对局',
  longrunning: '长驻对局',
}

export function runtimeModeLabel(value: string | null | undefined): string {
  return RUNTIME_MODE_LABELS[value || ''] || '标准对局'
}

/** 账号角色 */
const ROLE_LABELS: Record<string, string> = {
  user: '用户',
  organizer: '组织者',
  admin: '管理员',
}

export function roleLabel(value: string | null | undefined): string {
  return ROLE_LABELS[value || ''] || '用户'
}

/**
 * 对局是否计入平台排行榜的原因（rating_reason）。
 * 各页面文案统一走这里，不再各自维护副本。
 */
const RATING_REASON_LABELS: Record<string, string> = {
  eligible: '计入平台排行榜',
  same_owner: '同所有者调试 · 不计平台排行榜',
  self_play: '自博弈调试 · 不计平台排行榜',
  human: '人机对局 · 不计平台排行榜',
  contest: '赛事积分 · 不计平台排行榜',
  bot_missing: '历史 Bot 缺失 · 不计平台排行榜',
  owner_missing: '历史所有者缺失 · 不计平台排行榜',
  remote_local: '本地 Bot 练习 · 不计平台排行榜',
  ranked_bot_not_selected: '未派遣排行榜 Bot · 不计平台排行榜',
  alternate_time_control: '替代时限练习 · 不计平台排行榜',
}

export function ratingReasonLabel(value: string | null | undefined): string {
  return RATING_REASON_LABELS[value || ''] || '不计平台排行榜'
}
