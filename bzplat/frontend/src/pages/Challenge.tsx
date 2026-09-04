import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { ArrowLeftRight, User, Bot as BotIcon, Laptop, Plus, Play, Trophy, X as XIcon } from 'lucide-react'
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
import {
  localAgentBotName,
  localAgentStatus,
  type ExecutionEnvironment,
  type LocalAIAgent,
} from '@/components/runtime-environment'
import {
  parseTimeControlRegistries,
  timeControlDescription,
  timeControlLabel,
  type TimeControlRegistry,
} from '@/lib/time-controls'

/** 版本列表条目（公开视图：id+version+upload_note+created_at+size_bytes；owner 视图字段更多）。 */
interface VersionRow {
  id: number
  version: number
  upload_note?: string
  created_at?: string
  uploaded_at?: string
  size_bytes?: number
  runnable?: boolean
}

/** 一个座位的选中状态：bot + 选定版本 id（undefined=当前/激活版本）。 */
interface SeatState {
  bot: PickBot | null
  /** 选定版本的 bot_versions.id；undefined/null = 用当前激活版本。 */
  versionId: number | undefined
  environment: Extract<ExecutionEnvironment, 'platform_low' | 'remote_local'>
  localAgentId: string | null
}

const EMPTY_SEAT: SeatState = {
  bot: null,
  versionId: undefined,
  environment: 'platform_low',
  localAgentId: null,
}
const PLAYER_LABELS: Record<GameId, readonly [string, string]> = {
  holdem: ['玩家 1', '玩家 2'],
  // 竞赛五子棋在第三子后允许交换棋色，座位不能提前标成黑/白方。
  gomoku: ['开局提案方', '交换决策方'],
  pencil: ['红方', '蓝方'],
}
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
 * 两个内部座位仍按 0/1 存储，展示名称按游戏切换：德州玩家 1/2、
 * 五子棋开局提案方/交换决策方、点格棋红/蓝方。第二方可改为「我亲自上场」。
 * 提交按第二方类型走 /api/matches/challenge（bot vs bot）或
 * /api/matches/human（human_seat=1 固定）。
 */
