/**
 * BotVersionManager —— MyBots 版本管理对话框。
 *
 * 功能：上传新版本（选 Botzone 运行模式）、查看版本历史、回滚到指定版本。
 * 经：
 *   - POST /api/bots/{id}/versions        上传新版本
 *   - GET  /api/bots/{id}/versions        列版本历史
 *   - POST /api/bots/{id}/versions/{v}/activate  回滚到指定版本
 *
 * 遵循 AGENTS.md 前端规范：shadcn Dialog/Select + useConfirm + toast。
 */
import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { Upload, History, RotateCcw } from 'lucide-react'
import { fmtTime } from '@/lib/format'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Badge } from '@/components/ui/badge'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { Loading } from '@/components/ui/status'
import { useConfirm } from '@/hooks/use-confirm'
import { apiForm, apiGet, apiJson, errMsg } from '@/api'
import { toast } from 'sonner'

export interface BotVersion {
  id: number
  version: number
  binary_path: string
  upload_note: string
  size_bytes: number
  os: string
  arch: string
  format: string
  runtime_mode: string
  uploaded_at: string
  runnable?: boolean
  unsupported_reason?: string | null
}

interface Props {
  /** 打开对话框的 Bot id；null = 关闭 */
  botId: number | null
  botName?: string
  /** 当前版本（用于高亮 + 回滚后刷新外层列表） */
  currentVersion?: number
  /** Bot 当前运行模式（上传新版本的默认值） */
  currentRuntimeMode?: string
  onClose: () => void
  /** 版本变更（上传/回滚）后回调，外层刷新 bot 列表 */
  onChanged?: () => void
}

const RUNTIME_MODES = [
  { value: 'traditional', label: 'Traditional（默认）', desc: '每个决策点重启进程并发送完整历史信封，Bot 须自行重放。' },
  { value: 'longrunning', label: 'LongRunning（严格长驻）', desc: '进程整场不重启；首回合响应后必须输出 KEEP_RUNNING 握手，之后接收单 request。缺少握手会被拒绝。' },
]

