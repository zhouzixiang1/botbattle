import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import {
  BookOpen,
  Copy,
  Download,
  History,
  KeyRound,
  Laptop,
  MoreHorizontal,
  Pencil,
  Power,
  RotateCcw,
  Save,
  Trash2,
  Upload,
  Wifi,
  WifiOff,
  X,
  Bot as BotIcon,
} from 'lucide-react'
import { useAuth } from '@/components/useAuth'
import { DataRegion, PageFrame, PageHeader, StickyToolbar } from '@/components/layout'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { Badge } from '@/components/ui/badge'
import { EmptyState, ErrorMsg } from '@/components/ui/status'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { useConfirm } from '@/hooks/use-confirm'
import { toast } from 'sonner'
import {
  apiFormWithProgress,
  apiGet,
  apiJson,
  currentUserStore,
  errMsg,
  type CurrentUser,
} from '@/api'
import { GAMES, gameLabel } from '@/lib/games'
import { fmtTime } from '@/lib/format'
import BotVersionManager from '@/components/BotVersionManager'
import Pagination from '@/components/Pagination'
import { Loading } from '@/components/ui/status'
import { EntityName, Identifier, OverflowText } from '@/components/ui/overflow-text'
import {
  BOT_UPLOAD_MAX_LABEL,
  BotUploadProgress,
  botUploadSizeError,
  type BotUploadStage,
} from '@/components/bot-upload-progress'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { CopyIdentifier } from '@/pages/public-page-ui'
import {
  localAgentBotName,
  localAgentStatus,
  type LocalAIAgent,
} from '@/components/runtime-environment'

interface Bot {
  id: number
  name: string
  display_name?: string
  description?: string
  os?: string
  arch?: string
  format?: string
  current_version?: number
  is_active?: number
  updated_at?: string
  game_id?: string
  runtime_mode?: string
  runnable?: boolean
  unsupported_reason?: string | null
}

export default function MyBots() {
  const { user } = useAuth()
  // An account switch must discard every form/request state owned by the old
  // identity instead of reusing the same route component for the new account.
  return <MyBotsForIdentity key={user?.id ?? 'guest'} user={user} />
}

