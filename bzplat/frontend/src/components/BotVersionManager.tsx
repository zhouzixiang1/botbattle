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
import { fmtBytes, fmtTime } from '@/lib/format'
import { DataRegion } from '@/components/layout'
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
import { EmptyState, ErrorMsg, Loading } from '@/components/ui/status'
import { EntityName, Identifier, OverflowText } from '@/components/ui/overflow-text'
import {
  BOT_UPLOAD_MAX_LABEL,
  BotUploadProgress,
  botUploadSizeError,
  SOURCE_UPLOAD_MAX_LABEL,
  type BotUploadStage,
} from '@/components/bot-upload-progress'
import {
  buildEditorSourceFile,
  SourceEditorField,
  sourceEditorSizeError,
  sourceFileAccept,
  SourceUploadModeToggle,
  type SourceUploadMode,
} from '@/components/source-upload-fields'
import { useConfirm } from '@/hooks/use-confirm'
import { apiFormWithProgress, apiGet, apiJson, errMsg } from '@/api'
import { runtimeModeLabel } from '@/lib/labels'
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
  /** 当前账号；账号切换会使旧账号的请求与回调立即失效。 */
  identityKey: number | null
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
  { value: 'traditional', label: '标准对局（默认）', desc: '每个决策点重新启动进程，并下发完整历史，Bot 需自行还原局面。' },
  { value: 'longrunning', label: '长驻对局', desc: '整场对局只启动一次进程；首回合后需要按公开协议声明继续运行，未声明会被拒绝。' },
]

