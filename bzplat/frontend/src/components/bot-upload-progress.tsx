import { CheckCircle2, UploadCloud } from 'lucide-react'

import { cn } from '@/lib/utils'

export const BOT_UPLOAD_MAX_BYTES = 100 * 1024 * 1024
export const BOT_UPLOAD_MAX_LABEL = '100 MiB'

export type BotUploadStage = 'idle' | 'uploading' | 'preflight'

export function botUploadSizeError(file: File): string | null {
  return file.size > BOT_UPLOAD_MAX_BYTES
    ? `文件超过 ${BOT_UPLOAD_MAX_LABEL} 上限`
    : null
}

export function BotUploadProgress({
  stage,
  percent,
  className,
}: {
  stage: BotUploadStage
  percent: number | null
  className?: string
}) {
  if (stage === 'idle') return null
  const transferred = stage === 'preflight'
  const width = transferred ? 100 : percent

  return (
    <div
      className={cn('space-y-1.5 rounded-lg border border-border bg-muted/20 px-3 py-2.5', className)}
      role="status"
      aria-live="polite"
      data-upload-stage={stage}
    >
      <div className="flex min-w-0 items-center justify-between gap-2 text-xs">
        <span className="inline-flex min-w-0 items-center gap-1.5 font-medium text-foreground">
          {transferred
            ? <CheckCircle2 className="size-3.5 shrink-0 text-success" aria-hidden="true" />
            : <UploadCloud className="size-3.5 shrink-0 text-primary" aria-hidden="true" />}
          {transferred ? '文件已上传，正在服务端预检' : '正在上传文件'}
        </span>
        {!transferred && percent !== null && (
          <span className="shrink-0 font-mono tabular-nums text-muted-foreground">{percent}%</span>
        )}
      </div>
      <div
        className="h-1.5 overflow-hidden rounded-full bg-muted"
        role="progressbar"
        aria-label={transferred ? '服务端预检中' : 'Bot 文件上传进度'}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={width ?? undefined}
        aria-valuetext={transferred ? '文件传输完成，服务端预检中' : percent === null ? '正在传输' : `${percent}%`}
      >
        <div
          className={cn(
            'h-full rounded-full bg-primary transition-[width] duration-200',
            width === null && 'w-1/3 animate-pulse',
          )}
          style={width === null ? undefined : { width: `${width}%` }}
        />
      </div>
      {transferred && (
        <p className="text-xs leading-relaxed text-muted-foreground">
          平台正在校验 ELF 格式并运行标准首回合协议；通过后才会发布版本。
        </p>
      )}
    </div>
  )
}
