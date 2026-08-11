import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { User, Bot as BotIcon, Plus, Play, X as XIcon } from 'lucide-react'
import PageStub from '@/components/PageStub'
import OpponentPickerModal, { type PickBot } from '@/components/OpponentPickerModal'
import {
  ExecutionRequestCard,
  type ExecutionRequestSnapshot,
} from '@/components/execution-queue'
import { useAuth } from '@/components/useAuth'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Label } from '@/components/ui/label'
import { Badge } from '@/components/ui/badge'
import { ErrorMsg, Loading } from '@/components/ui/status'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { cn } from '@/lib/utils'
import { ApiError, apiFetch, apiGet, apiJson, errMsg } from '@/api'
import { useConfirm } from '@/hooks/use-confirm'
import { useSingleFlightPolling } from '@/hooks/use-single-flight-polling'
import { GAMES, type GameId } from '@/lib/games'

/** 版本列表条目（公开视图：id+version+upload_note+created_at+size_bytes；owner 视图字段更多）。 */
interface VersionRow {
  id: number
  version: number
  upload_note?: string
  created_at?: string
  uploaded_at?: string
  size_bytes?: number
}

/** 一个座位的选中状态：bot + 选定版本 id（undefined=当前/激活版本）。 */
interface SeatState {
  bot: PickBot | null
  /** 选定版本的 bot_versions.id；undefined/null = 用当前激活版本。 */
  versionId: number | undefined
}

const EMPTY_SEAT: SeatState = { bot: null, versionId: undefined }
const EXECUTION_SESSION_PREFIX = 'bzplat.challenge.execution.'
const SUBMISSION_CONFIRMATION_MS = 12_000

/**
 * Generate an opaque, owner-scoped idempotency key before the POST leaves the
 * browser.  Eighteen random bytes encode to exactly 24 unpadded base64url
 * characters (144 bits), matching the public execution-request contract.
 */
function createExecutionRequestId(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(18))
  let binary = ''
  for (const byte of bytes) binary += String.fromCharCode(byte)
  return `req_${btoa(binary).replaceAll('+', '-').replaceAll('/', '_')}`
}

function isTerminal(snapshot: ExecutionRequestSnapshot | null): boolean {
  const status = snapshot?.request.status
  return status === 'completed' || status === 'cancelled' || status === 'interrupted'
}

/**
 * 合并后的挑战页：单一人/机对局，无模式切换。
 *
 * 两座位（显示从 1 起计；后端仍 0 起计）：
 * - 座位 1（先手 / 黑）：固定 Bot。
 * - 座位 2（后手 / 白）：Bot 或「我亲自上场」（人类固定坐此位）。
 * 提交按座位 2 类型走 /api/matches/challenge（bot vs bot）或
 * /api/matches/human（human_seat=1 固定，对应 0 起计后端座 1=后手/白）。
 */