function MyBotsForIdentity({ user }: { user: CurrentUser | null }) {
  const isLoggedIn = user !== null
  const userId = user?.id ?? null
  const [confirm, confirmDialog] = useConfirm()
  const [bots, setBots] = useState<Bot[]>([])
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [name, setName] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [description, setDescription] = useState('')
  const [gameId, setGameId] = useState('holdem')
  const [runtimeMode, setRuntimeMode] = useState('traditional')
  const [filterGame, setFilterGame] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [uploadStage, setUploadStage] = useState<BotUploadStage>('idle')
  const [uploadPercent, setUploadPercent] = useState<number | null>(0)
  const uploadControllerRef = useRef<AbortController | null>(null)
  // 版本管理对话框状态：打开的 bot id（null = 关闭）+ 当前 bot 的运行模式
  const [verBot, setVerBot] = useState<{ id: number; name: string; current: number; mode: string } | null>(null)
  // 分页
  const [page, setPage] = useState(1)
  const [total, setTotal] = useState(0)
  const perPage = 20

  const load = useCallback(async () => {
    if (!isLoggedIn) {
      setLoading(false)
      return
    }
    setLoading(true)
    try {
      const params = new URLSearchParams()
      if (filterGame) params.set('game_id', filterGame)
      params.set('page', String(page))
      params.set('per_page', String(perPage))
      const d = await apiGet<{ bots: Bot[]; total?: number }>(`/api/bots/mine?${params.toString()}`)
      setBots(d.bots || [])
      if (d.total !== undefined) setTotal(d.total)
      setError('')
    } catch (e) {
      setError(errMsg(e, '加载失败'))
    } finally {
      setLoading(false)
    }
  }, [isLoggedIn, filterGame, page])

  useEffect(() => {
    void load()
  }, [load])

  useEffect(() => () => {
    uploadControllerRef.current?.abort()
    uploadControllerRef.current = null
  }, [])

  const onUpload = async (e: FormEvent) => {
    e.preventDefault()
    if (!file) {
      setError('请选择 Linux x86_64 ELF 程序文件')
      return
    }
    uploadControllerRef.current?.abort()
    const controller = new AbortController()
    uploadControllerRef.current = controller
    const targetUserId = userId
    const isCurrentUpload = () => (
      uploadControllerRef.current === controller &&
      !controller.signal.aborted &&
      (currentUserStore.get()?.id ?? null) === targetUserId
    )
    setBusy(true)
    setUploadStage('uploading')
    setUploadPercent(0)
    setError('')
    try {
      await apiFormWithProgress('/api/bots', {
        name,
        display_name: displayName,
        description,
        game_id: gameId,
        runtime_mode: runtimeMode,
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
      setName('')
      setDisplayName('')
      setDescription('')
      setFile(null)
      await load()
      if (!isCurrentUpload()) return
      toast.success('Bot 上传成功')
    } catch (err) {
      if (isCurrentUpload()) setError(errMsg(err, '上传失败'))
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

  const toggleActive = async (bot: Bot) => {
    if (!bot.is_active && bot.runnable === false) {
      setError(bot.unsupported_reason || '该历史 Bot 不是 Linux x86_64 ELF，不能重新启用')
      return
    }
    try {
      await apiJson(
        `/api/bots/${bot.id}/active?active=${bot.is_active ? 'false' : 'true'}`,
        'POST',
      )
      await load()
      toast.success(bot.is_active ? 'Bot 已停用' : 'Bot 已启用')
    } catch (e) {
      setError(errMsg(e, '更新失败'))
    }
  }

  const [editing, setEditing] = useState<number | null>(null)
  const [editDisplay, setEditDisplay] = useState('')
  const [editDesc, setEditDesc] = useState('')

  const startEdit = (b: Bot) => {
    setEditing(b.id)
    setEditDisplay(b.display_name || '')
    setEditDesc(b.description || '')
  }

  const saveEdit = async (b: Bot) => {
    try {
      await apiJson(`/api/bots/${b.id}`, 'PATCH', {
        display_name: editDisplay, description: editDesc,
      })
      setEditing(null)
      await load()
      toast.success('Bot 信息已更新')
    } catch (e) {
      setError(errMsg(e, '更新失败'))
    }
  }

  const del = async (b: Bot) => {
    if (!await confirm({
      title: '删除 Bot',
      desc: `确定删除 ${b.display_name || b.name}？（将停用此 Bot）`,
      confirmText: '删除',
      danger: true,
    })) return
    try {
      await apiJson(`/api/bots/${b.id}`, 'DELETE')
      await load()
      toast.success('Bot 已删除')
    } catch (e) {
      setError(errMsg(e, '删除失败'))
    }
  }

  if (!isLoggedIn) {
    return (
      <PageFrame layout="account-my-bots-guest">
        <PageHeader title="我的 Bot" description="登录后上传、维护与切换 Bot 版本。" />
        <DataRegion title="Bot 管理" className="mx-auto w-full max-w-5xl" contentClassName="space-y-3 px-4 py-6">
          <EmptyState text="请先登录后管理 Bot" icon={<BotIcon className="size-5 opacity-50" />} className="py-3" />
          <div className="flex min-w-0 justify-center"><Button asChild size="sm" className="max-sm:min-h-11"><Link to="/login">前往登录</Link></Button></div>
        </DataRegion>
      </PageFrame>
    )
  }

  return (
    <PageFrame
      layout="account-my-bots"
      className="max-sm:[&_[data-slot=button]]:min-h-11 max-sm:[&_[data-slot=button]]:min-w-11 max-sm:[&_[data-slot=input]]:min-h-11 max-sm:[&_[data-slot=select-trigger]]:min-h-11"
    >
      <PageHeader title="我的 Bot" description="上传 Linux x86_64 ELF，维护公开资料、运行状态与历史版本。" />

      {error && <ErrorMsg msg={error} />}

      <LocalBotConnections identityKey={userId} />

      <div className="grid min-w-0 gap-[var(--page-section-gap)] xl:grid-cols-[22rem_minmax(0,1fr)]">
      <DataRegion title="上传新 Bot" description="上传成功且通过预检后才会发布并激活。" className="self-start">
          <form onSubmit={(e) => void onUpload(e)} className="min-w-0 space-y-3 p-3">
            <div className="space-y-1.5">
              <Label>游戏类型</Label>
              <Select value={gameId} onValueChange={setGameId}>
                <SelectTrigger className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {GAMES.map((g) => (
                    <SelectItem key={g.id} value={g.id}>
                      {g.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label>Botzone 运行模式</Label>
              <Select value={runtimeMode} onValueChange={setRuntimeMode}>
                <SelectTrigger className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="traditional">Traditional（默认）</SelectItem>
                  <SelectItem value="longrunning">LongRunning（严格长驻）</SelectItem>
                </SelectContent>
              </Select>
              <p className="text-xs text-muted-foreground">
                {runtimeMode === 'longrunning'
                  ? '进程整场不重启；首回合响应后必须输出 KEEP_RUNNING 握手，之后接收单 request。缺少握手会被拒绝。'
                  : '平台默认模式；每个决策点重启进程并发送完整历史信封，Bot 须自行重放。'}
              </p>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="upload-name">名称（唯一标识）</Label>
              <Input
                id="upload-name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                required
                minLength={2}
                maxLength={32}
                pattern="[A-Za-z][A-Za-z0-9_]{1,31}"
              />
              <p className="text-xs text-muted-foreground">2–32 位，字母开头，仅可含字母、数字和下划线</p>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="upload-display">显示名</Label>
              <Input
                id="upload-display"
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="upload-desc">简介</Label>
              <Textarea
                id="upload-desc"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                rows={2}
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="upload-file">程序文件（Linux x86_64 ELF）</Label>
              <label
                htmlFor="upload-file"
                className="flex min-h-[var(--control-height)] min-w-0 cursor-pointer items-center gap-2 rounded-md border border-input bg-background px-3 py-2 text-sm text-muted-foreground transition-colors hover:bg-accent focus-within:ring-[3px] focus-within:ring-ring/50 max-sm:min-h-11"
              >
                <span className="shrink-0 font-medium text-foreground">选择文件</span>
                <span className="min-w-0 truncate">{file?.name || '未选择文件'}</span>
                <input
                  id="upload-file"
                  type="file"
                  onChange={(e) => {
                    const f = e.target.files?.[0] ?? null
                    const sizeError = f ? botUploadSizeError(f) : null
                    if (f && sizeError) {
                      setError(sizeError)
                      setFile(null)
                      e.target.value = ''
                      return
                    }
                    setError('')
                    setFile(f)
                  }}
                  required
                  className="sr-only"
                />
              </label>
              <p className="text-xs text-muted-foreground">
                仅接受 Linux x86_64 ELF，最大 {BOT_UPLOAD_MAX_LABEL}；Windows .exe、macOS 程序和原始 .py 文件均不支持。
              </p>
            </div>
            <BotUploadProgress stage={uploadStage} percent={uploadPercent} />
            <Button type="submit" disabled={busy} aria-busy={busy} className="w-full gap-1.5">
              <Upload className="size-4" />
              {uploadStage === 'preflight' ? '服务端预检中…' : busy ? '上传中…' : '上传'}
            </Button>
          </form>
      </DataRegion>

      <div className="flex min-w-0 flex-col gap-[var(--page-section-gap)]">
      <StickyToolbar label="我的 Bot 筛选">
          <span className="shrink-0 text-xs font-medium text-muted-foreground">筛选游戏</span>
          <Select value={filterGame || 'all'} onValueChange={(v) => { setFilterGame(v === 'all' ? '' : v); setPage(1) }}>
            <SelectTrigger className="w-[8.5rem] max-w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">全部</SelectItem>
              {GAMES.map((g) => (
                <SelectItem key={g.id} value={g.id}>
                  {g.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <span className="ml-auto shrink-0 text-xs text-muted-foreground">第 {page} 页</span>
      </StickyToolbar>

      <DataRegion title="Bot 列表" description="主操作保持可见，版本、编辑与删除收纳在更多菜单。">
        {loading ? (
          <Loading text="正在加载 Bot…" />
        ) : bots.length === 0 ? (
          <EmptyState text="暂无 Bot，请先上传" icon={<BotIcon className="size-5 opacity-50" />} className="py-8" />
        ) : (
          <ul className="divide-y divide-border">
            {bots.map((b, index) => (
              <li key={b.id} className="min-w-0 px-3 py-2.5">
                <div className="grid min-w-0 gap-2 sm:grid-cols-[2rem_minmax(0,1fr)_auto] sm:items-start">
                  <span className="hidden pt-1 font-mono text-xs tabular-nums text-muted-foreground sm:block">{(page - 1) * perPage + index + 1}</span>
                  <div className="min-w-0 flex-1">
                    <div className="flex min-w-0 flex-wrap items-center gap-2">
                      <Link to={`/bot/${b.id}`} className="inline-flex min-h-[var(--control-height)] min-w-0 flex-1 items-center hover:text-primary max-sm:min-h-11">
                        <EntityName lines={2} tooltip={false} tooltipFocusable={false} className="text-sm hover:text-primary">{b.display_name || b.name}</EntityName>
                      </Link>
                      <Badge variant="secondary">{gameLabel(b.game_id)}</Badge>
                      {b.runnable === false && <Badge variant="destructive">不可运行</Badge>}
                      <Badge variant={b.is_active ? 'default' : 'outline'}>{b.is_active ? '已启用' : '已停用'}</Badge>
                    </div>
                    {b.description && (
                      <OverflowText lines={2} tooltip={false} className="mt-1 text-xs text-muted-foreground">{b.description}</OverflowText>
                    )}
                    <div className="mt-1 flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
                      <CopyIdentifier value={b.id} />
                      {b.runnable === false && (
                        <span className="max-w-full break-all rounded bg-destructive/10 px-1.5 py-0.5 text-destructive">
                          诊断：{b.format || 'unknown'} / {b.os || 'unknown'}-{b.arch || 'unknown'}
                        </span>
                      )}
                      <Identifier>{b.runtime_mode || 'traditional'}</Identifier>
                      <span>当前版本 v{b.current_version ?? 0}</span>
                    </div>
                    {b.runnable === false && (
                      <p className="mt-1 break-words text-xs text-destructive [overflow-wrap:anywhere]">
                        {b.unsupported_reason || '仅保留为历史记录；请上传 Linux x86_64 ELF 新版本。'}
                      </p>
                    )}
                  </div>
                  <div className="flex min-w-0 shrink-0 items-center gap-1.5">
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <span>
                          <Button
                            type="button"
                            variant="outline"
                            size="sm"
                            disabled={!b.is_active && b.runnable === false}
                            onClick={() => void toggleActive(b)}
                            className="gap-1"
                          >
                            <Power className="size-3.5" />
                            {b.is_active ? '停用' : b.runnable === false ? '不可启用' : '启用'}
                          </Button>
                        </span>
                      </TooltipTrigger>
                      <TooltipContent>
                        {!b.is_active && b.runnable === false
                          ? b.unsupported_reason || '仅支持 Linux x86_64 ELF64（小端）'
                          : b.is_active ? '停用 Bot' : '启用 Bot'}
                      </TooltipContent>
                    </Tooltip>
                    <DropdownMenu>
                      <DropdownMenuTrigger asChild>
                        <Button
                          type="button"
                          variant="outline"
                          size="icon-sm"
                          aria-label={`管理 ${b.display_name || b.name}`}
                          className="max-sm:min-h-11 max-sm:min-w-11"
                        >
                          <MoreHorizontal className="size-4" />
                        </Button>
                      </DropdownMenuTrigger>
                      <DropdownMenuContent align="end">
                        <DropdownMenuItem onSelect={() => setVerBot({ id: b.id, name: b.display_name || b.name, current: b.current_version ?? 0, mode: b.runtime_mode || 'traditional' })}>
                          <History className="size-4" />版本管理
                        </DropdownMenuItem>
                        <DropdownMenuItem onSelect={() => startEdit(b)}><Pencil className="size-4" />编辑资料</DropdownMenuItem>
                        <DropdownMenuSeparator />
                        <DropdownMenuItem variant="destructive" onSelect={() => void del(b)}><Trash2 className="size-4" />删除 Bot</DropdownMenuItem>
                      </DropdownMenuContent>
                    </DropdownMenu>
                  </div>
                </div>
                {editing === b.id && (
                  <div className="mt-2 grid min-w-0 gap-2 rounded-lg bg-muted/50 p-3 sm:grid-cols-2 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.5fr)_auto] lg:items-end">
                    <div className="min-w-0 space-y-1"><Label htmlFor={`edit-display-${b.id}`} className="text-xs">显示名</Label><Input id={`edit-display-${b.id}`} value={editDisplay} onChange={(e) => setEditDisplay(e.target.value)} maxLength={64} className="w-full" /></div>
                    <div className="min-w-0 space-y-1"><Label htmlFor={`edit-desc-${b.id}`} className="text-xs">简介</Label><Input id={`edit-desc-${b.id}`} value={editDesc} onChange={(e) => setEditDesc(e.target.value)} maxLength={500} className="w-full" /></div>
                    <div className="flex min-w-0 shrink-0 items-center gap-1.5">
                    <Button type="button" size="sm" onClick={() => void saveEdit(b)} className="gap-1">
                      <Save className="size-3.5" />保存
                    </Button>
                    <Button type="button" variant="outline" size="sm" onClick={() => setEditing(null)} className="gap-1">
                      <X className="size-3.5" />取消
                    </Button>
                    </div>
                  </div>
                )}
              </li>
            ))}
          </ul>
        )}
      </DataRegion>
      <Pagination page={page} perPage={perPage} total={total} onPageChange={setPage} />
      </div>
      </div>
      {confirmDialog}
      <BotVersionManager
        botId={verBot?.id ?? null}
        identityKey={userId}
        botName={verBot?.name}
        currentVersion={verBot?.current}
        currentRuntimeMode={verBot?.mode}
        onClose={() => setVerBot(null)}
        onChanged={load}
      />
    </PageFrame>
  )
}

interface LocalAgentSecret {
  agent: LocalAIAgent
  token: string
  connection_url: string
}

function websocketUrl(path: string): string {
  try {
    const url = new URL(path, window.location.origin)
    url.protocol = url.protocol === 'https:' || url.protocol === 'wss:' ? 'wss:' : 'ws:'
    return url.toString()
  } catch {
    return path
  }
}

async function copyText(value: string, success: string) {
  try {
    await navigator.clipboard.writeText(value)
    toast.success(success)
  } catch {
    toast.error('复制失败，请手动选择文本复制')
  }
}

function LocalBotConnections({ identityKey }: { identityKey: number | null }) {
  const [confirm, confirmDialog] = useConfirm()
  const [agents, setAgents] = useState<LocalAIAgent[]>([])
  const [availableBots, setAvailableBots] = useState<Bot[]>([])
  const [botId, setBotId] = useState('')
  const [label, setLabel] = useState('我的电脑')
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [issued, setIssued] = useState<LocalAgentSecret | null>(null)

  const load = useCallback(async (quiet = false) => {
    if (identityKey == null) return
    if (!quiet) setLoading(true)
    try {
      const agentRequest = apiGet<{ items: LocalAIAgent[] }>('/api/local-ai/agents')
      const [agentData, botData] = quiet
        ? [await agentRequest, null]
        : await Promise.all([
          agentRequest,
          apiGet<{ bots: Bot[] }>('/api/bots/mine?page=1&per_page=100'),
        ])
      setAgents((agentData.items || []).filter((agent) => agent.status !== 'revoked'))
      if (botData) {
        const runnable = (botData.bots || []).filter((bot) => bot.is_active && bot.runnable !== false)
        setAvailableBots(runnable)
        setBotId((current) => current || (runnable[0] ? String(runnable[0].id) : ''))
      }
      setError('')
    } catch (err) {
      setError(errMsg(err, '本地连接加载失败'))
    } finally {
      if (!quiet) setLoading(false)
    }
  }, [identityKey])

  useEffect(() => {
    void load()
    const timer = window.setInterval(() => void load(true), 5_000)
    return () => window.clearInterval(timer)
  }, [load])

  // Radix mirrors Select state through a hidden native select.  Firefox and
  // WebKit can briefly write the empty sentinel back while async options are
  // replaced.  Keep the first runnable Bot as the effective selection unless
  // the user has explicitly chosen another still-present Bot.
  const effectiveBotId = availableBots.some((bot) => String(bot.id) === botId)
    ? botId
    : availableBots[0] ? String(availableBots[0].id) : ''

  const createAgent = async (event: FormEvent) => {
    event.preventDefault()
    if (!effectiveBotId) return
    setBusy('create')
    setError('')
    try {
      const response = await apiJson<LocalAgentSecret>('/api/local-ai/agents', 'POST', {
        bot_id: Number(effectiveBotId),
        label: label.trim(),
      })
      setIssued(response)
      setLabel('我的电脑')
      await load(true)
      toast.success('本地连接已建立')
    } catch (err) {
      setError(errMsg(err, '建立本地连接失败'))
    } finally {
      setBusy('')
    }
  }

  const rotateToken = async (agent: LocalAIAgent) => {
    if (!await confirm({
      title: '更换连接令牌',
      desc: `${agent.label} 当前使用的令牌会立即失效，需要在你的电脑上换成新令牌。`,
      confirmText: '更换令牌',
    })) return
    setBusy(agent.public_id)
    try {
      const response = await apiJson<LocalAgentSecret>(
        `/api/local-ai/agents/${encodeURIComponent(agent.public_id)}/rotate`,
        'POST',
      )
      setIssued(response)
      await load(true)
      toast.success('连接令牌已更换')
    } catch (err) {
      setError(errMsg(err, '更换令牌失败'))
    } finally {
      setBusy('')
    }
  }

  const revokeAgent = async (agent: LocalAIAgent) => {
    if (!await confirm({
      title: '撤销本地连接',
      desc: `撤销 ${agent.label} 后，这台电脑不能再代替 ${localAgentBotName(agent)} 参加练习对局。`,
      confirmText: '撤销连接',
      danger: true,
    })) return
    setBusy(agent.public_id)
    try {
      await apiJson(`/api/local-ai/agents/${encodeURIComponent(agent.public_id)}`, 'DELETE')
      if (issued?.agent.public_id === agent.public_id) setIssued(null)
      await load(true)
      toast.success('本地连接已撤销')
    } catch (err) {
      setError(errMsg(err, '撤销连接失败'))
    } finally {
      setBusy('')
    }
  }

  const command = issued
    ? `python local_ai_client.py --url '${websocketUrl(issued.connection_url)}' --command './my-bot'`
    : ''

  return (
    <DataRegion
      title="本地 Bot 连接"
      description="先用一个已启用 Bot 作为对局身份；本机程序可运行尚未上传的新代码。本地对局不计平台排行榜。"
      actions={(
        <>
          <Button asChild size="sm" variant="outline">
            <Link to="/wiki?slug=local-ai">
              <BookOpen className="size-4" />接入说明
            </Link>
          </Button>
          <Button asChild size="sm" variant="outline">
            <a href="/api/local-ai/client" download="local_ai_client.py">
              <Download className="size-4" />下载连接器
            </a>
          </Button>
          <Badge variant="outline">平台不访问你的电脑端口</Badge>
        </>
      )}
      contentClassName="space-y-3 p-3"
      data-testid="local-bot-connections"
    >
      {error && <ErrorMsg msg={error} />}
      <form onSubmit={(event) => void createAgent(event)} className="grid min-w-0 gap-2 sm:grid-cols-[minmax(0,1.2fr)_minmax(0,1fr)_auto] sm:items-end">
        <div className="min-w-0 space-y-1">
          <Label>对局中显示为</Label>
          <Select value={effectiveBotId || 'none'} onValueChange={(value) => setBotId(value === 'none' ? '' : value)} disabled={availableBots.length === 0}>
            <SelectTrigger className="w-full" aria-label="对局中显示为"><SelectValue placeholder="选择我的 Bot" /></SelectTrigger>
            <SelectContent>
              {availableBots.length === 0 && <SelectItem value="none" disabled>请先上传并启用 Bot</SelectItem>}
              {availableBots.map((bot) => <SelectItem key={bot.id} value={String(bot.id)}>{bot.display_name || bot.name} · {gameLabel(bot.game_id)}</SelectItem>)}
            </SelectContent>
          </Select>
        </div>
        <div className="min-w-0 space-y-1">
          <Label htmlFor="local-agent-label">连接名称</Label>
          <Input id="local-agent-label" value={label} onChange={(event) => setLabel(event.target.value)} maxLength={32} required />
        </div>
        <Button type="submit" size="sm" disabled={!effectiveBotId || !label.trim() || busy === 'create'}>
          <Laptop className="size-4" />{busy === 'create' ? '建立中…' : '建立连接'}
        </Button>
      </form>

      {issued && (
        <div className="min-w-0 rounded-lg border border-warning/35 bg-warning/10 p-3" role="status" data-testid="local-agent-secret">
          <div className="flex min-w-0 flex-wrap items-center justify-between gap-2">
            <div className="min-w-0">
              <div className="inline-flex items-center gap-1.5 text-sm font-semibold"><KeyRound className="size-4 text-warning" />请立即保存令牌</div>
              <p className="mt-0.5 text-xs text-muted-foreground">令牌只显示这一次；关闭提示后只能更换，不能找回。</p>
            </div>
            <Button type="button" size="sm" variant="outline" onClick={() => setIssued(null)}>我已保存</Button>
          </div>
          <div className="mt-2 grid min-w-0 gap-2 lg:grid-cols-2">
            <div className="min-w-0 rounded-md border bg-background p-2">
              <div className="mb-1 flex items-center justify-between gap-2 text-xs font-medium"><span>连接令牌</span><Button type="button" size="sm" variant="ghost" onClick={() => void copyText(issued.token, '令牌已复制')}><Copy className="size-3.5" />复制</Button></div>
              <code className="block max-w-full break-all text-xs text-foreground">{issued.token}</code>
            </div>
            <div className="min-w-0 rounded-md border bg-background p-2">
              <div className="mb-1 flex items-center justify-between gap-2 text-xs font-medium"><span>启动命令</span><Button type="button" size="sm" variant="ghost" onClick={() => void copyText(command, '启动命令已复制')}><Copy className="size-3.5" />复制</Button></div>
              <code data-testid="local-agent-command" className="block max-w-full break-all text-xs text-foreground">{command}</code>
              <p className="mt-1 text-xs text-muted-foreground">先设置环境变量 <code>BZ_LOCAL_AI_TOKEN</code>，再把 <code>./my-bot</code> 换成你的程序路径。</p>
            </div>
          </div>
        </div>
      )}

      {loading ? <Loading text="正在读取本地连接…" className="py-3" /> : agents.length === 0 ? (
        <EmptyState text="还没有本地连接；选择一个 Bot 建立即可" icon={<Laptop className="size-5 opacity-50" />} className="py-3" />
      ) : (
        <ul className="divide-y divide-border rounded-lg border">
          {agents.map((agent) => {
            const state = localAgentStatus(agent)
            return (
              <li key={agent.public_id} className="grid min-w-0 gap-2 px-3 py-2 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center">
                <div className="min-w-0">
                  <div className="flex min-w-0 flex-wrap items-center gap-2 text-sm font-medium">
                    {state.available ? <Wifi className="size-4 shrink-0 text-primary" /> : <WifiOff className="size-4 shrink-0 text-muted-foreground" />}
                    <span className="break-words [overflow-wrap:anywhere]">{localAgentBotName(agent)}</span>
                    <Badge variant={state.available ? 'default' : agent.status === 'revoked' ? 'destructive' : 'secondary'}>{state.label}</Badge>
                  </div>
                  <p className="mt-0.5 break-words text-xs text-muted-foreground">
                    {agent.label} · {gameLabel(agent.game_id)}
                    {!agent.is_online && ` · ${agent.last_seen_at ? `最近在线 ${fmtTime(agent.last_seen_at)}` : '尚未连接'}`}
                  </p>
                </div>
                <div className="flex min-w-0 flex-wrap gap-1.5">
                  {agent.status !== 'revoked' && <Button type="button" size="sm" variant="outline" disabled={busy === agent.public_id || agent.is_busy} onClick={() => void rotateToken(agent)}><RotateCcw className="size-3.5" />{agent.is_busy ? '对局中' : '换令牌'}</Button>}
                  {agent.status !== 'revoked' && <Button type="button" size="sm" variant="ghost" disabled={busy === agent.public_id || agent.is_busy} onClick={() => void revokeAgent(agent)} className="text-destructive hover:text-destructive"><Trash2 className="size-3.5" />撤销</Button>}
                </div>
              </li>
            )
          })}
        </ul>
      )}
      {confirmDialog}
    </DataRegion>
  )
}
