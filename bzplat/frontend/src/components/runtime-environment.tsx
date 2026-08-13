import { Badge } from '@/components/ui/badge'
import { cn } from '@/lib/utils'

export type ExecutionEnvironment = 'platform_low' | 'platform_high' | 'remote_local' | 'human'

export interface LocalAIAgent {
  public_id: string
  bot_id: number
  label: string
  game_id: string
  status: string
  is_online: boolean
  is_busy: boolean
  is_available: boolean
  unavailable_reason?: 'revoked' | 'bot_disabled' | 'offline' | 'busy' | ''
  bot_active?: boolean
  last_seen_at?: string | null
  bot_name: string
  bot_display_name?: string | null
}

const ENVIRONMENT_LABEL: Record<ExecutionEnvironment, string> = {
  platform_low: '节能沙箱',
  platform_high: '赛事沙箱',
  remote_local: '本地 Bot',
  human: '真人',
}

export function executionEnvironmentLabel(value?: string | null): string | null {
  if (!value || !(value in ENVIRONMENT_LABEL)) return null
  return ENVIRONMENT_LABEL[value as ExecutionEnvironment]
}

export function RuntimeEnvironmentBadge({
  environment,
  className,
}: {
  environment?: string | null
  className?: string
}) {
  const label = executionEnvironmentLabel(environment)
  if (!label || environment === 'human') return null
  return (
    <Badge
      variant="outline"
      className={cn(
        'shrink-0 text-[10px]',
        environment === 'remote_local' && 'border-primary/30 bg-primary/5 text-primary',
        className,
      )}
    >
      {label}
    </Badge>
  )
}

export function localAgentStatus(agent: LocalAIAgent): { label: string; available: boolean } {
  if (agent.unavailable_reason === 'revoked' || agent.status === 'revoked') return { label: '已撤销', available: false }
  if (agent.unavailable_reason === 'bot_disabled' || agent.bot_active === false) return { label: 'Bot 已停用', available: false }
  if (agent.unavailable_reason === 'offline' || !agent.is_online) return { label: '未连接', available: false }
  if (agent.unavailable_reason === 'busy' || agent.is_busy) return { label: '对局中', available: false }
  return { label: agent.is_available ? '可用' : '暂不可用', available: agent.is_available }
}

export function localAgentBotName(agent: LocalAIAgent): string {
  return agent.bot_display_name?.trim() || agent.bot_name
}