export default function BotVersionManager({
  botId,
  identityKey,
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
  const [loadError, setLoadError] = useState('')
  const [mutationError, setMutationError] = useState('')
  // 上传新版本表单
  const [note, setNote] = useState('')
  const [mode, setMode] = useState(currentRuntimeMode || 'traditional')
  const [file, setFile] = useState<File | null>(null)
  const [sourceFormat, setSourceFormat] = useState('elf')
  const [sourceEntry, setSourceEntry] = useState('')
  // 源码类型的上传方式（文件 / 在线编辑）与编辑器代码；ELF 恒为文件上传。
  const [sourceUploadMode, setSourceUploadMode] = useState<SourceUploadMode>('file')
  const [sourceCode, setSourceCode] = useState('')
  const [uploadStage, setUploadStage] = useState<BotUploadStage>('idle')
  const [uploadPercent, setUploadPercent] = useState<number | null>(0)
  // Dialog 在 A→关闭→B 时会复用同一组件；A 的慢响应不得回灌 B。
  const activeBotIdRef = useRef<number | null>(botId)
  const activeIdentityKeyRef = useRef<number | null>(identityKey)
  const requestGenerationRef = useRef(0)
  const mutationGenerationRef = useRef(0)
  const uploadControllerRef = useRef<AbortController | null>(null)
  // Prop changes are authoritative during render, before the reset effect. This
  // closes the narrow render-to-effect window in which A could otherwise still
  // look current after the parent has already reused the dialog for B.
  activeBotIdRef.current = botId
  activeIdentityKeyRef.current = identityKey

  const isCurrentMutation = (
    targetBotId: number,
    generation: number,
    targetIdentityKey: number | null = identityKey,
  ) => (
    activeBotIdRef.current === targetBotId &&
    activeIdentityKeyRef.current === targetIdentityKey &&
    mutationGenerationRef.current === generation
  )

  const load = useCallback(async () => {
    if (botId === null) return
    const targetBotId = botId
    const targetIdentityKey = identityKey
    const generation = ++requestGenerationRef.current
    setLoading(true)
    setLoadError('')
    try {
      const d = await apiGet<{ versions: BotVersion[]; current_version: number }>(
        `/api/bots/${targetBotId}/versions`,
      )
      if (
        activeBotIdRef.current !== targetBotId ||
        activeIdentityKeyRef.current !== targetIdentityKey ||
        requestGenerationRef.current !== generation
      ) return
      setVersions(d.versions || [])
      setCurVer(d.current_version)
    } catch (e) {
      if (
        activeBotIdRef.current !== targetBotId ||
        activeIdentityKeyRef.current !== targetIdentityKey ||
        requestGenerationRef.current !== generation
      ) return
      setLoadError(errMsg(e, '加载版本历史失败'))
    } finally {
      if (
        activeBotIdRef.current === targetBotId &&
        activeIdentityKeyRef.current === targetIdentityKey &&
        requestGenerationRef.current === generation
      ) setLoading(false)
    }
  }, [botId, identityKey])

  // 切换 Bot 时重置所有本地状态（busy 泄漏会导致新 Bot 对话框按钮被禁用）
  useEffect(() => {
    uploadControllerRef.current?.abort()
    uploadControllerRef.current = null
    activeBotIdRef.current = botId
    activeIdentityKeyRef.current = identityKey
    requestGenerationRef.current += 1 // 立即使上一个 Bot 的在途响应失效
    mutationGenerationRef.current += 1 // 上传/回滚的 catch/finally 也必须立刻失效
    setVersions([])
    setCurVer(undefined)
    setLoading(false)
    setBusy(false)
    setMode(currentRuntimeMode || 'traditional')
    setNote('')
    setSourceFormat('elf')
    setSourceEntry('')
    setSourceUploadMode('file')
    setSourceCode('')
    setFile(null)
    setUploadStage('idle')
    setUploadPercent(0)
    setLoadError('')
    setMutationError('')
    if (botId !== null) void load()
    return () => {
      requestGenerationRef.current += 1
      mutationGenerationRef.current += 1
      uploadControllerRef.current?.abort()
      uploadControllerRef.current = null
    }
  }, [botId, currentRuntimeMode, identityKey, load])

  // 两种上传方式互斥：切换时丢弃旧选择（含编辑器构造的单文件），入口回到默认。
  const onUploadModeChange = (mode: SourceUploadMode) => {
    setSourceUploadMode(mode)
    setFile(null)
    if (mode === 'editor') setSourceEntry('')
  }

  // 在线编辑模式：把代码实时构造成规范入口名的单文件进入既有 file 状态，
  // FormData 提交链与 BotUploadProgress 保持零改动。
  useEffect(() => {
    if (sourceFormat === 'elf' || sourceUploadMode !== 'editor') return
    setFile(buildEditorSourceFile(sourceFormat, sourceCode))
  }, [sourceFormat, sourceUploadMode, sourceCode])

  const onUpload = async (e: FormEvent) => {
    e.preventDefault()
    const editorActive = sourceFormat !== 'elf' && sourceUploadMode === 'editor'
    // 超限错误已由编辑器内联展示，这里只负责阻止提交。
    if (editorActive && sourceEditorSizeError(sourceCode)) return
    if (botId === null || (editorActive ? !sourceCode.trim() : !file)) {
      setMutationError(editorActive ? '请输入在线编辑代码，或改用文件上传' : '请选择 Linux x86_64 ELF 程序文件')
      return
    }
    const targetBotId = botId
    const targetIdentityKey = identityKey
    const generation = ++mutationGenerationRef.current
    uploadControllerRef.current?.abort()
    const controller = new AbortController()
    uploadControllerRef.current = controller
    const isCurrentUpload = () => (
      isCurrentMutation(targetBotId, generation, targetIdentityKey) &&
      uploadControllerRef.current === controller &&
      !controller.signal.aborted
    )
    setBusy(true)
    setUploadStage('uploading')
    setUploadPercent(0)
    setMutationError('')
    try {
      await apiFormWithProgress(`/api/bots/${targetBotId}/versions`, {
        upload_note: note,
        runtime_mode: mode,
        source_format: sourceFormat,
        ...(sourceFormat !== 'elf' && sourceEntry.trim() ? { source_entry: sourceEntry.trim() } : {}),
        file,
      }, {
        signal: controller.signal,
        onProgress: ({ percent }) => {
          if (isCurrentUpload()) setUploadPercent(percent)
        },
        onTransferComplete: () => {
          if (!isCurrentUpload()) return
          setUploadPercent(100)
          setUploadStage('preflight')
        },
      })
      if (!isCurrentUpload()) return
      toast.success(`已上传新版本`)
      setNote('')
      setFile(null)
      setSourceCode('')
      await load()
      if (!isCurrentUpload()) return
      onChanged?.()
    } catch (err) {
      if (isCurrentUpload()) {
        setMutationError(errMsg(err, '上传失败'))
      }
    } finally {
      const isCurrent = isCurrentUpload()
      if (uploadControllerRef.current === controller) {
        uploadControllerRef.current = null
      }
      if (isCurrent) {
        setBusy(false)
        setUploadStage('idle')
        setUploadPercent(0)
      }
    }
  }

  const rollback = async (v: BotVersion) => {
    if (botId === null) return
    if (v.runnable === false) {
      setMutationError(v.unsupported_reason || '该历史版本不是 Linux x86_64 ELF，不能激活')
      return
    }
    const targetBotId = botId
    const targetIdentityKey = identityKey
    // 上传/回滚后外层列表的 currentVersion prop 可能尚未刷新；按钮高亮与
    // 防重复判断必须同用本地最新值，否则 v1→v2 后无法在同一弹窗切回 v1。
    if (v.version === (curVer ?? currentVersion)) return
    const ok = await confirm({
      title: `回滚到 v${v.version}?`,
      desc: `将把当前版本切换为 v${v.version}（对战流程：${runtimeModeLabel(v.runtime_mode)}）。其他兼容版本仍会保留；已退役协议版本不能恢复。`,
      danger: true,
    })
    // 用户确认期间可能已经关掉 A 并打开 B；绝不能把 A 的版本号发给 B。
    if (
      !ok ||
      activeBotIdRef.current !== targetBotId ||
      activeIdentityKeyRef.current !== targetIdentityKey
    ) return
    const generation = ++mutationGenerationRef.current
    setBusy(true)
    setMutationError('')
    try {
      await apiJson(`/api/bots/${targetBotId}/versions/${v.version}/activate`, 'POST')
      if (!isCurrentMutation(targetBotId, generation, targetIdentityKey)) return
      toast.success(`已回滚到 v${v.version}`)
      await load()
      if (!isCurrentMutation(targetBotId, generation, targetIdentityKey)) return
      onChanged?.()
    } catch (e) {
      if (isCurrentMutation(targetBotId, generation, targetIdentityKey)) {
        setMutationError(errMsg(e, '回滚失败'))
      }
    } finally {
      if (isCurrentMutation(targetBotId, generation, targetIdentityKey)) setBusy(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent
        data-scroll-region="bot-version-dialog"
        data-overflow-allowed="y"
        className="max-h-[85dvh] overflow-y-auto overscroll-contain sm:max-w-xl lg:max-w-3xl lg:grid-cols-[minmax(0,1fr)_minmax(0,1.2fr)] lg:items-start lg:gap-4"
      >
        <DialogHeader className="min-w-0 lg:col-span-2">
          <DialogTitle className="flex min-w-0 items-start gap-2">
            <History className="size-4" />
            <span className="shrink-0">版本管理</span>
            {botName && <EntityName lines={2} tooltip={botName} className="min-w-0 text-sm">{botName}</EntityName>}
          </DialogTitle>
          <DialogDescription>
            上传新版本、查看历史，并切换到当前协议兼容的版本。过旧的版本已不能启用。
          </DialogDescription>
        </DialogHeader>

        <DataRegion title="上传新版本" description="服务端检查通过后发布，并自动切换为当前版本。">
        <form onSubmit={(e) => void onUpload(e)} className="min-w-0 space-y-3 p-3">
          <div className="grid min-w-0 gap-3 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label>对战流程</Label>
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
          </div>
          <div className="space-y-1.5">
            <div className="space-y-1.5">
                <Label>程序类型</Label>
                <Select value={sourceFormat} onValueChange={(v) => { setSourceFormat(v); setSourceEntry(''); setSourceUploadMode('file'); setFile(null) }}>
                  <SelectTrigger className="w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="elf">ELF 程序</SelectItem>
                    <SelectItem value="cpp">C / C++ 源码</SelectItem>
                    <SelectItem value="c">C 源码</SelectItem>
                    <SelectItem value="go">Go 源码</SelectItem>
                    <SelectItem value="python">Python 源码</SelectItem>
                  </SelectContent>
                </Select>
                {sourceFormat !== 'elf' && sourceUploadMode === 'file' && (
                  <div className="space-y-1.5">
                    <Label htmlFor="ver-entry">入口文件（可选，默认见下）</Label>
                    <Input
                      id="ver-entry"
                      value={sourceEntry}
                      onChange={(e) => setSourceEntry(e.target.value)}
                      placeholder={sourceFormat === 'python' ? '__main__.py' : `main.${sourceFormat === 'go' ? 'go' : sourceFormat === 'c' ? 'c' : 'cpp'}`}
                      maxLength={200}
                    />
                  </div>
                )}
              </div>
              {sourceFormat !== 'elf' && (
                <SourceUploadModeToggle mode={sourceUploadMode} onModeChange={onUploadModeChange} />
              )}
              {sourceFormat !== 'elf' && sourceUploadMode === 'editor' ? (
                <SourceEditorField
                  id="ver-source"
                  sourceFormat={sourceFormat}
                  code={sourceCode}
                  onCodeChange={setSourceCode}
                />
              ) : (
                <>
                  <Label htmlFor="ver-file">程序文件{sourceFormat === 'elf' ? '（Linux x86_64 ELF）' : `（zip 源码包或单个源文件，最大 ${SOURCE_UPLOAD_MAX_LABEL}）`}</Label>
                  <label
                    htmlFor="ver-file"
                    className="flex min-h-[var(--control-height)] min-w-0 max-sm:min-h-11 cursor-pointer items-center gap-2 rounded-md border border-input bg-background px-3 py-2 text-sm text-muted-foreground transition-colors hover:bg-accent focus-within:ring-[3px] focus-within:ring-ring/50"
                  >
                    <span className="shrink-0 font-medium text-foreground">选择文件</span>
                    <span className="min-w-0 truncate">{file?.name || '未选择文件'}</span>
                    <input
                      id="ver-file"
                      type="file"
                      accept={sourceFileAccept(sourceFormat)}
                      onChange={(e) => {
                        const f = e.target.files?.[0] ?? null
                        const sizeError = f ? botUploadSizeError(f, sourceFormat) : null
                        if (f && sizeError) {
                          toast.error(sizeError)
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
                </>
              )}
            <p className="text-xs text-muted-foreground">
              {sourceFormat === 'elf'
                  ? `仅接受 Linux x86_64 ELF，最大 ${BOT_UPLOAD_MAX_LABEL}；Windows .exe 和 macOS 程序不支持。`
                  : sourceFormat === 'python'
                    ? '支持 zip 源码包或单个 .py 文件，也可在在线编辑器中直接粘贴代码；平台直接运行，当前仅支持标准库。'
                    : '支持 zip 源码包或与语言匹配的单个源文件，也可在在线编辑器中直接粘贴代码；平台自动完成编译。'}
            </p>
          </div>
          {mutationError && <ErrorMsg msg={mutationError} />}
          <BotUploadProgress
            stage={uploadStage}
            percent={uploadPercent}
            {...(sourceFormat === 'elf'
              ? {}
              : {
                  verifyingLabel: '文件已上传，正在服务端编译校验',
                  verifyingProgressLabel: '服务端编译校验中',
                  verifyingHint: '平台正在编译源码并运行标准首回合协议；通过后才会发布版本。',
                })}
          />
          <Button type="submit" disabled={busy} aria-busy={busy} className="w-full gap-1.5">
            <Upload className="size-4" />
            {uploadStage === 'preflight'
              ? sourceFormat === 'elf' ? '服务端检查中…' : '服务端编译校验中…'
              : busy ? '上传中…' : '上传新版本'}
          </Button>
        </form>
        </DataRegion>

        <DataRegion title="版本历史" description="版本号是该 Bot 内部的业务序列；只有当前协议兼容版本可以切换。">
          {loading ? (
            <Loading text="加载中…" />
          ) : loadError ? (
            <ErrorMsg msg={loadError} className="px-4 py-6" />
          ) : versions.length === 0 ? (
            <EmptyState text="暂无版本记录" icon={<History className="size-5 opacity-50" />} className="py-7" />
          ) : (
            <ul className="divide-y divide-border">
              {versions.map((v) => {
                const isCurrent = v.version === (curVer ?? currentVersion)
                return (
                  <li
                    key={v.id}
                    className="flex min-w-0 items-start gap-2 px-3 py-2.5"
                  >
                    <div className="min-w-0 flex-1">
                      <div className="flex min-w-0 flex-wrap items-center gap-2 text-sm font-medium text-foreground">
                        <span>v{v.version}</span>
                        {isCurrent && <Badge variant="default">当前</Badge>}
                        {v.runnable === false && (
                          <Badge variant="destructive">
                            {v.unsupported_reason === '该版本已退役' ? '已退役' : '不可运行'}
                          </Badge>
                        )}
                      </div>
                      <div className="mt-0.5 flex min-w-0 flex-wrap gap-x-3 gap-y-1 text-xs text-muted-foreground">
                        <span>{fmtTime(v.uploaded_at)}</span>
                        <span>{fmtBytes(v.size_bytes)}</span>
                        <Identifier>{runtimeModeLabel(v.runtime_mode)}</Identifier>
                        {v.runnable === false && (
                          <span className="text-destructive">
                            {v.unsupported_reason || '该文件不是受支持的 Linux 程序'}
                          </span>
                        )}
                      </div>
                      {v.upload_note && (
                        <OverflowText lines={2} tooltip={false} className="mt-0.5 text-xs text-muted-foreground">{v.upload_note}</OverflowText>
                      )}
                    </div>
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <span className="inline-flex min-w-0 shrink-0">
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
                        </span>
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
        </DataRegion>
        {confirmDialog}
      </DialogContent>
    </Dialog>
  )
}