export default function Challenge() {
  const { isLoggedIn, user } = useAuth()
  const nav = useNavigate()
  const [gameId, setGameId] = useState<GameId>('holdem')
  const [timeControlRegistries, setTimeControlRegistries] = useState<Record<string, TimeControlRegistry>>({})
  const [timeControlId, setTimeControlId] = useState('')
  const [timeControlsLoading, setTimeControlsLoading] = useState(true)
  const [timeControlsError, setTimeControlsError] = useState('')
  const playerLabels = PLAYER_LABELS[gameId]
  // 两个位置内部仍 0 起计以对齐后端；五子棋显示角色而不预判交换后的棋色。
  const [seats, setSeats] = useState<[SeatState, SeatState]>([
    { ...EMPTY_SEAT },
    { ...EMPTY_SEAT },
  ])
  // Bot-vs-Bot 中“我的 Bot”可映射到任一物理座位；切换时完整交换双方配置。
  const [mySeat, setMySeat] = useState<0 | 1>(0)
  // 第二方类型：'bot' 或 'human'（人类固定使用内部位置 1）。
  const [seat2Kind, setSeat2Kind] = useState<'bot' | 'human'>('bot')
  // 弹窗：pickingSeat 标记当前为哪个座位挑 bot（'s1'|'s2'）。
  const [pickingSeat, setPickingSeat] = useState<'s1' | 's2' | null>(null)
  const [localAgents, setLocalAgents] = useState<LocalAIAgent[]>([])
  const [agentsLoading, setAgentsLoading] = useState(false)
  const [agentError, setAgentError] = useState('')
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

  useEffect(() => {
    if (!isLoggedIn) {
      setTimeControlRegistries({})
      setTimeControlId('')
      setTimeControlsError('')
      setTimeControlsLoading(false)
      return
    }
    const controller = new AbortController()
    setTimeControlsLoading(true)
    setTimeControlsError('')
    // React StrictMode 的探测性首轮 effect 会立即 cleanup；延后一拍启动，
    // 避免发出随后必然取消、会污染浏览器网络验收的无意义请求。
    const startTimer = window.setTimeout(() => {
      void apiFetch<unknown>('/api/games', { signal: controller.signal })
        .then((payload) => {
          if (controller.signal.aborted) return
          const registries = parseTimeControlRegistries(payload)
          if (!registries) throw new Error('游戏时限配置格式无效')
          setTimeControlRegistries(Object.fromEntries(registries.map((registry) => [registry.game_id, registry])))
        })
        .catch((err: unknown) => {
          if (controller.signal.aborted) return
          setTimeControlRegistries({})
          setTimeControlsError(errMsg(err, '游戏时限加载失败'))
        })
        .finally(() => {
          if (!controller.signal.aborted) setTimeControlsLoading(false)
        })
    }, 0)
    return () => {
      window.clearTimeout(startTimer)
      controller.abort()
    }
  }, [isLoggedIn])

  const timeControlRegistry = timeControlRegistries[gameId]
  const selectedTimeControl = timeControlRegistry?.time_controls.find((control) => control.id === timeControlId)
  const timeControlReady = selectedTimeControl !== undefined
  const alternateTimeControl = Boolean(
    selectedTimeControl && selectedTimeControl.id !== timeControlRegistry?.default_time_control_id,
  )

  useEffect(() => {
    setTimeControlId((current) => (
      timeControlRegistry?.time_controls.some((control) => control.id === current)
        ? current
        : timeControlRegistry?.default_time_control_id || ''
    ))
  }, [gameId, timeControlRegistry])

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

  const loadLocalAgents = useCallback(async () => {
    if (!isLoggedIn) return
    setAgentsLoading(true)
    try {
      const data = await apiGet<{ items: LocalAIAgent[] }>('/api/local-ai/agents')
      setLocalAgents((data.items || []).filter((agent) => agent.status !== 'revoked'))
      setAgentError('')
    } catch (err) {
      setAgentError(errMsg(err, '本地 Bot 连接状态加载失败'))
    } finally {
      setAgentsLoading(false)
    }
  }, [isLoggedIn])

  useEffect(() => {
    if (!isLoggedIn) return
    void loadLocalAgents()
    const timer = window.setInterval(() => void loadLocalAgents(), 5_000)
    return () => window.clearInterval(timer)
  }, [isLoggedIn, loadLocalAgents])

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
        [botId]: {
          rows: (d.versions || []).filter((version) => version.runnable !== false),
          current: d.current_version,
          loading: false,
        },
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
    const idx = slot === 's1' ? 0 : 1
    if (
      idx === mySeat
      && seat2Kind === 'bot'
      && user?.role !== 'admin'
      && bot.owner_id !== user?.id
    ) {
      setError(`Bot 对战时，${playerLabels[mySeat]}只能使用自己的 Bot`)
      setPickingSeat(null)
      return
    }
    setSeats((s) => {
      const next: [SeatState, SeatState] = [s[0], s[1]]
      next[idx] = {
        bot,
        versionId: undefined,
        environment: 'platform_low',
        localAgentId: null,
      }
      return next
    })
    setPickingSeat(null)
    if (!(slot === 's1' && seat2Kind === 'human')) void loadVersions(bot.id)
  }

  const clearSeat = (slot: 's1' | 's2') => {
    const idx = slot === 's1' ? 0 : 1
    setSeats((s) => {
      const next: [SeatState, SeatState] = [s[0], s[1]]
      next[idx] = {
        ...EMPTY_SEAT,
        environment: s[idx].environment,
      }
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

  const setSeatEnvironment = (
    slot: 's1' | 's2',
    environment: SeatState['environment'],
  ) => {
    const idx = slot === 's1' ? 0 : 1
    setSeats((current) => {
      const next: [SeatState, SeatState] = [current[0], current[1]]
      next[idx] = {
        ...EMPTY_SEAT,
        environment,
      }
      return next
    })
    setError('')
  }

  const setSeatLocalAgent = (slot: 's1' | 's2', publicId: string) => {
    const idx = slot === 's1' ? 0 : 1
    setSeats((current) => {
      const next: [SeatState, SeatState] = [current[0], current[1]]
      next[idx] = {
        ...current[idx],
        bot: null,
        versionId: undefined,
        environment: 'remote_local',
        localAgentId: publicId,
      }
      return next
    })
  }

  const chooseSeat2Kind = (kind: 'bot' | 'human') => {
    const seatOneBot = seats[0].bot
    const myBot = seats[mySeat].bot
    setSeat2Kind(kind)
    if (
      kind === 'bot'
      && user?.role !== 'admin'
      && myBot
      && myBot.owner_id !== user?.id
    ) {
      setError(`已切换为 Bot 对战，请为${playerLabels[mySeat]}选择自己的 Bot`)
    } else {
      setError('')
      // 人类模式不展示版本，首次选 Bot 时不会拉历史；切回 Bot 模式且
      // 保留的是自己的 Bot 时补拉，避免版本下拉只剩“当前版本”。
      if (kind === 'bot' && seatOneBot) void loadVersions(seatOneBot.id)
    }
    setSeats((s) => {
      const next: [SeatState, SeatState] = [s[0], s[1]]
      if (kind === 'human') {
        // 人类 API 固定使用 Bot 当前激活版本，清掉不会被提交的历史版本状态。
        next[0] = next[0].environment === 'remote_local'
          ? { ...EMPTY_SEAT }
          : { ...next[0], versionId: undefined }
        next[1] = { ...EMPTY_SEAT }
      } else if (
        user?.role !== 'admin'
        && next[mySeat].bot
        && next[mySeat].bot.owner_id !== user?.id
      ) {
        // 人类模式允许挑战任意 Bot；切回 Bot-vs-Bot 后“我的位置”必须重新选自己的。
        next[mySeat] = { ...EMPTY_SEAT }
      }
      return next
    })
  }

  const chooseMySeat = (nextSeat: 0 | 1) => {
    if (seat2Kind !== 'bot' || nextSeat === mySeat) return
    // Bot、版本、运行环境和本地连接必须作为一个整体移动，避免切换位置后
    // 版本或 agent 静默绑定到另一方。
    setSeats((current) => [current[1], current[0]])
    setMySeat(nextSeat)
    setError('')
  }

  // 自博弈：第二方 = Bot 且双方使用同一 bot id。
  const selectedLocalAgents = seats.map((seat) => (
    seat.localAgentId
      ? localAgents.find((agent) => agent.public_id === seat.localAgentId) || null
      : null
  )) as [LocalAIAgent | null, LocalAIAgent | null]
  const selectedBotIds = seats.map((seat, index) => (
    seat.environment === 'remote_local' ? selectedLocalAgents[index]?.bot_id ?? null : seat.bot?.id ?? null
  )) as [number | null, number | null]
  const selfPlay = seat2Kind === 'bot'
    && selectedBotIds[0] != null
    && selectedBotIds[0] === selectedBotIds[1]
  const usesLocalBot = seat2Kind === 'bot' && seats.some((seat) => seat.environment === 'remote_local')

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError('')
    try {
      if (!timeControlReady) throw new Error('当前游戏的对局时限不可用')
      if (seat2Kind === 'human') {
        // 人类固定使用内部座位 1，平台 Bot 使用内部座位 0。
        if (!seats[0].bot) throw new Error(`请选择${playerLabels[0]}的 Bot`)
        const body: Record<string, unknown> = {
          bot_id: seats[0].bot.id,
          human_seat: 1,
          game_id: gameId,
          time_control_id: timeControlId,
        }
        const requestId = createExecutionRequestId()
        body.request_id = requestId
        pendingConfirmationUntil.current = Date.now() + SUBMISSION_CONFIRMATION_MS
        // Persist before the network call. If the server commits but its 202
        // response is lost, the existing polling path can recover by this id.
        rememberExecution(requestId)
        // HumanChallengeBody 不接受 bot_version_id，故第一方选版本时人类对战忽略版本。
        const d = await apiJson<ExecutionRequestSnapshot>('/api/matches/human', 'POST', body)
        if (d.public_id !== requestId) throw new Error('服务端返回的执行请求编号不一致')
        acceptExecution(d)
        return
      }
      // bot vs bot
      if (selectedBotIds[0] == null || selectedBotIds[1] == null) {
        throw new Error('请为双方选择可用的 Bot')
      }
      for (const index of [0, 1] as const) {
        if (seats[index].environment !== 'remote_local') continue
        const agent = selectedLocalAgents[index]
        if (!agent) throw new Error(`请选择${playerLabels[index]}的本地 Bot 连接`)
        if (!localAgentStatus(agent).available) {
          throw new Error(`${agent.label} 当前不可用，请先在你的电脑上启动连接程序`)
        }
      }
      if (
        selectedLocalAgents[0]
        && selectedLocalAgents[0]?.public_id === selectedLocalAgents[1]?.public_id
      ) {
        throw new Error('同一个本地连接不能同时控制双方，请分别启动两个连接')
      }
      const body: Record<string, unknown> = {
        my_bot_id: selectedBotIds[mySeat],
        opponent_bot_id: selectedBotIds[mySeat === 0 ? 1 : 0],
        my_seat: mySeat,
        game_id: gameId,
        time_control_id: timeControlId,
        my_environment: seats[mySeat].environment,
        opponent_environment: seats[mySeat === 0 ? 1 : 0].environment,
        my_local_agent_id: selectedLocalAgents[mySeat]?.public_id ?? null,
        opponent_local_agent_id: selectedLocalAgents[mySeat === 0 ? 1 : 0]?.public_id ?? null,
      }
      const opponentSeat = mySeat === 0 ? 1 : 0
      if (seats[mySeat].environment === 'platform_low' && seats[mySeat].versionId !== undefined) {
        body.my_bot_version_id = seats[mySeat].versionId
      }
      if (seats[opponentSeat].environment === 'platform_low' && seats[opponentSeat].versionId !== undefined) {
        body.opponent_bot_version_id = seats[opponentSeat].versionId
      }
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
    pendingConfirmationUntil.current = 0
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
      desc: '平台会安全停止当前任务，然后释放运行位。',
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
      <PageStub title="发起挑战" subtitle="选择游戏和双方 Bot（支持自博弈、人类对战、指定历史版本）">
        <p className="mx-auto max-w-md rounded-lg border border-border bg-card px-4 py-3 text-center text-sm text-muted-foreground">
          请先{' '}
          <Link
            to="/login"
            className="inline-flex min-h-11 min-w-11 items-center justify-center px-1 font-medium text-primary hover:underline sm:min-h-0 sm:min-w-0 sm:px-0"
          >
            登录
          </Link>{' '}
          后选择双方 Bot 发起挑战。
        </p>
      </PageStub>
    )
  }

  // 两个内部座位共用选择器；标题由当前游戏决定。
  const renderBotSeat = (slot: 's1' | 's2', showLabel = true) => {
    const idx = slot === 's1' ? 0 : 1
    const seat = seats[idx]
    const seatLabel = playerLabels[idx]
    const vc = seat.bot ? versionCache[seat.bot.id] : undefined
    const localAgent = selectedLocalAgents[idx]
    const gameAgents = localAgents.filter((agent) => agent.game_id === gameId)
    const mineOnly = idx === mySeat && seat2Kind === 'bot' && user?.role !== 'admin'
    const versionsEnabled = !(slot === 's1' && seat2Kind === 'human')
    return (
      <div className="space-y-2" data-testid={`challenge-seat-${idx}`}>
        {showLabel && (
          <div className="flex items-center justify-between">
            <div className="flex flex-wrap items-center gap-2">
              <Label>{seatLabel}</Label>
              {seat2Kind === 'bot' && idx === mySeat && (
                <Badge variant="secondary" className="text-[10px]">
                  {user?.role === 'admin' ? '发起方 Bot' : '我的 Bot'}
                </Badge>
              )}
            </div>
            {(seat.bot || seat.localAgentId) && (
              <button
                type="button"
                onClick={() => clearSeat(slot)}
                className="inline-flex min-h-[var(--control-height)] items-center gap-1 px-2 text-xs text-muted-foreground hover:text-destructive max-sm:min-h-11"
              >
                <XIcon className="size-3" /> 清除
              </button>
            )}
          </div>
        )}
        <div className="space-y-1">
          <Label className="text-xs text-muted-foreground">运行位置</Label>
          <Select
            value={seat.environment}
            onValueChange={(value) => {
              if (value === 'platform_low' || value === 'remote_local') {
                setSeatEnvironment(slot, value)
              }
            }}
            disabled={seat2Kind === 'human'}
          >
            <SelectTrigger className="w-full" aria-label={`${seatLabel}运行位置`}>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="platform_low">节能沙箱</SelectItem>
              {seat2Kind === 'bot' && <SelectItem value="remote_local">本地 Bot（我的电脑）</SelectItem>}
            </SelectContent>
          </Select>
        </div>

        {seat.environment === 'platform_low' ? (
          <>
          {!showLabel && seat.bot && (
            <button
              type="button"
              onClick={() => clearSeat(slot)}
              className="inline-flex min-h-[var(--control-height)] items-center gap-1 px-2 text-xs text-muted-foreground hover:text-destructive max-sm:min-h-11"
            >
              <XIcon className="size-3" /> 清除
            </button>
          )}
        <button
          type="button"
          onClick={() => setPickingSeat(slot)}
          className="flex min-h-[var(--control-height)] w-full min-w-0 items-center gap-2 rounded-lg border border-dashed border-input px-3 py-2 text-left text-sm text-muted-foreground hover:bg-accent max-sm:min-h-11"
        >
          {seat.bot ? (
            <span className="flex min-w-0 flex-wrap items-center gap-2 text-foreground">
              <BotIcon className="size-4 shrink-0 text-primary" />
              <strong className="max-w-full break-words [overflow-wrap:anywhere]">{seat.bot.display_name || seat.bot.name}</strong>
              <Badge variant={seat.bot.is_ranked ? 'default' : 'outline'} className="text-[10px]">
                <Trophy className="size-3" aria-hidden="true" />
                {seat.bot.is_ranked ? '排行榜 Bot' : '练习 Bot'}
              </Badge>
              <span className="max-w-full break-words text-xs text-muted-foreground [overflow-wrap:anywhere]">
                {seat.bot.owner_display || seat.bot.owner_name || '所属用户不可用'}
                {seat.bot.owner_id != null && seat.bot.owner_id === user?.id ? '（我的）' : ''}
              </span>
            </span>
          ) : (
            <>
              <Plus className="size-4" />
              {mineOnly
                ? '选择我的 Bot'
                : seat2Kind === 'bot' && idx === mySeat && user?.role === 'admin'
                  ? '选择发起方 Bot（全站可用）'
                  : '选择 Bot（搜索 / 我的 / 按用户）'}
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
            onValueChange={(v) => {
              if (!v) return
              setSeatVersion(slot, v === 'current' ? undefined : Number(v))
            }}
          >
            <SelectTrigger className="w-full" aria-label={`${seatLabel}版本`}>
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
          </>
        ) : (
          <div className="space-y-1.5">
            <Select
              value={seat.localAgentId || 'none'}
              onValueChange={(value) => { if (value && value !== 'none') setSeatLocalAgent(slot, value) }}
              disabled={agentsLoading && gameAgents.length === 0}
            >
              <SelectTrigger className="w-full" aria-label={`${seatLabel}本地 Bot 连接`}>
                <SelectValue placeholder="选择本地 Bot" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="none" disabled>
                  {agentsLoading ? '正在读取连接…' : gameAgents.length === 0 ? '还没有本地连接' : '选择本地 Bot'}
                </SelectItem>
                {gameAgents.map((agent) => {
                  const status = localAgentStatus(agent)
                  return (
                    <SelectItem key={agent.public_id} value={agent.public_id} disabled={!status.available}>
                      {localAgentBotName(agent)} · {agent.label} · {status.label}
                    </SelectItem>
                  )
                })}
              </SelectContent>
            </Select>
            {localAgent ? (
              <p className={cn('text-xs', localAgentStatus(localAgent).available ? 'text-primary' : 'text-destructive')}>
                {localAgentBotName(localAgent)} · {localAgent.label} · {localAgentStatus(localAgent).label}
              </p>
            ) : (
              <p className="text-xs text-muted-foreground">
                先在“我的 Bot”建立连接，并让电脑保持在线。
              </p>
            )}
          </div>
        )}
      </div>
    )
  }

  const localSeatsAvailable = ([0, 1] as const).every((index) => (
    seats[index].environment !== 'remote_local'
    || (selectedLocalAgents[index] != null && localAgentStatus(selectedLocalAgents[index]!).available)
  ))
  const ready = seat2Kind === 'human'
    ? !!seats[0].bot
    : selectedBotIds[0] != null
      && selectedBotIds[1] != null
      && localSeatsAvailable
      && (!usesLocalBot || !agentError)
      && !(
        selectedLocalAgents[0]
        && selectedLocalAgents[0]?.public_id === selectedLocalAgents[1]?.public_id
      )
  const submissionReady = ready && timeControlReady && !timeControlsLoading && !timeControlsError

  return (
    <PageStub title="发起挑战" subtitle="选择双方如何运行；日常测试使用节能沙箱或自己的电脑。">
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
              <ErrorMsg
                msg={executionOffline ? '当前离线；请求仍保留，联网后会自动恢复。' : error}
                announce={false}
              />
              <div className="flex flex-wrap gap-2">
                <Button type="button" variant="outline" className="max-sm:min-h-11" onClick={refreshExecution}>
                  立即重试
                </Button>
                <Button type="button" variant="ghost" className="max-sm:min-h-11" onClick={resetExecution}>
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
                <ErrorMsg
                  msg={executionOffline ? '当前离线；以下保留上次状态，联网后会自动续查。' : error}
                  announce={false}
                />
                {executionStale && <p className="mt-1 text-xs text-muted-foreground">队列位置可能已变化。</p>}
              </div>
              <Button type="button" variant="outline" className="max-sm:min-h-11" onClick={refreshExecution}>
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
      <form
        onSubmit={(e) => void onSubmit(e)}
        className="mx-auto w-full max-w-5xl max-sm:[&_[data-slot=button]]:min-h-11 max-sm:[&_[data-slot=button]]:min-w-11 max-sm:[&_[data-slot=select-trigger]]:min-h-11"
        data-testid="challenge-form"
      >
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
                <SelectTrigger className="mt-1.5 w-full" aria-label="游戏">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {GAMES.map((g) => (
                    <SelectItem key={g.id} value={g.id}>{g.label}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-1.5 rounded-lg border border-border bg-muted/20 p-3">
              <Label>对局时限</Label>
              <Select
                value={timeControlId || 'time-control-pending'}
                onValueChange={(value) => { if (value !== 'time-control-pending') setTimeControlId(value) }}
                disabled={timeControlsLoading || !timeControlRegistry || timeControlRegistry.time_controls.length <= 1}
              >
                <SelectTrigger className="w-full" aria-label="对局时限" aria-describedby="challenge-time-control-help">
                  <SelectValue>
                    {selectedTimeControl
                      ? timeControlLabel(selectedTimeControl)
                      : timeControlsLoading
                        ? '正在读取时限…'
                        : '时限不可用'}
                  </SelectValue>
                </SelectTrigger>
                <SelectContent>
                  {timeControlRegistry?.time_controls.map((control) => (
                    <SelectItem key={control.id} value={control.id}>
                      {timeControlLabel(control)}{control.is_default ? ' · 默认' : ' · 练习'}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <p id="challenge-time-control-help" className="text-xs leading-relaxed text-muted-foreground">
                {selectedTimeControl
                  ? timeControlDescription(selectedTimeControl, seat2Kind === 'human')
                  : '只接受平台公开的固定时限，加载失败时不会提交对局。'}
              </p>
              {seat2Kind === 'human' && selectedTimeControl && (
                <p className="text-xs font-medium text-primary">
                  非对称练习：所选时限只约束 Bot；你仍使用页面的防挂机时限。
                </p>
              )}
              {seat2Kind === 'bot' && alternateTimeControl && (
                <p className="text-xs font-medium text-primary">
                  替代时限属于练习模式，本局不计平台排行榜。
                </p>
              )}
              {timeControlsError && <ErrorMsg msg={timeControlsError} className="text-xs" />}
            </div>

            <div className="rounded-lg border border-border p-3">
              {selfPlay && (
                <Badge variant="secondary" className="mb-3 gap-1">
                  <BotIcon className="size-3" />
                  自博弈
                </Badge>
              )}
              {seat2Kind === 'bot' && (
                <div
                  className="mb-3 flex flex-col gap-2 rounded-lg bg-muted/50 p-2.5 sm:flex-row sm:items-center sm:justify-between"
                  data-testid="challenge-my-seat"
                >
                  <div className="min-w-0">
                    <div className="flex items-center gap-2 text-sm font-medium text-foreground">
                      <ArrowLeftRight className="size-4 shrink-0 text-primary" aria-hidden="true" />
                      {user?.role === 'admin' ? '发起方 Bot 位置' : '我的 Bot 位置'}
                    </div>
                    <p className="mt-0.5 text-xs leading-relaxed text-muted-foreground">
                      切换时会连同双方 Bot、版本和运行位置一起交换。
                    </p>
                  </div>
                  <div
                    className="grid w-full grid-cols-2 gap-2 sm:w-auto"
                    role="group"
                    aria-label={user?.role === 'admin' ? '发起方 Bot 位置' : '我的 Bot 位置'}
                  >
                    {([0, 1] as const).map((index) => (
                      <button
                        key={index}
                        type="button"
                        onClick={() => chooseMySeat(index)}
                        aria-pressed={mySeat === index}
                        aria-label={`${user?.role === 'admin' ? '发起方 Bot' : '我的 Bot'} 设为${playerLabels[index]}`}
                        disabled={busy}
                        className={cn(
                          'inline-flex min-h-11 min-w-0 touch-manipulation items-center justify-center rounded-md border px-3 py-2 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50 disabled:pointer-events-none disabled:opacity-50',
                          mySeat === index
                            ? 'border-primary bg-primary text-primary-foreground'
                            : 'border-input bg-background text-foreground hover:bg-accent',
                        )}
                      >
                        <span className="min-w-0 break-words text-center [overflow-wrap:anywhere]">
                          作为{playerLabels[index]}
                        </span>
                      </button>
                    ))}
                  </div>
                </div>
              )}
              <div className="grid gap-3 sm:grid-cols-2">
                {/* 物理座位 0 固定为 Bot；Bot-vs-Bot 时可成为“我的位置”或对手位置。 */}
                {renderBotSeat('s1')}

                {/* 物理座位 1 可选 Bot 或本人；真人对战仍由独立 API 固定在该座位。 */}
                <div className="space-y-2">
                  <div className="flex items-center justify-between gap-2">
                    <div className="flex flex-wrap items-center gap-2">
                      <Label>{playerLabels[1]}</Label>
                      {seat2Kind === 'bot' && mySeat === 1 && (
                        <Badge variant="secondary" className="text-[10px]">
                          {user?.role === 'admin' ? '发起方 Bot' : '我的 Bot'}
                        </Badge>
                      )}
                    </div>
                    <div className="inline-flex rounded-lg border border-input p-0.5 text-xs" role="group" aria-label={`${playerLabels[1]}玩家类型`}>
                      <button
                        type="button"
                        onClick={() => chooseSeat2Kind('bot')}
                        aria-pressed={seat2Kind === 'bot'}
                        className={cn(
                          'inline-flex min-h-[var(--control-height)] items-center gap-1 rounded-md px-2 py-1 max-sm:min-h-11',
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
                          'inline-flex min-h-[var(--control-height)] items-center gap-1 rounded-md px-2 py-1 max-sm:min-h-11',
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

                  {seat2Kind === 'bot' ? renderBotSeat('s2', false) : (
                    <div className="rounded-lg border border-dashed border-input px-3 py-3 text-sm text-muted-foreground">
                      你（<strong className="break-words text-foreground [overflow-wrap:anywhere]">@{user?.username}</strong>）亲自上场，本局不计平台排行榜。
                    </div>
                  )}
                </div>
              </div>

              <p className="mt-3 text-xs text-muted-foreground">
                {seat2Kind === 'human'
                  ? `${playerLabels[0]}使用节能沙箱，${playerLabels[1]}由你亲自上场；本局不计平台排行榜。`
                  : '节能沙箱由平台运行；本地 Bot 由你的电脑回答裁判请求，可两边都选本地连接。'}
              </p>
            </div>

            {usesLocalBot && (
              <div className="flex min-w-0 items-start gap-2 rounded-lg border border-primary/25 bg-primary/5 px-3 py-2 text-xs" role="status">
                <Laptop className="mt-0.5 size-4 shrink-0 text-primary" aria-hidden="true" />
                <span className="min-w-0 break-words">
                  <strong className="text-foreground">本地 Bot 练习局，不计平台排行榜。</strong>
                  {' '}开始前请保持所选连接在线；平台只负责裁判，不会连接你的电脑端口。
                </span>
              </div>
            )}

            {agentError && <ErrorMsg msg={agentError} />}

            {error && (
              <div ref={errorAlertRef} role="alert" tabIndex={-1}>
                <ErrorMsg msg={error} />
              </div>
            )}
            <Button
              type="submit"
              disabled={busy || !submissionReady}
              className="w-full gap-1.5"
            >
              <Play className="size-4" />
              {busy ? '发起中…' : seat2Kind === 'human' ? '开始人类对战' : '开始对局'}
            </Button>
            {!busy && !submissionReady && (
              <p className="text-center text-xs text-muted-foreground">
                {!timeControlReady
                  ? '对局时限尚未就绪，请稍后重试'
                  : seat2Kind === 'human'
                  ? `请选择${playerLabels[0]}的 Bot`
                  : usesLocalBot
                    ? '请为双方选择可用连接；离线或正在对局的本地 Bot 不能开始'
                    : '请为双方选择 Bot'}
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
          purpose={
            seat2Kind === 'human'
              ? 'opponent'
              : (pickingSeat === 's1' ? 0 : 1) === mySeat
              ? user?.role === 'admin' ? 'initiator' : 'mine'
              : 'opponent'
          }
          mineOnly={
            pickingSeat !== null
            && (pickingSeat === 's1' ? 0 : 1) === mySeat
            && seat2Kind === 'bot'
            && user?.role !== 'admin'
          }
          onClose={() => setPickingSeat(null)}
          onPick={(b) => pickBotFor(pickingSeat, b)}
        />
      )}
      {confirmDialog}
    </PageStub>
  )
}
