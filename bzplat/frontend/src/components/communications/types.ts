export interface ThreadSummary {
  public_id: string
  kind: string
  subject: string
  status: string
  latest_body: string
  latest_at: string
  unread_count: number
  created_at: string
  updated_at: string
}

export interface MessageItem {
  public_id: string
  reply_to?: string | null
  author: {
    kind: 'user' | 'admin' | 'platform'
    username?: string | null
    display_name?: string | null
  }
  body_text: string
  created_at: string
}

export interface ThreadDetail {
  conversation: {
    public_id: string
    kind: string
    subject: string
    status: string
    created_at: string
    updated_at: string
    participants?: Array<{
      public_id: string
      kind: string
      username?: string | null
      display_name?: string | null
    }>
    bug_report?: {
      public_id: string
      status: string
      category: string
      impact: string
      current_route: string
    }
  }
  messages: MessageItem[]
}

export interface BugSummary {
  public_id: string
  conversation_public_id: string
  category: string
  impact: string
  title: string
  current_route: string
  status: string
  created_at: string
  updated_at: string
  username?: string | null
}

export interface BugDetail extends BugSummary {
  reporter_username?: string | null
  events: Array<{
    public_id: string
    event_type: string
    from_status: string
    to_status: string
    note: string
    created_at: string
    actor_username?: string | null
  }>
  attachments: Array<{
    public_id: string
    original_name: string
    media_type: string
    size_bytes: number
    sha256: string
    created_at: string
  }>
  diagnostic?: {
    public_id: string
    schema_version: number
    bundle: Record<string, unknown>
    created_at: string
  } | null
}

export interface BroadcastSummary {
  public_id: string
  state: string
  audience_kind: string
  audience_count: number
  subject: string
  channels: string[]
  audience_snapshot_hash: string
  preview_expires_at?: string | null
  scheduled_at?: string | null
  approved_at?: string | null
  started_at?: string | null
  completed_at?: string | null
  cancelled_at?: string | null
  created_at: string
  updated_at: string
  delivered_count?: number
  failed_recipient_count?: number
  failed_delivery_count?: number
}

export interface FailedBroadcastRecipient {
  public_id: string
  state: string
  attempt_count: number
  max_attempts: number
  last_error: string
  processed_at?: string | null
  username?: string | null
}

export interface FailedDelivery {
  public_id: string
  channel: string
  status: string
  attempt_count: number
  max_attempts: number
  last_error: string
  provider?: string | null
  provider_message_id?: string | null
  template_key?: string | null
  conversation_public_id?: string | null
  broadcast_public_id?: string | null
  username?: string | null
  created_at: string
  updated_at: string
}

export interface BroadcastDetail extends BroadcastSummary {
  audience_filter: Record<string, unknown>
  body_text: string
  created_by_username?: string | null
  recipients: Record<string, number>
  deliveries: Record<string, number>
  failed_recipients: FailedBroadcastRecipient[]
  failed_deliveries: FailedDelivery[]
}

export const THREAD_KIND_LABELS: Record<string, string> = {
  notification: '通知',
  support: '支持',
  bug_report: '反馈',
  broadcast: '群发',
  system: '系统',
}

export const BUG_STATUS_LABELS: Record<string, string> = {
  new: '新提交',
  acknowledged: '已确认',
  needs_info: '待补充',
  in_progress: '处理中',
  resolved: '已解决',
  duplicate: '重复问题',
  wont_fix: '不处理',
}

export const BUG_CATEGORY_LABELS: Record<string, string> = {
  match: '对局',
  bot: 'Bot',
  contest: '锦标赛',
  account: '账号',
  page: '页面',
  other: '其他',
}

export const BUG_IMPACT_LABELS: Record<string, string> = {
  blocked: '无法继续使用',
  major: '主要功能异常',
  minor: '局部功能异常',
  cosmetic: '显示问题',
}

export const BROADCAST_STATE_LABELS: Record<string, string> = {
  draft: '待确认',
  scheduled: '待发送',
  running: '发送中',
  completed: '已完成',
  cancelled: '已取消',
}

export const AUDIENCE_KIND_LABELS: Record<string, string> = {
  active_users: '全部启用用户',
  role: '按角色',
  game_bot_owners: '按游戏 Bot 所有者',
  contest_entrants: '锦标赛参赛者',
  selected_users: '指定用户',
}