function fmtSize(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(2)} MB`
}

export default function BotVersionManager({
  botId,
  botName,
  currentVersion,
  currentRuntimeMode,
  onClose,
  onChanged,
}: Props) {
  const open = botId !== null
  const [confirm, confirmDialog] = useConfirm()
  const [versions, setVersions] = useState<BotVersion[]>([])
  // 本地缓存的 current_version——上传/回滚后 load() 刷新，立即正确高亮当前版本
  // （外层 prop currentVersion 不会同步刷新，故本地优先）。
  const [curVer, setCurVer] = useState<number | undefined>(undefined)
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  // 上传新版本表单
  const [note, setNote] = useState('')
  const [mode, setMode] = useState(currentRuntimeMode || 'traditional')
  const [file, setFile] = useState<File | null>(null)
  // Dialog 在 A→关闭→B 时会复用同一组件；A 的慢响应不得回灌 B。
  const activeBotIdRef = useRef<number | null>(botId)
  const requestGenerationRef = useRef(0)
  const mutationGenerationRef = useRef(0)
  // Prop changes are authoritative during render, before the reset effect. This
  // closes the narrow render-to-effect window in which A could otherwise still
  // look current after the parent has already reused the dialog for B.
  activeBotIdRef.current = botId

  const isCurrentMutation = (targetBotId: number, generation: number) => (
    activeBotIdRef.current === targetBotId &&
    mutationGenerationRef.current === generation
  )

  const load = useCallback(async () => {
    if (botId === null) return
    const targetBotId = botId
    const generation = ++requestGenerationRef.current
    setLoading(true)
    setError('')
    try {
      const d = await apiGet<{ versions: BotVersion[]; current_version: number }>(
        `/api/bots/${targetBotId}/versions`,
      )
      if (
        activeBotIdRef.current !== targetBotId ||
        requestGenerationRef.current !== generation
      ) return
      setVersions(d.versions || [])
      setCurVer(d.current_version)
    } catch (e) {
      if (
        activeBotIdRef.current !== targetBotId ||
        requestGenerationRef.current !== generation
      ) return
      setError(errMsg(e, '加载版本历史失败'))
    } finally {
      if (
        activeBotIdRef.current === targetBotId &&
        requestGenerationRef.current === generation
      ) setLoading(false)
    }
  }, [botId])

  // 切换 Bot 时重置所有本地状态（busy 泄漏会导致新 Bot 对话框按钮被禁用）
  useEffect(() => {
    activeBotIdRef.current = botId
    requestGenerationRef.current += 1 // 立即使上一个 Bot 的在途响应失效
    mutationGenerationRef.current += 1 // 上传/回滚的 catch/finally 也必须立刻失效
    setVersions([])
    setCurVer(undefined)
    setLoading(false)
    setBusy(false)
    setMode(currentRuntimeMode || 'traditional')
    setNote('')
    setFile(null)
    setError('')
    if (botId !== null) void load()
  }, [botId, currentRuntimeMode, load])

  const onUpload = async (e: FormEvent) => {
    e.preventDefault()
    if (botId === null || !file) {
      setError('请选择 Linux x86_64 ELF 程序文件')
      return
    }
    const targetBotId = botId
    const generation = ++mutationGenerationRef.current
    setBusy(true)
    setError('')
    try {
      await apiForm(`/api/bots/${targetBotId}/versions`, 'POST', {
        upload_note: note,
        runtime_mode: mode,
        file,
      })
      if (!isCurrentMutation(targetBotId, generation)) return
      toast.success(`已上传新版本`)
      setNote('')
      setFile(null)
      await load()
      if (!isCurrentMutation(targetBotId, generation)) return
      onChanged?.()
    } catch (err) {
      if (isCurrentMutation(targetBotId, generation)) {
        setError(errMsg(err, '上传失败'))
      }
    } finally {
      if (isCurrentMutation(targetBotId, generation)) setBusy(false)
    }
  }

  const rollback = async (v: BotVersion) => {
    if (botId === null) return
    if (v.runnable === false) {
      setError(v.unsupported_reason || '该历史版本不是 Linux x86_64 ELF，不能激活')
      return
    }
    const targetBotId = botId
    // 上传/回滚后外层列表的 currentVersion prop 可能尚未刷新；按钮高亮与
    // 防重复判断必须同用本地最新值，否则 v1→v2 后无法在同一弹窗切回 v1。
    if (v.version === (curVer ?? currentVersion)) return
    const ok = await confirm({
      title: `回滚到 v${v.version}?`,
      desc: `将把当前版本切换为 v${v.version}（运行模式: ${v.runtime_mode}）。其他版本保留，可随时再切回。`,
      danger: true,
    })
    // 用户确认期间可能已经关掉 A 并打开 B；绝不能把 A 的版本号发给 B。
    if (!ok || activeBotIdRef.current !== targetBotId) return
    const generation = ++mutationGenerationRef.current
    setBusy(true)
    try {
      await apiJson(`/api/bots/${targetBotId}/versions/${v.version}/activate`, 'POST')
      if (!isCurrentMutation(targetBotId, generation)) return
      toast.success(`已回滚到 v${v.version}`)
      await load()
      if (!isCurrentMutation(targetBotId, generation)) return
      onChanged?.()
    } catch (e) {
      if (isCurrentMutation(targetBotId, generation)) {
        setError(errMsg(e, '回滚失败'))
      }
    } finally {
      if (isCurrentMutation(targetBotId, generation)) setBusy(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <History className="size-4" />
            版本管理{botName ? ` · ${botName}` : ''}
          </DialogTitle>
          <DialogDescription>
            上传新版本、查看历史、回滚到任意旧版本。每版本独立标明 Botzone 运行模式，回滚时恢复。
          </DialogDescription>
        </DialogHeader>

        {/* 上传新版本 */}
        <form onSubmit={(e) => void onUpload(e)} className="space-y-3 rounded-lg border border-border bg-muted/30 p-3">
          <h3 className="text-sm font-medium text-foreground">上传新版本</h3>
          <div className="space-y-1.5">
            <Label>Botzone 运行模式</Label>
            <Select value={mode} onValueChange={setMode}>
              <SelectTrigger className="h-9 w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {RUNTIME_MODES.map((m) => (
                  <SelectItem key={m.value} value={m.value}>
                    {m.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <p className="text-xs text-muted-foreground">
              {RUNTIME_MODES.find((m) => m.value === mode)?.desc}
            </p>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="ver-note">版本备注（可选）</Label>
            <Input id="ver-note" value={note} onChange={(e) => setNote(e.target.value)} maxLength={200} />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="ver-file">程序文件（Linux x86_64 ELF）</Label>
            <label
              htmlFor="ver-file"
              className="flex cursor-pointer items-center gap-2 rounded-md border border-input bg-background px-3 py-2 text-sm text-muted-foreground transition-colors hover:bg-accent"
            >
              <span className="shrink-0 font-medium text-foreground">选择文件</span>
              <span className="min-w-0 truncate">{file?.name || '未选择文件'}</span>
              <input
                id="ver-file"
                type="file"
                onChange={(e) => {
                  const f = e.target.files?.[0] ?? null
                  // 客户端 50MB 预检（与 MyBots 上传一致），避免大文件传完才被服务端拒
                  if (f && f.size > 50 * 1024 * 1024) {
                    toast.error('文件超过 50MB 上限')
                    setFile(null)
                    e.target.value = ''
                    return
                  }
                  setFile(f)
                }}
                required
                className="sr-only"
              />
            </label>
            <p className="text-xs text-muted-foreground">
              仅接受 Linux x86_64 ELF，最大 50MB；Windows .exe、macOS 程序和原始 .py 文件均不支持。
            </p>
          </div>
          {error && <p className="text-sm text-destructive">{error}</p>}
          <Button type="submit" disabled={busy} className="gap-1.5">
            <Upload className="size-4" />
            {busy ? '处理中…' : '上传新版本'}
          </Button>
        </form>

        {/* 版本历史 */}
        <div className="space-y-2">
          <h3 className="text-sm font-medium text-foreground">版本历史</h3>
          {loading ? (
            <Loading text="加载中…" />
          ) : versions.length === 0 ? (
            <p className="text-sm text-muted-foreground">暂无版本记录</p>
          ) : (
            <ul className="space-y-1.5">
              {versions.map((v) => {
                const isCurrent = v.version === (curVer ?? currentVersion)
                return (
                  <li
                    key={v.id}
                    className="flex items-center gap-2 rounded-md border border-border bg-background px-3 py-2"
                  >
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-2 text-sm font-medium text-foreground">
                        <span>v{v.version}</span>
                        {isCurrent && <Badge variant="default">当前</Badge>}
                        <Badge variant="secondary" className="font-mono text-[10px]">
                          {v.runtime_mode}
                        </Badge>
                        {v.runnable === false && <Badge variant="destructive">不可运行</Badge>}
                      </div>
                      <div className="mt-0.5 flex flex-wrap gap-2 text-xs text-muted-foreground">
                        <span>{fmtTime(v.uploaded_at)}</span>
                        <span>{fmtSize(v.size_bytes)}</span>
                        <span>{v.os}/{v.arch}</span>
                      </div>
                      {v.upload_note && (
                        <p className="mt-0.5 truncate text-xs text-muted-foreground">{v.upload_note}</p>
                      )}
                    </div>
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <Button
                          type="button"
                          variant="outline"
                          size="sm"
                          disabled={busy || isCurrent || v.runnable === false}
                          onClick={() => void rollback(v)}
                          className="gap-1"
                        >
                          <RotateCcw className="size-3.5" />
                          回滚
                        </Button>
                      </TooltipTrigger>
                      <TooltipContent>
                        {v.runnable === false
                          ? v.unsupported_reason || '仅支持 Linux x86_64 ELF64（小端）'
                          : isCurrent ? '已是当前版本' : `回滚到 v${v.version}`}
                      </TooltipContent>
                    </Tooltip>
                  </li>
                )
              })}
            </ul>
          )}
        </div>
        {confirmDialog}
      </DialogContent>
    </Dialog>
  )
}
