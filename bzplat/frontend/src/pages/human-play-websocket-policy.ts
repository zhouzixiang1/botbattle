export interface HumanWebSocketCloseEvidence {
  code: number
  reason: string
  lastRejectReason: string
  lastProtocolError: string
}

export type HumanWebSocketClosePolicy =
  | { retry: true }
  | { retry: false; message: string }

const TERMINAL_REASON_MESSAGES: Readonly<Record<string, string>> = Object.freeze({
  rate_limit_exceeded: '操作过于频繁，连接已关闭。请稍后再试。',
  session_revoked: '会话已失效，连接已关闭。请重新登录。',
  forbidden: '无权访问该对局，连接已关闭。',
  message_too_large: '动作消息过大，连接已关闭。请刷新页面后重试。',
  invalid_game_id: '对局游戏协议不存在，连接已关闭。',
  connection_limit: '人类对战连接数已达上限，连接已关闭。请稍后刷新页面重试。',
})

/**
 * Resolve one human-play close from both protocol and transport evidence.
 *
 * A terminal reject can race the browser's CloseEvent, so either source must
 * fail closed. Only the two transient transport codes get the bounded retry
 * path; policy, payload and capacity closes require explicit user action.
 */
export function resolveHumanWebSocketClosePolicy(
  evidence: HumanWebSocketCloseEvidence,
): HumanWebSocketClosePolicy {
  for (const reason of [evidence.reason, evidence.lastRejectReason]) {
    const message = TERMINAL_REASON_MESSAGES[String(reason || '').trim()]
    if (message) return { retry: false, message }
  }

  const prior = String(evidence.lastProtocolError || '').trim()
  if (evidence.code === 1008) {
    return {
      retry: false,
      message: prior || '连接因安全策略关闭，已停止自动重连。',
    }
  }
  if (evidence.code === 1009) {
    return {
      retry: false,
      message: '动作消息过大，连接已关闭。请刷新页面后重试。',
    }
  }
  if (evidence.code === 1013) {
    return {
      retry: false,
      message: '人类对战连接数已达上限，连接已关闭。请稍后刷新页面重试。',
    }
  }
  if (evidence.code === 1001 || evidence.code === 1006) {
    return { retry: true }
  }
  return {
    retry: false,
    message: prior || '人类对战连接已关闭。请刷新页面重试。',
  }
}
