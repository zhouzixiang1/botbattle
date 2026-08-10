import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { Upload, Trash2, Pencil, Save, X, Power, Bot as BotIcon, History } from 'lucide-react'
import PageStub from '@/components/PageStub'
import { useAuth } from '@/components/useAuth'
import { Card, CardContent } from '@/components/ui/card'
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
    if (!isLoggedIn) return
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
      <PageStub title="我的 Bot">
        <p>
          请先{' '}
          <Link to="/login" className="font-medium text-primary hover:underline">
            登录
          </Link>{' '}
          后管理 Bot。
        </p>
      </PageStub>
    )
  }

  return (
    <PageStub title="我的 Bot" subtitle="上传 Linux x86_64 ELF Bot，并选择对应游戏与运行模式">
      {/* 桌面双栏：左=上传表单（sticky 常驻），右=筛选+列表主区；<lg 单列堆叠 */}
      <div className="lg:grid lg:grid-cols-[20rem_minmax(0,1fr)] lg:gap-6">
      <div className="lg:sticky lg:top-20 lg:self-start">
      <Card>
        <CardContent>
          <form onSubmit={(e) => void onUpload(e)} className="space-y-3">
            <h2 className="text-sm font-medium text-foreground">上传新 Bot</h2>
            <div className="space-y-1.5">
              <Label>游戏类型</Label>
              <Select value={gameId} onValueChange={setGameId}>
                <SelectTrigger className="mt-1.5 h-9 w-full">
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
                <SelectTrigger className="mt-1.5 h-9 w-full">
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
                className="flex cursor-pointer items-center gap-2 rounded-md border border-input bg-background px-3 py-2 text-sm text-muted-foreground transition-colors hover:bg-accent"
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
            {error && <ErrorMsg msg={error} />}
            <Button type="submit" disabled={busy} className="gap-1.5">
              <Upload className="size-4" />
              {busy ? '上传中…' : '上传'}
            </Button>
          </form>
        </CardContent>
      </Card>
      </div>{/* /左栏 sticky */}

      <div className="mt-6 min-w-0 lg:mt-0">
      <div className="mb-3">
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          筛选游戏
          <Select value={filterGame || 'all'} onValueChange={(v) => { setFilterGame(v === 'all' ? '' : v); setPage(1) }}>
            <SelectTrigger className="h-9 w-[8.5rem]">
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
        </div>
      </div>

      <Card className="gap-0 py-0">
        {bots.length === 0 ? (
          <EmptyState text="暂无 Bot，请先上传" icon={<BotIcon className="size-7 opacity-40" />} />
        ) : (
          <ul className="divide-y divide-border">
            {bots.map((b) => (
              <li key={b.id} className="px-4 py-3">
                <div className="flex flex-wrap items-center gap-3">
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2 font-medium text-foreground">
                      <Link to={`/bot/${b.id}`} className="min-w-0 break-words [overflow-wrap:anywhere] hover:text-primary">
                        {b.display_name || b.name}
                      </Link>
                      <span className="font-mono text-xs text-muted-foreground">#{b.id}</span>
                      <Badge variant="secondary">{gameLabel(b.game_id)}</Badge>
                      {b.runnable === false && <Badge variant="destructive">不可运行</Badge>}
                    </div>
                    {b.description && (
                      <p className="mt-0.5 break-words text-xs text-muted-foreground [overflow-wrap:anywhere]">{b.description}</p>
                    )}
                    <div className="mt-1 flex flex-wrap gap-2 text-xs text-muted-foreground">
                      <span className="max-w-full break-all rounded bg-muted px-1.5 py-0.5">
                        {b.os || '—'} / {b.arch || '—'}
                      </span>
                      <span className="max-w-full break-all rounded bg-muted px-1.5 py-0.5">
                        format: {b.format || 'unknown'}
                      </span>
                      <span className="max-w-full break-all rounded bg-muted px-1.5 py-0.5 font-mono">
                        {b.runtime_mode || 'traditional'}
                      </span>
                      <span>v{b.current_version ?? 0}</span>
                      <span>{b.is_active ? '启用' : '停用'}</span>
                    </div>
                    {b.runnable === false && (
                      <p className="mt-1 break-words text-xs text-destructive [overflow-wrap:anywhere]">
                        {b.unsupported_reason || '仅保留为历史记录；请上传 Linux x86_64 ELF 新版本。'}
                      </p>
                    )}
                  </div>
                  <div className="flex flex-wrap gap-1.5">
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
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <Button
                          type="button"
                          variant="outline"
                          size="sm"
                          onClick={() =>
                            setVerBot({
                              id: b.id,
                              name: b.display_name || b.name,
                              current: b.current_version ?? 0,
                              mode: b.runtime_mode || 'traditional',
                            })
                          }
                          className="gap-1"
                        >
                          <History className="size-3.5" />
                          版本
                        </Button>
                      </TooltipTrigger>
                      <TooltipContent>上传新版本 / 查看历史 / 回滚</TooltipContent>
                    </Tooltip>
                    <Button type="button" variant="outline" size="sm" onClick={() => startEdit(b)} className="gap-1">
                      <Pencil className="size-3.5" />
                      编辑
                    </Button>
                    <Button type="button" variant="destructive" size="sm" onClick={() => void del(b)} className="gap-1">
                      <Trash2 className="size-3.5" />
                      删除
                    </Button>
                  </div>
                </div>
                {editing === b.id && (
                  <div className="mt-2 flex min-w-0 flex-wrap items-end gap-2 rounded-lg bg-muted p-3">
                    <label className="min-w-0 flex-[1_1_12rem] space-y-1 text-xs text-muted-foreground">
                      显示名
                      <Input value={editDisplay} onChange={(e) => setEditDisplay(e.target.value)} maxLength={64} className="h-8 w-full max-w-full" />
                    </label>
                    <label className="min-w-0 flex-[1_1_12rem] space-y-1 text-xs text-muted-foreground">
                      简介
                      <Input value={editDesc} onChange={(e) => setEditDesc(e.target.value)} maxLength={500} className="h-8 w-full max-w-full" />
                    </label>
                    <Button type="button" size="sm" onClick={() => void saveEdit(b)} className="gap-1">
                      <Save className="size-3.5" />保存
                    </Button>
                    <Button type="button" variant="outline" size="sm" onClick={() => setEditing(null)} className="gap-1">
                      <X className="size-3.5" />取消
                    </Button>
                  </div>
                )}
              </li>
            ))}
          </ul>
        )}
        <Pagination page={page} perPage={perPage} total={total} onPageChange={setPage} />
      </Card>
      </div>{/* /右栏 */}
      </div>{/* /桌面双栏栅格 */}
      {confirmDialog}
      <BotVersionManager
        botId={verBot?.id ?? null}
        botName={verBot?.name}
        currentVersion={verBot?.current}
        currentRuntimeMode={verBot?.mode}
        onClose={() => setVerBot(null)}
        onChanged={load}
      />
    </PageStub>
  )
}
