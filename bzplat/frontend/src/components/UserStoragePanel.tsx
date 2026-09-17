import { useCallback, useEffect, useRef, useState } from 'react'
import { CloudUpload, HardDrive, Loader2, Trash2 } from 'lucide-react'
import { toast } from 'sonner'

import { apiFormWithProgress, apiGet, apiJson, errMsg } from '@/api'
import { Button } from '@/components/ui/button'
import { DataRegion } from '@/components/layout'
import { EmptyState, ErrorMsg, Loading } from '@/components/ui/status'
import { OverflowText } from '@/components/ui/overflow-text'
import { useConfirm } from '@/hooks/use-confirm'
import { BotUploadProgress } from '@/components/bot-upload-progress'
import { fmtBytes, fmtTime } from '@/lib/format'
import { cn } from '@/lib/utils'

interface StorageFile {
  name: string
  sha256: string
  size_bytes: number
  created_at: string
  updated_at: string
}

interface StorageFilesResponse {
  files: StorageFile[]
  usage: { files: number; bytes: number }
  quota: { bytes: number; max_files: number }
}

export function UserStoragePanel() {
  const [confirm, confirmDialog] = useConfirm()
  const [data, setData] = useState<StorageFilesResponse | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [uploadPercent, setUploadPercent] = useState<number | null>(null)
  const [uploading, setUploading] = useState(false)
  const [deletingName, setDeletingName] = useState<string | null>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      setData(await apiGet<StorageFilesResponse>('/api/storage/files'))
    } catch (cause) {
      setError(errMsg(cause, '云存储加载失败，请稍后重试'))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void load() }, [load])

  const quotaBytes = data?.quota.bytes ?? 0
  const usedBytes = data?.usage.bytes ?? 0
  const usedPercent = quotaBytes > 0
    ? Math.min(100, Math.round((usedBytes / quotaBytes) * 100))
    : 0
  const remainingBytes = Math.max(0, quotaBytes - usedBytes)

  const onPickFile = () => fileInputRef.current?.click()

  const onUpload = async (file: File) => {
    if (uploading) return
    if (file.size > remainingBytes) {
      toast.error(
        `空间不足：剩余 ${fmtBytes(remainingBytes)}，这个文件有 ${fmtBytes(file.size)}`,
      )
      return
    }
    if (file.size < 1) {
      toast.error('不能上传空文件')
      return
    }
    setUploading(true)
    setUploadPercent(0)
    try {
      await apiFormWithProgress('/api/storage/files', { file }, {
        onProgress: ({ percent }) => setUploadPercent(percent),
      })
      toast.success(`已上传 ${file.name}`)
      await load()
    } catch (cause) {
      toast.error(errMsg(cause, '上传失败，请稍后重试'))
    } finally {
      setUploading(false)
      setUploadPercent(null)
      if (fileInputRef.current) fileInputRef.current.value = ''
    }
  }

  const onDelete = async (file: StorageFile) => {
    if (deletingName) return
    const ok = await confirm({
      title: `删除「${file.name}」？`,
      desc: `该文件占用 ${fmtBytes(file.size_bytes)}，删除后不可恢复。`,
      danger: true,
    })
    if (!ok) return
    setDeletingName(file.name)
    try {
      await apiJson(`/api/storage/files/${encodeURIComponent(file.name)}`, 'DELETE')
      toast.success(`已删除 ${file.name}`)
      await load()
    } catch (cause) {
      toast.error(errMsg(cause, '删除失败，请稍后重试'))
    } finally {
      setDeletingName(null)
    }
  }

  return (
    <DataRegion
      title="云存储"
      description="存放模型权重等数据文件；同名上传覆盖旧文件。Bot 对局读取能力将在后续版本开放。"
      actions={
        <>
          <input
            ref={fileInputRef}
            type="file"
            className="hidden"
            aria-label="选择要上传的文件"
            onChange={(event) => {
              const file = event.target.files?.[0]
              if (file) void onUpload(file)
            }}
          />
          <Button
            size="sm"
            onClick={onPickFile}
            disabled={uploading}
            data-storage-upload
          >
            {uploading
              ? <Loader2 className="size-4 animate-spin" aria-hidden="true" />
              : <CloudUpload className="size-4" aria-hidden="true" />}
            上传文件
          </Button>
        </>
      }
    >
      <div className="space-y-3 px-3 py-3">
        <div
          className="rounded-lg border border-border bg-muted/20 px-3 py-2.5"
          data-storage-quota
        >
          <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
            <span className="inline-flex items-center gap-1.5 font-medium text-foreground">
              <HardDrive className="size-3.5 text-primary" aria-hidden="true" />
              已用 {fmtBytes(usedBytes)} / {fmtBytes(quotaBytes)}
            </span>
            <span className="text-muted-foreground">
              {data ? `${data.usage.files}/${data.quota.max_files} 个文件` : ''}
            </span>
          </div>
          <div
            className="mt-2 h-1.5 overflow-hidden rounded-full bg-muted"
            role="progressbar"
            aria-label="云存储空间用量"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={usedPercent}
          >
            <div
              className={cn(
                'h-full rounded-full bg-primary transition-[width] duration-200',
              )}
              style={{ width: `${usedPercent}%` }}
            />
          </div>
        </div>

        <BotUploadProgress
          stage={uploading ? 'uploading' : 'idle'}
          percent={uploadPercent}
          progressLabel="云存储文件上传进度"
        />

        {error ? (
          <ErrorMsg msg={error} className="py-4" />
        ) : loading && !data ? (
          <Loading text="正在加载云存储…" />
        ) : !data || data.files.length === 0 ? (
          <EmptyState
            text="还没有文件；点右上角「上传文件」，把模型权重等数据文件放进来。"
            icon={<HardDrive className="size-5 opacity-50" />}
            className="py-8"
          />
        ) : (
          <ul className="divide-y divide-border border-t border-b border-border">
            {data.files.map((file) => (
              <li
                key={file.name}
                className="flex min-h-[44px] items-center gap-3 py-1.5"
                data-storage-file={file.name}
              >
                <div className="min-w-0 flex-1">
                  <OverflowText className="text-sm font-medium text-foreground" tooltipFocusable>
                    {file.name}
                  </OverflowText>
                  <span className="text-xs text-muted-foreground">
                    {fmtBytes(file.size_bytes)} · 更新于 {fmtTime(file.updated_at)}
                  </span>
                </div>
                <Button
                  variant="outline"
                  size="sm"
                  className="h-11 px-3 text-destructive hover:text-destructive"
                  onClick={() => void onDelete(file)}
                  disabled={deletingName === file.name || uploading}
                  aria-label={`删除文件 ${file.name}`}
                  data-storage-delete={file.name}
                >
                  {deletingName === file.name
                    ? <Loader2 className="size-4 animate-spin" aria-hidden="true" />
                    : <Trash2 className="size-4" aria-hidden="true" />}
                  删除
                </Button>
              </li>
            ))}
          </ul>
        )}
      </div>
      {confirmDialog}
    </DataRegion>
  )
}
