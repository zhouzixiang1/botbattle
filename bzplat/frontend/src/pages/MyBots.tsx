import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { Upload, Trash2, Pencil, Save, X, Power, Bot as BotIcon, History, MoreHorizontal } from 'lucide-react'
import { useAuth } from '@/components/useAuth'
import { DataRegion, PageFrame, PageHeader, StickyToolbar, SummaryStrip } from '@/components/layout'
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
import { apiForm, apiGet, apiJson, errMsg } from '@/api'
import { GAMES, gameLabel } from '@/lib/games'
import BotVersionManager from '@/components/BotVersionManager'
import Pagination from '@/components/Pagination'
import { Loading } from '@/components/ui/status'
import { EntityName, Identifier, OverflowText } from '@/components/ui/overflow-text'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { CopyIdentifier, SummaryMetric } from '@/pages/public-page-ui'

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
  const { isLoggedIn } = useAuth()
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

  const onUpload = async (e: FormEvent) => {
    e.preventDefault()
    if (!file) {
      setError('请选择 Linux x86_64 ELF 程序文件')
      return
    }
    setBusy(true)
    setError('')
    try {
      await apiForm('/api/bots', 'POST', {
        name,
        display_name: displayName,
        description,
        game_id: gameId,
        runtime_mode: runtimeMode,
        file,
      })
      setName('')
      setDisplayName('')
      setDescription('')
      setFile(null)
      await load()
      toast.success('Bot 上传成功')
    } catch (err) {
      setError(errMsg(err, '上传失败'))
    } finally {
      setBusy(false)
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
          <div className="flex min-w-0 justify-center"><Button asChild size="sm"><Link to="/login">前往登录</Link></Button></div>
        </DataRegion>
      </PageFrame>
    )
  }

  const activeCount = bots.filter((bot) => Boolean(bot.is_active)).length
  const runnableCount = bots.filter((bot) => bot.runnable !== false).length

  return (
    <PageFrame layout="account-my-bots">
      <PageHeader title="我的 Bot" description="上传 Linux x86_64 ELF，维护公开资料、运行状态与历史版本。" />

      <SummaryStrip columns={3}>
        <SummaryMetric label="Bot 总数" value={total} detail={filterGame ? gameLabel(filterGame) : '全部游戏'} icon={<BotIcon className="size-4" />} />
        <SummaryMetric label="本页启用" value={activeCount} detail={`本页共 ${bots.length} 个`} icon={<Power className="size-4" />} />
        <SummaryMetric label="本页可运行" value={runnableCount} detail="符合当前 ELF 契约" />
      </SummaryStrip>

      {error && <ErrorMsg msg={error} />}

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
                className="flex min-w-0 cursor-pointer items-center gap-2 rounded-md border border-input bg-background px-3 py-2 text-sm text-muted-foreground transition-colors hover:bg-accent focus-within:ring-[3px] focus-within:ring-ring/50"
              >
                <span className="shrink-0 font-medium text-foreground">选择文件</span>
                <span className="min-w-0 truncate">{file?.name || '未选择文件'}</span>
                <input
                  id="upload-file"
                  type="file"
                  onChange={(e) => {
                    const f = e.target.files?.[0] ?? null
                    // 前端预校验：超过 50MB 直接拒绝（与服务端限制一致，避免无谓上传）
                    if (f && f.size > 50 * 1024 * 1024) {
                      setError('文件过大，请上传 ≤50MB 的 Linux x86_64 ELF 程序文件')
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
            <Button type="submit" disabled={busy} aria-busy={busy} className="w-full gap-1.5">
              <Upload className="size-4" />
              {busy ? '上传中…' : '上传'}
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
                      <Link to={`/bot/${b.id}`} className="min-w-0 flex-1 hover:text-primary">
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
                        <Button type="button" variant="outline" size="icon-sm" aria-label={`管理 ${b.display_name || b.name}`}><MoreHorizontal className="size-4" /></Button>
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
        botName={verBot?.name}
        currentVersion={verBot?.current}
        currentRuntimeMode={verBot?.mode}
        onClose={() => setVerBot(null)}
        onChanged={load}
      />
    </PageFrame>
  )
}
