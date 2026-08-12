import { Send } from 'lucide-react'

import type { ThreadDetail } from '@/components/communications/types'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { EmptyState } from '@/components/ui/status'
import { Switch } from '@/components/ui/switch'
import { Textarea } from '@/components/ui/textarea'
import { fmtTime } from '@/lib/format'
import { cn } from '@/lib/utils'

interface Props {
  thread: ThreadDetail | null
  reply: string
  onReplyChange: (value: string) => void
  onSend: () => void
  sending: boolean
  allowReply?: boolean
  email?: boolean
  onEmailChange?: (value: boolean) => void
  emptyText?: string
  viewerKind?: 'user' | 'admin'
}

export function ThreadView({
  thread,
  reply,
  onReplyChange,
  onSend,
  sending,
  allowReply = true,
  email = false,
  onEmailChange,
  emptyText = '从中间列表选择一封消息',
  viewerKind = 'user',
}: Props) {
  if (!thread) return <EmptyState text={emptyText} className="py-16" />
  const isOpen = thread.conversation.status === 'open'
  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col">
      <header className="border-b px-4 py-3">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <h3 className="min-w-0 flex-1 break-words text-sm font-semibold">
            {thread.conversation.subject || '无主题消息'}
          </h3>
          <Badge variant={isOpen ? 'secondary' : 'outline'}>{isOpen ? '进行中' : '已关闭'}</Badge>
        </div>
        <div className="mt-1 flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
          {thread.conversation.participants?.some((item) => item.username) && (
            <span className="min-w-0 truncate">
              参与者：{thread.conversation.participants
                .filter((item) => item.username)
                .map((item) => item.display_name || item.username)
                .join('、')}
            </span>
          )}
          <span className="ml-auto shrink-0 font-mono tabular-nums">更新于 {fmtTime(thread.conversation.updated_at)}</span>
        </div>
      </header>

      <div className="min-h-[18rem] flex-1 space-y-3 overflow-y-auto overscroll-contain p-3" data-scroll-region="message-thread" data-overflow-allowed="y">
        {thread.messages.map((message) => {
          const mine = viewerKind === 'admin'
            ? message.author.kind === 'admin' || message.author.kind === 'platform'
            : message.author.kind === 'user'
          const author = message.author.display_name || message.author.username || (message.author.kind === 'user' ? '用户' : '平台')
          return (
            <article
              key={message.public_id}
              className={cn(
                'max-w-[min(46rem,92%)] rounded-lg border px-3 py-2 text-sm shadow-xs',
                mine ? 'ml-auto border-primary/20 bg-primary/5' : 'bg-muted/35',
              )}
            >
              <div className="mb-1 flex min-w-0 items-center gap-2 text-xs text-muted-foreground">
                <span className="truncate font-medium text-foreground">{author}</span>
                <time className="ml-auto shrink-0 font-mono tabular-nums">{fmtTime(message.created_at)}</time>
              </div>
              <p className="whitespace-pre-wrap break-words leading-relaxed">{message.body_text}</p>
            </article>
          )
        })}
      </div>

      {allowReply && (
        <footer className="border-t p-3">
          <label className="sr-only" htmlFor="communication-reply">回复内容</label>
          <Textarea
            id="communication-reply"
            value={reply}
            onChange={(event) => onReplyChange(event.target.value)}
            rows={3}
            maxLength={10_000}
            placeholder={isOpen ? '输入回复；不要粘贴密码、令牌或其他敏感信息' : '此会话已关闭'}
            disabled={!isOpen || sending}
            className="max-h-36 min-h-20 resize-y"
          />
          <div className="mt-2 flex min-w-0 flex-wrap items-center gap-3">
            {onEmailChange && (
              <label className="flex min-h-10 cursor-pointer items-center gap-2 text-xs text-muted-foreground">
                <Switch checked={email} onCheckedChange={onEmailChange} disabled={!isOpen || sending} />
                同时发送邮件
              </label>
            )}
            <span className="text-xs tabular-nums text-muted-foreground">{reply.length}/10000</span>
            <Button
              type="button"
              size="sm"
              className="ml-auto"
              disabled={!isOpen || sending || !reply.trim()}
              aria-busy={sending}
              onClick={onSend}
            >
              <Send className="size-4" />{sending ? '发送中…' : '发送回复'}
            </Button>
          </div>
        </footer>
      )}
    </div>
  )
}
