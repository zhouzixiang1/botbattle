import { type ReactNode } from 'react'
import { Loader2, Inbox, AlertCircle, RefreshCw } from 'lucide-react'
import { cn } from '@/lib/utils'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'

/* ── 共享状态组件（空/载/错/刷新），前台+管理端通用，语义 token + 暗色适配 ── */

/** 空状态：图标 + 文案，居中 */
export function EmptyState({
  text = '暂无数据',
  icon,
  className,
}: {
  text?: string
  icon?: ReactNode
  className?: string
}) {
  return (
    <div
      data-slot="empty-state"
      className={cn(
        'flex flex-col items-center justify-center gap-2 py-10 text-sm text-muted-foreground',
        className
      )}
    >
      <span aria-hidden="true">{icon ?? <Inbox className="size-7 opacity-40" />}</span>
      <span>{text}</span>
    </div>
  )
}

/** 加载中：旋转图标 + 文案 */
export function Loading({ text = '加载中…', className }: { text?: string; className?: string }) {
  return (
    <div
      data-slot="loading-state"
      role="status"
      aria-live="polite"
      className={cn(
        'flex items-center justify-center gap-2 py-10 text-sm text-muted-foreground',
        className
      )}
    >
      <Loader2 aria-hidden="true" className="size-4 animate-spin" />
      <span>{text}</span>
    </div>
  )
}

/** 错误提示行 */
export function ErrorMsg({
  msg,
  className,
  announce = true,
}: {
  msg?: string
  className?: string
  announce?: boolean
}) {
  return msg ? (
    <p
      data-slot="error-message"
      role={announce ? 'alert' : undefined}
      className={cn(
        'flex items-center gap-1.5 text-sm text-destructive',
        className
      )}
    >
      <AlertCircle aria-hidden="true" className="size-4 shrink-0" />
      {msg}
    </p>
  ) : null
}

/** 刷新按钮（带旋转图标） */
export function RefreshBtn({ onClick, className }: { onClick: () => void; className?: string }) {
  return (
    <Button
      type="button"
      variant="outline"
      size="sm"
      onClick={onClick}
      className={cn('gap-1.5', className)}
    >
      <RefreshCw aria-hidden="true" className="size-3.5" />
      刷新
    </Button>
  )
}

/* ── 状态徽章：对局/赛事/邮件等状态的统一着色 ── */

const STATUS_VARIANT: Record<string, { variant: 'default' | 'secondary' | 'destructive' | 'outline'; label?: string }> = {
  // 对局
  completed: { variant: 'default', label: '已完成' },
  running: { variant: 'default', label: '进行中' },
  pending: { variant: 'secondary', label: '排队中' },
  aborted: { variant: 'destructive', label: '已中止' },
  // 赛事阶段（running 与对局共用「进行中」，上方已声明）
  finished: { variant: 'default', label: '已结束' },
  open: { variant: 'default', label: '报名中' },
  published: { variant: 'secondary', label: '排期已发布' },
  draft: { variant: 'secondary', label: '草稿' },
  cancelled: { variant: 'destructive', label: '已取消' },
  // 邮件
  sent: { variant: 'default', label: '已发送' },
  failed: { variant: 'destructive', label: '失败' },
  skipped: { variant: 'secondary', label: '已跳过' },
  rest: { variant: 'secondary', label: '休息期' },
}

/** 通用状态徽章：自动按状态映射颜色 + 中文标签 */
export function StatusBadge({
  status,
  className,
}: {
  status: string
  className?: string
}) {
  const conf = STATUS_VARIANT[status] ?? { variant: 'secondary' as const }
  return (
    <Badge variant={conf.variant} className={cn('text-[10px]', className)}>
      {conf.label ?? status}
    </Badge>
  )
}
