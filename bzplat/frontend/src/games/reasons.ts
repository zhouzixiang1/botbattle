import type {
  RawEvent,
  TerminalReasonPresentation,
  TerminalReasonResolver,
} from '@/games/base'

type ReasonMap = Readonly<Record<string, TerminalReasonPresentation>>

const neutral = (label: string): TerminalReasonPresentation => ({ label, tone: 'neutral' })
const danger = (label: string): TerminalReasonPresentation => ({ label, tone: 'danger' })

/**
 * 平台层终局原因的唯一中文投影。游戏包只声明自己的裁判原因，不能在页面再造一份映射。
 */
export const PLATFORM_TERMINAL_REASONS: ReasonMap = {
  completed: neutral('正常结束'),
  protocol_error: danger('Bot 响应协议错误'),
  technical_loss: danger('Bot 技术判负'),
  timeout: danger('Bot 决策超时'),
  crash: danger('Bot 运行异常'),
  illegal: danger('非法动作'),
  error: danger('决策异常'),
  version_unavailable: danger('Bot 版本不可用'),
  bot_deleted: danger('Bot 已删除'),
  bot_crashed: danger('Bot 启动失败'),
  platform_error: danger('平台运行异常'),
  admin_aborted: danger('管理员中止'),
  contest_bot_unavailable: danger('赛事 Bot 不可用'),
  contest_both_bots_unavailable: danger('赛事双方 Bot 均不可用'),
  contest_ended_pending_orphan: danger('赛事结束时仍有孤立对局'),
  human_inactive: danger('真人玩家连续超时'),
  orphan_after_restart: danger('服务重启后中止'),
  orphan_pending_after_restart: danger('服务重启后取消排队'),
  orphan_pending_no_contest: danger('无归属赛事的排队对局'),
  invalid_game_id: danger('游戏类型无效'),
  invalid_match_config: danger('对局配置无效'),
}

/**
 * 将持久化/SSE reason 变成稳定展示语义。已完成但未知的历史 reason 只显示通用中文，
 * 不把内部英文码泄漏给用户；非完成态未知原因则按异常终局处理。
 */
export function resolveTerminalReason(
  reason: unknown,
  status?: string,
  gameReasons: ReasonMap = {},
): TerminalReasonPresentation {
  const code = String(reason ?? '').trim()
  const known = gameReasons[code] ?? PLATFORM_TERMINAL_REASONS[code]
  if (known) return known
  if (status === 'completed') return neutral('已完成')
  if (status === 'aborted') return danger('对局已中止')
  return danger('异常结束')
}

export function createTerminalReasonResolver(gameReasons: ReasonMap): TerminalReasonResolver {
  return (reason, status) => resolveTerminalReason(reason, status, gameReasons)
}

/**
 * 平台产生的通用事件由平台层统一中文化；游戏包只负责裁判事件。
 * 返回 null 表示应继续交给具体游戏的 describeEvent。
 */
export function describePlatformEvent(event: RawEvent): string | null {
  if (event.type === 'error') {
    return resolveTerminalReason(event.reason || 'platform_error', 'aborted').label
  }
  return null
}