export default function Challenge() {
  const { isLoggedIn, user } = useAuth()
  const nav = useNavigate()
  const [gameId, setGameId] = useState<GameId>('holdem')
  // 两座位（内部仍 0 起计以对齐后端；显示 +1）。
  const [seats, setSeats] = useState<[SeatState, SeatState]>([
    { ...EMPTY_SEAT },
    { ...EMPTY_SEAT },
  ])
  // 座位 2 的类型：'bot' 或 'human'（人类固定座位 2 = 后手/白）。
  const [seat2Kind, setSeat2Kind] = useState<'bot' | 'human'>('bot')
  // 弹窗：pickingSeat 标记当前为哪个座位挑 bot（'s1'|'s2'）。
  const [pickingSeat, setPickingSeat] = useState<'s1' | 's2' | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [execution, setExecution] = useState<ExecutionRequestSnapshot | null>(null)
  const [executionAction, setExecutionAction] = useState<'cancel' | 'retry' | null>(null)
  const [pendingPublicId, setPendingPublicId] = useState<string | null>(null)
  const [storageChecked, setStorageChecked] = useState(false)
  const [executionStale, setExecutionStale] = useState(false)
  const executionRevision = useRef(0)
  const navigatedMatch = useRef<string | null>(null)
  const pendingConfirmationUntil = useRef(0)
  const errorAlertRef = useRef<HTMLDivElement>(null)
  const [confirm, confirmDialog] = useConfirm()

  const executionStorageKey = user?.id == null
    ? null
    : `${EXECUTION_SESSION_PREFIX}${user.id}`

  const rememberExecution = useCallback((publicId: string) => {
    setPendingPublicId(publicId)
    if (!executionStorageKey) return
    try {
      // Only the opaque owner-scoped request id is kept, and only for this tab.
      sessionStorage.setItem(executionStorageKey, publicId)
    } catch {
      // Storage can be unavailable in hardened/private browser contexts.
    }
  }, [executionStorageKey])

  const forgetExecution = useCallback(() => {
    setPendingPublicId(null)
    if (!executionStorageKey) return
    try {
      sessionStorage.removeItem(executionStorageKey)
    } catch {
      // See rememberExecution: storage is an enhancement, not a hard dependency.
    }
  }, [executionStorageKey])

  const acceptExecution = useCallback((snapshot: ExecutionRequestSnapshot) => {
    pendingConfirmationUntil.current = 0
    setExecution(snapshot)
    rememberExecution(snapshot.public_id)
    setExecutionStale(false)
  }, [rememberExecution])

  useEffect(() => {
    executionRevision.current += 1
    navigatedMatch.current = null
    pendingConfirmationUntil.current = 0
    setExecution(null)
    setPendingPublicId(null)
    setExecutionStale(false)
    setStorageChecked(false)
    if (!isLoggedIn || !executionStorageKey) {
      setStorageChecked(true)
      return
    }
    try {
      const saved = sessionStorage.getItem(executionStorageKey)?.trim()
      if (saved) {
        // A reload can happen while the POST response is being lost. Give the
        // owner-scoped id a short visibility grace period before treating 404
        // as authoritative absence.
        pendingConfirmationUntil.current = Date.now() + SUBMISSION_CONFIRMATION_MS
        setPendingPublicId(saved)
      }
    } catch {
      // Fall through to a fresh form when sessionStorage is unavailable.
    } finally {
      setStorageChecked(true)
    }
  }, [executionStorageKey, isLoggedIn])

  useEffect(() => {
    if (!error) return
    const frame = requestAnimationFrame(() => errorAlertRef.current?.focus())
    return () => cancelAnimationFrame(frame)
  }, [error])

  const resetSeatsOnGameChange = useCallback(() => {
    setSeats([{ ...EMPTY_SEAT }, { ...EMPTY_SEAT }])
  }, [])

  // 版本选择器所需：缓存每个 bot id 的版本列表（弹窗选定 bot 后按需拉取）。
  // key = bot id；value = { rows, current, loading }
  const [versionCache, setVersionCache] = useState<
    Record<number, { rows: VersionRow[]; current: number | undefined; loading: boolean }>
  >({})

  const loadVersions = useCallback(async (botId: number) => {
    setVersionCache((c) =>
      c[botId] ? { ...c, [botId]: { ...c[botId], loading: true } } : { ...c, [botId]: { rows: [], current: undefined, loading: true } },
    )
    try {
      const d = await apiGet<{ versions: VersionRow[]; current_version: number }>(
        `/api/bots/${botId}/versions`,
      )
      setVersionCache((c) => ({
        ...c,
        [botId]: { rows: d.versions || [], current: d.current_version, loading: false },
      }))
    } catch {
      setVersionCache((c) => ({
        ...c,
        [botId]: { rows: [], current: undefined, loading: false },
      }))
    }
  }, [])

  // 选定某座位的 bot（来自弹窗）：写入 bot + 重置版本为「当前」+ 拉版本列表。
  const pickBotFor = (slot: 's1' | 's2', bot: PickBot) => {
    if (slot === 's1' && seat2Kind === 'bot' && bot.owner_id !== user?.id) {
      setError('Bot 对战的座位 1 只能使用自己的 Bot')
      setPickingSeat(null)
      return
    }
    const idx = slot === 's1' ? 0 : 1
    setSeats((s) => {
      const next: [SeatState, SeatState] = [s[0], s[1]]
      next[idx] = { bot, versionId: undefined }
      return next
    })
    setPickingSeat(null)
    if (!(slot === 's1' && seat2Kind === 'human')) void loadVersions(bot.id)
  }

  const clearSeat = (slot: 's1' | 's2') => {
    const idx = slot === 's1' ? 0 : 1
    setSeats((s) => {
      const next: [SeatState, SeatState] = [s[0], s[1]]
      next[idx] = { ...EMPTY_SEAT }
      return next
    })
  }

  const setSeatVersion = (slot: 's1' | 's2', vId: number | undefined) => {
    const idx = slot === 's1' ? 0 : 1
    setSeats((s) => {
      const next: [SeatState, SeatState] = [s[0], s[1]]
      next[idx] = { ...next[idx], versionId: vId }
      return next
    })
  }

  const chooseSeat2Kind = (kind: 'bot' | 'human') => {
    const seat1Bot = seats[0].bot
    setSeat2Kind(kind)
    if (kind === 'bot' && seat1Bot && seat1Bot.owner_id !== user?.id) {
      setError('已切换为 Bot 对战，请为座位 1 选择自己的 Bot')
    } else {
      setError('')
      // 人类模式不展示版本，首次选 Bot 时不会拉历史；切回 Bot 模式且
      // 保留的是自己的 Bot 时补拉，避免版本下拉只剩“当前版本”。
      if (kind === 'bot' && seat1Bot) void loadVersions(seat1Bot.id)
    }
    setSeats((s) => {
      const next: [SeatState, SeatState] = [s[0], s[1]]
      if (kind === 'human') {
        // 人类 API 固定使用 Bot 当前激活版本，清掉不会被提交的历史版本状态。
        next[0] = { ...next[0], versionId: undefined }
      } else if (next[0].bot && next[0].bot.owner_id !== user?.id) {
        // 人类模式允许挑战任意 Bot；切回 Bot-vs-Bot 后 my_bot_id 必须重新选自己的。
        next[0] = { ...EMPTY_SEAT }
      }
      return next
    })
  }

  // 自博弈：座位 2 = Bot 且两座同 bot id。
  const selfPlay =
    seat2Kind === 'bot' && seats[0].bot && seats[1].bot && seats[0].bot!.id === seats[1].bot!.id

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError('')
    try {
      if (seat2Kind === 'human') {
        // 人类对战：人类固定座位 2（后端 0 起计 = 座 1）。座位 1 = Bot。
        if (!seats[0].bot) throw new Error('请选择座位 1 的 Bot')
        const body: Record<string, unknown> = {
          bot_id: seats[0].bot.id,
          human_seat: 1, // 固定：人类 = 后端座 1 = 后手/白
          game_id: gameId,
        }
        const requestId = createExecutionRequestId()
        body.request_id = requestId
        pendingConfirmationUntil.current = Date.now() + SUBMISSION_CONFIRMATION_MS
        // Persist before the network call. If the server commits but its 202
        // response is lost, the existing polling path can recover by this id.
        rememberExecution(requestId)
        // 注：HumanChallengeBody 不接受 bot_version_id，故座位 1 选版本时人类对战忽略版本。
        const d = await apiJson<ExecutionRequestSnapshot>('/api/matches/human', 'POST', body)
        if (d.public_id !== requestId) throw new Error('服务端返回的执行请求编号不一致')
        acceptExecution(d)
        return
      }
      // bot vs bot
      if (!seats[0].bot || !seats[1].bot) throw new Error('请为两个座位各选择一个 Bot')
      const body: Record<string, unknown> = {
        my_bot_id: seats[0].bot.id,
        opponent_bot_id: seats[1].bot.id,
        game_id: gameId,
      }
      if (seats[0].versionId !== undefined) body.my_bot_version_id = seats[0].versionId
      if (seats[1].versionId !== undefined) body.opponent_bot_version_id = seats[1].versionId
      const requestId = createExecutionRequestId()
      body.request_id = requestId
      pendingConfirmationUntil.current = Date.now() + SUBMISSION_CONFIRMATION_MS
      rememberExecution(requestId)
      const d = await apiJson<ExecutionRequestSnapshot>('/api/matches/challenge', 'POST', body)
      if (d.public_id !== requestId) throw new Error('服务端返回的执行请求编号不一致')
      acceptExecution(d)
    } catch (err) {
      // A deterministic client/business rejection cannot have committed this
      // request. Transport failures and 5xx responses are ambiguous, so retain
      // the id and let the owner-scoped GET recover the durable job.
      if (err instanceof ApiError && err.status >= 400 && err.status < 500) {
        pendingConfirmationUntil.current = 0
        forgetExecution()
      }
      setError(errMsg(err, '发起挑战失败'))
    } finally {
      setBusy(false)
    }
  }

  const executionPublicId = execution?.public_id || pendingPublicId
  const pollExecution = useCallback(async (signal: AbortSignal) => {
    if (!executionPublicId) return
    const revision = executionRevision.current
    const next = await apiFetch<ExecutionRequestSnapshot>(
      `/api/execution-requests/${encodeURIComponent(executionPublicId)}`,
      { method: 'GET', signal },
    )
    if (signal.aborted || revision !== executionRevision.current) return
    acceptExecution(next)
  }, [acceptExecution, executionPublicId])

  const {
    refresh: refreshExecution,
    polling: executionPolling,
    offline: executionOffline,
  } = useSingleFlightPolling({
    task: pollExecution,
    enabled: storageChecked && !!executionPublicId && !isTerminal(execution) && executionAction === null,
    intervalMs: 1_500,
    maxIntervalMs: 12_000,
    onSuccess: () => {
      setError('')
      setExecutionStale(false)
    },
    onError: (err) => {
      if (
        err instanceof ApiError
        && err.status === 404
        && Date.now() < pendingConfirmationUntil.current
      ) {
        setExecutionStale(false)
        setError('请求已发出，正在确认受理状态；系统会继续查询同一请求号。')
        return
      }
      if (err instanceof ApiError && (err.status === 403 || err.status === 404)) {
        executionRevision.current += 1
        forgetExecution()
        setExecution(null)
        setExecutionStale(false)
        setError('这条执行请求已失效或不属于当前账号，请重新发起。')
        return
      }
      setExecutionStale(!!execution)
      setError(errMsg(err, '执行请求状态更新失败'))
    },
  })

  useEffect(() => {
    const matchId = execution?.request.match_id
    if (!matchId) return
    const { request } = execution
    if (request.cancel_requested || request.status === 'cancelled' || request.status === 'interrupted') return
    if (navigatedMatch.current === matchId) return
    navigatedMatch.current = matchId
    executionRevision.current += 1
    forgetExecution()
    nav(
      request.source === 'human' ? `/play/${matchId}` : `/match/${matchId}`,
      { replace: true },
    )
  }, [execution, forgetExecution, nav])

  const cancelExecution = async () => {
    if (!execution || executionAction || execution.request.cancel_requested) return
    if (!await confirm({
      title: execution.request.status === 'queued' ? '取消排队' : '取消对局',
      desc: '平台会先清理并确认该任务的沙箱容器为零，然后才释放容量。',
      confirmText: '确认取消',
      danger: true,
    })) return
    executionRevision.current += 1
    setExecutionAction('cancel')
    setError('')
    try {
      const next = await apiJson<ExecutionRequestSnapshot>(
        `/api/execution-requests/${encodeURIComponent(execution.public_id)}`,
        'DELETE',
      )
      acceptExecution(next)
    } catch (err) {
      setError(errMsg(err, '取消执行请求失败'))
    } finally {
      setExecutionAction(null)
    }
  }

  const retryExecution = async () => {
    if (!execution || executionAction || !execution.request.retryable) return
    executionRevision.current += 1
    setExecutionAction('retry')
    setError('')
    try {
      const next = await apiJson<ExecutionRequestSnapshot>(
        `/api/execution-requests/${encodeURIComponent(execution.public_id)}/retry`,
        'POST',
      )
      acceptExecution(next)
    } catch (err) {
      setError(errMsg(err, '重新排队失败'))
    } finally {
      setExecutionAction(null)
    }
  }

  const resetExecution = () => {
    executionRevision.current += 1
    pendingConfirmationUntil.current = 0
    forgetExecution()
    setExecution(null)
    setExecutionAction(null)
    setExecutionStale(false)
    setError('')
  }

  if (!isLoggedIn) {
    return (
      <PageStub title="发起挑战" subtitle="选择游戏与座位 Bot（支持自博弈、人类对战、指定历史版本）">
        <p className="mx-auto max-w-md rounded-lg border border-border bg-card px-4 py-3 text-center text-sm text-muted-foreground">
          请先{' '}
          <Link to="/login" className="font-medium text-primary hover:underline">
            登录
          </Link>{' '}
          后选择双方 Bot 发起挑战。
        </p>
      </PageStub>
    )
  }

  // bot 座位渲染（座位 1 与座位 2-bot 共用）。slot='s1'|'s2'；座位号显示 +1。
  const renderBotSeat = (slot: 's1' | 's2') => {
    const idx = slot === 's1' ? 0 : 1
    const seat = seats[idx]
    const seatLabel = slot === 's1' ? '座位 1（先手 / 黑）' : '座位 2（后手 / 白）'
    const vc = seat.bot ? versionCache[seat.bot.id] : undefined
    const mineOnly = slot === 's1' && seat2Kind === 'bot'
    const versionsEnabled = !(slot === 's1' && seat2Kind === 'human')
    return (
      <div className="space-y-2">
        <div className="flex items-center justify-between">
          <Label>{seatLabel}</Label>
          {seat.bot && (
            <button
              type="button"
              onClick={() => clearSeat(slot)}
              className="inline-flex min-h-11 items-center gap-1 px-2 text-xs text-muted-foreground hover:text-destructive"
            >
              <XIcon className="size-3" /> 清除
            </button>
          )}
        </div>
        <button
          type="button"
          onClick={() => setPickingSeat(slot)}
          className="flex min-h-11 w-full min-w-0 items-center gap-2 rounded-lg border border-dashed border-input px-3 py-2.5 text-left text-sm text-muted-foreground hover:bg-accent"
        >
          {seat.bot ? (
            <span className="flex min-w-0 flex-wrap items-center gap-2 text-foreground">
              <BotIcon className="size-4 shrink-0 text-primary" />
              <strong className="max-w-full break-words [overflow-wrap:anywhere]">{seat.bot.display_name || seat.bot.name}</strong>
              <span className="max-w-full break-words text-xs text-muted-foreground [overflow-wrap:anywhere]">
                {seat.bot.owner_display || seat.bot.owner_name || '所属用户不可用'}
                {seat.bot.owner_id != null && seat.bot.owner_id === user?.id ? '（我的）' : ''}
              </span>
            </span>
          ) : (
            <>
              <Plus className="size-4" />
              {mineOnly ? '选择我的 Bot' : '选择 Bot（搜索 / 我的 / 按用户）'}
            </>
          )}
        </button>

        {/* 版本选择：bot 选定后展示。
            「当前/激活版本」用 'current' 哨兵而非空串——Radix Select 把 value=""
            当作未选中/占位状态，空串会导致选中后触发器仍显示 placeholder
            而非「当前版本 (vN)」（审计 P1-C）。与项目 Select 规范一致（空值用非空哨兵）。 */}
        {seat.bot && versionsEnabled && (
          <Select
            value={seat.versionId === undefined ? 'current' : String(seat.versionId)}
            onValueChange={(v) => setSeatVersion(slot, v === 'current' ? undefined : Number(v))}
          >
            <SelectTrigger className="h-11 w-full">
              <SelectValue placeholder="选择版本" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="current">
                {vc?.current !== undefined ? `当前版本 (v${vc.current})` : '当前版本'}
              </SelectItem>
              {(vc?.rows || []).map((vr) => {
                const isCurrent = vc?.current !== undefined && vr.version === vc.current
                return (
                  <SelectItem key={vr.id} value={String(vr.id)}>
                    v{vr.version}
                    {vr.upload_note ? ` ${vr.upload_note}` : ''}
                    {isCurrent ? ' · 当前' : ''}
                  </SelectItem>
                )
              })}
            </SelectContent>
          </Select>
        )}
        {seat.bot && !versionsEnabled && (
          <p className="text-xs text-muted-foreground">人类对战使用该 Bot 的当前激活版本</p>
        )}
      </div>
    )
  }

  const ready = seat2Kind === 'human' ? !!seats[0].bot : !!seats[0].bot && !!seats[1].bot

  return (
    <PageStub title="发起挑战" subtitle="座位 1 固定 Bot；座位 2 可选 Bot 或亲自上场（人类不计天梯）">
      {!storageChecked || (pendingPublicId && !execution) ? (
        <Card
          className="mx-auto max-w-2xl gap-3 p-4"
          aria-busy={executionPolling}
          data-testid="execution-request-recovery"
        >
          <div role="status" aria-live="polite">
            <h2 className="text-sm font-semibold">正在恢复上次执行请求</h2>
            <p className="mt-1 text-xs text-muted-foreground">
              刷新页面不会丢失排队任务；恢复后仍可查看进度、取消或重试。
            </p>
          </div>
          {executionPolling && <Loading text="正在读取请求状态…" className="py-4" />}
          {(error || executionOffline) && (
            <div ref={errorAlertRef} className="space-y-2" role="alert" tabIndex={-1}>
              <ErrorMsg msg={executionOffline ? '当前离线；请求仍保留，联网后会自动恢复。' : error} />
              <div className="flex flex-wrap gap-2">
                <Button type="button" variant="outline" className="min-h-11" onClick={refreshExecution}>
                  立即重试
                </Button>
                <Button type="button" variant="ghost" className="min-h-11" onClick={resetExecution}>
                  放弃恢复并返回表单
                </Button>
              </div>
            </div>
          )}
        </Card>
      ) : execution ? (
        <div className="space-y-3">
          {(error || executionOffline) && (
            <div
              ref={errorAlertRef}
              className="flex flex-wrap items-center justify-between gap-2"
              role="alert"
              tabIndex={-1}
            >
              <div>
                <ErrorMsg msg={executionOffline ? '当前离线；以下保留上次状态，联网后会自动续查。' : error} />
                {executionStale && <p className="mt-1 text-xs text-muted-foreground">队列位置可能已变化。</p>}
              </div>
              <Button type="button" variant="outline" className="min-h-11" onClick={refreshExecution}>
                立即重试
              </Button>
            </div>
          )}
          <ExecutionRequestCard
            snapshot={execution}
            busy={executionAction !== null}
            busyAction={executionAction}
            onCancel={() => { void cancelExecution() }}
            onRetry={() => { void retryExecution() }}
            onReset={resetExecution}
          />
        </div>
      ) : (
      <form onSubmit={(e) => void onSubmit(e)} className="mx-auto max-w-2xl">
        <Card>
          <CardContent className="space-y-4">
            {/* 游戏筛选：切换时重置两座位（不同游戏的 bot 不互通） */}
            <div className="space-y-1.5">
              <Label>游戏</Label>
              <Select
                value={gameId}
                onValueChange={(v) => {
                  setGameId(v as GameId)
                  resetSeatsOnGameChange()
                }}
              >
                <SelectTrigger className="mt-1.5 h-11 w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {GAMES.map((g) => (
                    <SelectItem key={g.id} value={g.id}>{g.label}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <div className="rounded-lg border border-border p-3">
              {selfPlay && (
                <Badge variant="secondary" className="mb-3 gap-1">
                  <BotIcon className="size-3" />
                  自博弈
                </Badge>
              )}
              <div className="grid gap-3 sm:grid-cols-2">
                {/* 座位 1：固定 Bot */}
                {renderBotSeat('s1')}

                {/* 座位 2：Bot 或 人类（小开关，仅此座位有） */}
                <div className="space-y-2">
                  <div className="flex items-center justify-between">
                    <Label>座位 2（后手 / 白）</Label>
                    <div className="inline-flex rounded-lg border border-input p-0.5 text-xs" role="group" aria-label="座位 2 玩家类型">
                      <button
                        type="button"
                        onClick={() => chooseSeat2Kind('bot')}
                        aria-pressed={seat2Kind === 'bot'}
                        className={cn(
                          'inline-flex min-h-11 items-center gap-1 rounded-md px-2 py-1',
                          seat2Kind === 'bot'
                            ? 'bg-primary/10 text-primary'
                            : 'text-muted-foreground hover:bg-accent',
                        )}
                      >
                        <BotIcon className="size-3" />
                        选 Bot
                      </button>
                      <button
                        type="button"
                        onClick={() => chooseSeat2Kind('human')}
                        aria-pressed={seat2Kind === 'human'}
                        className={cn(
                          'inline-flex min-h-11 items-center gap-1 rounded-md px-2 py-1',
                          seat2Kind === 'human'
                            ? 'bg-primary/10 text-primary'
                            : 'text-muted-foreground hover:bg-accent',
                        )}
                      >
                        <User className="size-3" />
                        我亲自上场
                      </button>
                    </div>
                  </div>

                  {seat2Kind === 'bot' ? (
                    <div className="space-y-2">
                      {seats[1].bot && (
                        <button
                          type="button"
                          onClick={() => clearSeat('s2')}
                          className="inline-flex min-h-11 items-center gap-1 px-2 text-xs text-muted-foreground hover:text-destructive"
                        >
                          <XIcon className="size-3" /> 清除
                        </button>
                      )}
                      <button
                        type="button"
                        onClick={() => setPickingSeat('s2')}
                        className="flex min-h-11 w-full min-w-0 items-center gap-2 rounded-lg border border-dashed border-input px-3 py-2.5 text-left text-sm text-muted-foreground hover:bg-accent"
                      >
                        {seats[1].bot ? (
                          <span className="flex min-w-0 flex-wrap items-center gap-2 text-foreground">
                            <BotIcon className="size-4 shrink-0 text-primary" />
                            <strong className="max-w-full break-words [overflow-wrap:anywhere]">{seats[1].bot.display_name || seats[1].bot.name}</strong>
                            <span className="max-w-full break-words text-xs text-muted-foreground [overflow-wrap:anywhere]">
                              {seats[1].bot.owner_display || seats[1].bot.owner_name || '所属用户不可用'}
                              {seats[1].bot.owner_id != null && seats[1].bot.owner_id === user?.id ? '（我的）' : ''}
                            </span>
                          </span>
                        ) : (
                          <>
                            <Plus className="size-4" />
                            选择 Bot（搜索 / 我的 / 按用户）
                          </>
                        )}
                      </button>
                      {seats[1].bot && (
                        (() => {
                          const vc = versionCache[seats[1].bot!.id]
                          return (
                            <Select
                              value={seats[1].versionId === undefined ? 'current' : String(seats[1].versionId)}
                              onValueChange={(v) => setSeatVersion('s2', v === 'current' ? undefined : Number(v))}
                            >
                              <SelectTrigger className="h-11 w-full">
                                <SelectValue placeholder="选择版本" />
                              </SelectTrigger>
                              <SelectContent>
                                <SelectItem value="current">
                                  {vc?.current !== undefined ? `当前版本 (v${vc.current})` : '当前版本'}
                                </SelectItem>
                                {(vc?.rows || []).map((vr) => {
                                  const isCurrent = vc?.current !== undefined && vr.version === vc.current
                                  return (
                                    <SelectItem key={vr.id} value={String(vr.id)}>
                                      v{vr.version}
                                      {vr.upload_note ? ` ${vr.upload_note}` : ''}
                                      {isCurrent ? ' · 当前' : ''}
                                    </SelectItem>
                                  )
                                })}
                              </SelectContent>
                            </Select>
                          )
                        })()
                      )}
                    </div>
                  ) : (
                    <div className="rounded-lg border border-dashed border-input px-3 py-3 text-sm text-muted-foreground">
                      你（<strong className="break-words text-foreground [overflow-wrap:anywhere]">@{user?.username}</strong>）作为人类玩家，不计天梯。
                    </div>
                  )}
                </div>
              </div>

              <p className="mt-3 text-xs text-muted-foreground">
                {seat2Kind === 'human'
                  ? '座位 1 选 Bot，座位 2 由你亲自上场。人类对战占 1 个沙箱单位、不计天梯。'
                  : '两个座位可选同一个 Bot（自博弈），亦可各自指定历史版本对比。版本缺省=当前激活版本。'}
              </p>
            </div>

            {error && (
              <div ref={errorAlertRef} role="alert" tabIndex={-1}>
                <ErrorMsg msg={error} />
              </div>
            )}
            <Button
              type="submit"
              disabled={busy || !ready}
              className="min-h-11 w-full gap-1.5"
            >
              <Play className="size-4" />
              {busy ? '发起中…' : seat2Kind === 'human' ? '开始人类对战' : '开始对局'}
            </Button>
            {!busy && !ready && (
              <p className="text-center text-xs text-muted-foreground">
                {seat2Kind === 'human'
                  ? '请选择座位 1 的 Bot'
                  : '请为两个座位各选择一个 Bot'}
              </p>
            )}
          </CardContent>
        </Card>
      </form>
      )}

      {!execution && pickingSeat !== null && (
        <OpponentPickerModal
          gameId={gameId}
          myUserId={user?.id}
          mineOnly={pickingSeat === 's1' && seat2Kind === 'bot'}
          onClose={() => setPickingSeat(null)}
          onPick={(b) => pickBotFor(pickingSeat, b)}
        />
      )}
      {confirmDialog}
    </PageStub>
  )
}
