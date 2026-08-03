/**
 * 统一对局页（实时观赛 + 历史回放合一）。路由 /match/:id 唯一页。
 *
 * - running/pending → 直播模式：开 SSE，定位到最新后按回放速度推进（DVR 模型），
 *   Bot 瞬间连走则游标落后、显示「落后 N 手」、可「跳到最新」；match_end 到达后
 *   游标走完剩余停（不强制跳结局）；已结束对局重开页 → 自动从头播放。
 * - completed/aborted → 回放模式：一次性加载 events_json，从头自动播放。
 * - 座位身份：从 match.bot_a/bot_b（后端 JOIN）构造 SeatInfo 传 canvas。
 * - 合并旧 MatchDetail + ArenaWatch；/watch/:id 重定向到此。
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { Play, Pause, ChevronLeft, ChevronRight, SkipBack, SkipForward, Radio, ArrowLeft } from 'lucide-react'
import PageStub from '@/components/PageStub'
import MatchBoard from '@/components/MatchBoard'
import { Card, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Slider } from '@/components/ui/slider'
import { ErrorMsg, Loading } from '@/components/ui/status'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { apiGet, apiPost, errMsg } from '@/api'
import { gameLabel, gameIcon, normalizeGameId, matchTypeBadge } from '@/lib/games'
import { isBoardGame } from '@/games'
import Comments from '@/components/Comments'
import { SPEEDS } from '@/components/use-playback'
import { getGame } from '@/games'
import type { RawEvent } from '@/games/base'
import {
  type MatchSeatRow,
  seatInfos,
  seatHeaderLabel,
  resolveWinnerLabel,
  fmtNet,
} from '@/lib/match-seats'

type MatchRow = MatchSeatRow & { game_id?: string; status?: string; reason?: string }

const STATUS_VARIANT: Record<string, 'default' | 'secondary' | 'destructive' | 'outline'> = {
  connecting: 'secondary', live: 'default', match_end: 'outline', error: 'destructive',
  completed: 'default', aborted: 'destructive', running: 'default', pending: 'secondary',
}

/** 找德州每手起始事件索引（逐手跳转用）。 */
function handBoundaries(events: RawEvent[]): number[] {
  const bounds: number[] = []
  events.forEach((ev, i) => { if (ev.type === 'hand_start') bounds.push(i) })
  if (events.length) bounds.push(events.length)
  return bounds
}

export default function MatchViewer() {
  const { id } = useParams<{ id: string }>()
  const [match, setMatch] = useState<MatchRow | null>(null)
  const [events, setEvents] = useState<RawEvent[]>([])
  const [status, setStatus] = useState<string>('connecting')  // connecting|live|match_end|error|replay
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  // 播放游标（-1 = 贴尾/未启动；否则 0-based）
  const [stepIdx, setStepIdx] = useState(-1)
  const [playing, setPlaying] = useState(false)
  const [speedIdx, setSpeedIdx] = useState(1)
  const logRef = useRef<HTMLDivElement>(null)
  const curActionRef = useRef<HTMLDivElement>(null)

  // 直播 SSE / 回放加载（一次性探测状态，决定模式）
  const isLiveMatch = match?.status === 'running' || match?.status === 'pending'
  useEffect(() => {
    if (!id) return
    setLoading(true)
    setError('')
    setEvents([])
    setStepIdx(-1)
    setPlaying(false)
    let cancelled = false
    let es: EventSource | null = null

    // 浏览计数（公开，失败忽略）
    void apiPost(`/api/matches/${encodeURIComponent(id)}/view`, 'POST', {}).catch(() => undefined)

    void apiGet<{ match: MatchRow; replay: { events_json?: string } }>(`/api/matches/${encodeURIComponent(id)}`)
      .then((d) => {
        if (cancelled) return
        setMatch(d.match)
        const m = d.match
        const evs: RawEvent[] = (() => { try { return JSON.parse(d.replay?.events_json || '[]') as RawEvent[] } catch { return [] } })()
        setEvents(evs)
        const live = m.status === 'running' || m.status === 'pending'
        if (live) {
          setStatus('live'); setStepIdx(-1); setPlaying(true)
          es = new EventSource(`/api/matches/${encodeURIComponent(id)}/events`)
          es.onmessage = (msg) => {
            try {
              const ev = JSON.parse(msg.data) as RawEvent
              if (ev.type === 'snapshot') {
                if (ev.match) setMatch(ev.match as MatchRow)
                const hist = Array.isArray(ev.events) ? (ev.events as RawEvent[]) : []
                setEvents(hist.slice(-4000))
                setStepIdx(-1); setPlaying(true)
              } else {
                setEvents((prev) => [...prev, ev])
              }
              if (ev.type === 'match_end' || ev.type === 'error') {
                // 回写胜者/earnings 到 match，避免顶栏一直「—」
                setMatch((prev) => {
                  if (!prev) return prev
                  const patch: MatchRow = { ...prev, status: 'completed' }
                  if (ev.winner !== undefined) patch.winner = ev.winner as number | null
                  if (ev.earnings_a !== undefined) patch.earnings_a = Number(ev.earnings_a)
                  if (ev.earnings_b !== undefined) patch.earnings_b = Number(ev.earnings_b)
                  if (ev.reason) patch.reason = String(ev.reason)
                  return patch
                })
                setStatus(String(ev.type)); es?.close()
              }
            } catch { /* ignore */ }
          }
          es.onerror = () => { setStatus((s) => (s === 'match_end' ? s : 'error')); es?.close() }
        } else {
          setStatus('replay'); setStepIdx(evs.length > 0 ? 0 : -1); setPlaying(evs.length > 0)
        }
      })
      .catch((e) => { if (!cancelled) { setError(errMsg(e)); setStatus('error') } })
      .finally(() => { if (!cancelled) setLoading(false) })

    return () => { cancelled = true; es?.close() }
  }, [id])

  const gameId = normalizeGameId(match?.game_id)
  const isBoard = isBoardGame(gameId)
  const total = events.length
  // cur：当前显示到第几步。-1 = 贴尾（直播跟随/回放启动前）
  const cur = stepIdx < 0 ? Math.max(0, total - 1) : Math.min(stepIdx, total - 1)
  const visible = total > 0 ? events.slice(0, cur + 1) : []
  const atLive = stepIdx < 0
  const lag = atLive ? 0 : Math.max(0, total - 1 - cur)
  const seats = seatInfos(match)
  // 当前可见事件归约：经注册表 spec.reduce（消除 gameId=== if-chain）。
  // kind 区分仍保留用于下方 VM 字段分流（扑克 matchWinner vs 棋类 winner）。
  const visibleVm = useMemo(() => {
    if (total === 0) return null
    const slice = events.slice(0, cur + 1)
    const kind = getGame(gameId).kind
    const vm = getGame(gameId).reduce(slice as RawEvent[])
    return { kind, vm } as
      | { kind: 'cards'; vm: { matchWinner: number | null; seats?: { net: number }[] } }
      | { kind: 'board'; vm: { winner: number | null; moveCount?: number; scores?: number[] } }
  }, [gameId, events, cur, total])
  const finished =
    match?.status === 'completed' ||
    match?.status === 'aborted' ||
    status === 'match_end' ||
    status === 'replay'
  const eventWinner =
    visibleVm?.kind === 'cards' ? visibleVm.vm.matchWinner
      : visibleVm?.kind === 'board' ? visibleVm.vm.winner
        : undefined
  const colorLabel = (seat: number) => {
    if (!match) return `座位 ${seat}`
    // 棋类座位着色（黑白/红蓝）按 spec.kind === 'board' 判定，再细分 gomoku/pencil
    if (isBoardGame(gameId)) {
      const colorName = gameId === 'gomoku' ? (seat === 0 ? '黑' : '白') : (seat === 0 ? '红' : '蓝')
      return `${seatHeaderLabel(match, seat as 0 | 1)}（${colorName}）`
    }
    return seatHeaderLabel(match, seat as 0 | 1)
  }
  const winnerLabel = resolveWinnerLabel(match, eventWinner, finished, colorLabel)
  const netA = visibleVm?.kind === 'cards'
    ? visibleVm.vm.seats?.[0]?.net ?? null
    : typeof match?.earnings_a === 'number' ? match.earnings_a : null
  const netB = visibleVm?.kind === 'cards'
    ? visibleVm.vm.seats?.[1]?.net ?? null
    : typeof match?.earnings_b === 'number' ? match.earnings_b : null
  const liveSteps = visibleVm?.kind === 'board' ? visibleVm.vm.moveCount ?? null : null
  const pencilScores = gameId === 'pencil' && visibleVm?.kind === 'board' ? visibleVm.vm.scores ?? null : null
  const typeBadge = matchTypeBadge(match?.match_type)

  // 定速播放定时器（直播+回放共用）：按 SPEEDS 步进；到末尾后直播继续等（保持 playing），
  // 回放则停。直播时游标追上末尾 → 转贴尾(-1)，新事件来了继续推进。
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => {
    if (!playing || total === 0) return
    if (cur >= total - 1) {
      if (!isLiveMatch && status !== 'live') {
        // 回放到头：停
        setPlaying(false)
      } else {
        // 直播追上末尾：转贴尾等新事件（保持 playing，不步进）
        setStepIdx(-1)
      }
      return
    }
    timerRef.current = setTimeout(() => {
      setStepIdx((s) => {
        const next = (s < 0 ? total - 1 : s) + 1
        return next >= total - 1 ? -1 : next
      })
    }, SPEEDS[speedIdx].ms)
    return () => { if (timerRef.current) clearTimeout(timerRef.current) }
  }, [playing, cur, total, speedIdx, isLiveMatch, status])

  const pause = () => setPlaying(false)
  const step = (delta: number) => {
    setPlaying(false)
    setStepIdx((s) => {
      const base = s < 0 ? Math.max(0, total - 1) : s
      return Math.max(0, Math.min(Math.max(0, total - 1), base + delta))
    })
  }
  const seek = (idx: number) => { setPlaying(false); setStepIdx(idx) }
  const jumpToLive = () => { setStepIdx(-1); setPlaying(true) }
  const togglePlay = () => {
    if (!playing && cur >= total - 1 && !atLive) setStepIdx(total > 1 ? 0 : -1)
    setPlaying((p) => !p)
  }

  // 德州逐手跳转
  const bounds = useMemo(() => handBoundaries(events), [events])
  const jumpHand = (delta: number) => {
    pause()
    if (!bounds.length) return
    let hIdx = 0
    for (let i = 0; i < bounds.length - 1; i++) if (cur >= bounds[i] && cur < bounds[i + 1]) { hIdx = i; break }
    const target = Math.max(0, Math.min(bounds.length - 2, hIdx + delta))
    setStepIdx(bounds[target] ?? 0)
  }
  const curHandIdx = (() => { for (let i = 0; i < bounds.length - 1; i++) if (cur >= bounds[i] && cur < bounds[i + 1]) return i; return bounds.length >= 2 ? bounds.length - 2 : 0 })()

  // 动作日志自动滚动到当前步
  useEffect(() => {
    const c = logRef.current, row = curActionRef.current
    if (!c || !row) return
    const cTop = c.scrollTop, cBottom = cTop + c.clientHeight
    const rTop = row.offsetTop, rBottom = rTop + row.offsetHeight
    if (rTop < cTop) c.scrollTop = rTop
    else if (rBottom > cBottom) c.scrollTop = rBottom - c.clientHeight
  }, [cur])

  const GameIcon = gameIcon(gameId)
  const isLive = status === 'live' || isLiveMatch

  return (
    <PageStub title={isLive ? '实时观赛' : '对局详情'}>
      <div className="mb-4 flex flex-wrap items-center gap-2 text-sm">
        <span className="font-mono text-xs text-muted-foreground">{id}</span>
        <Badge variant="secondary" className="gap-1"><GameIcon className="size-3" />{gameLabel(gameId)}</Badge>
        {typeBadge && (
          <Badge variant="outline" className={`text-[10px] ${typeBadge.cls}`}>{typeBadge.label}</Badge>
        )}
        <Badge variant={STATUS_VARIANT[status] ?? 'secondary'} className="gap-1">
          {status === 'live' && <span className="size-1.5 animate-pulse rounded-full bg-current" />}
          {status === 'live' ? '直播中' : status === 'completed' ? '已完成' : status === 'aborted' ? '已中止' : status === 'match_end' ? '已结束' : status === 'error' ? '出错' : status === 'connecting' ? '连接中' : '回放'}
        </Badge>
        {match && (
          <span className="text-muted-foreground">
            {isBoard ? '步数' : '手数'}：
            <span className="font-mono text-foreground">
              {String(liveSteps ?? match.hands_played ?? 0)}
            </span>
            {!isBoard && <span className="text-muted-foreground">/{String(match.total_hands ?? '')}</span>}
          </span>
        )}
        {match && (
          <span className="text-muted-foreground">
            胜者：
            <span className="font-medium text-foreground">{winnerLabel}</span>
          </span>
        )}
        {match?.reason && (match.status === 'aborted' || status === 'error') && (
          <span className="text-xs text-destructive">原因：{match.reason}</span>
        )}
        {lag > 0 && (
          <Button variant="outline" size="sm" onClick={jumpToLive} className="gap-1">
            <Radio className="size-3" />落后 {lag} 手·跳最新
          </Button>
        )}
      </div>

      {/* 对阵双方 + 累计筹码 / 点格比分 */}
      {match && (
        <div className="mb-4 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
          <span className="font-medium text-foreground">{seatHeaderLabel(match, 0)}</span>
          <span className="text-muted-foreground">vs</span>
          <span className="font-medium text-foreground">{seatHeaderLabel(match, 1)}</span>
          {!isBoard && netA != null && netB != null && (
            <span className="font-mono text-xs text-muted-foreground">
              累计筹码{' '}
              <span className={netA >= 0 ? 'text-emerald-600 dark:text-emerald-400' : 'text-red-500'}>
                座0 {fmtNet(netA)}
              </span>
              {' · '}
              <span className={netB >= 0 ? 'text-emerald-600 dark:text-emerald-400' : 'text-red-500'}>
                座1 {fmtNet(netB)}
              </span>
            </span>
          )}
          {pencilScores && (
            <span className="font-mono text-xs text-muted-foreground">
              比分 <span className="text-red-500">{pencilScores[0]}</span>
              {' : '}
              <span className="text-blue-500">{pencilScores[1]}</span>
            </span>
          )}
          {match.bot_a_id != null && match.bot_b_id != null && match.match_type !== 'human' && (
            <span className="text-xs text-muted-foreground">
              <Link to={`/bot/${match.bot_a_id}`} className="text-primary hover:underline">座0 详情</Link>
              {' · '}
              <Link to={`/bot/${match.bot_b_id}`} className="text-primary hover:underline">座1 详情</Link>
            </span>
          )}
        </div>
      )}

      {error && <ErrorMsg msg={error} className="mb-4" />}

      {loading ? (
        <Loading text="加载中…" />
      ) : visible.length === 0 ? (
        <Card><CardContent className="py-12 text-center text-sm text-muted-foreground">暂无事件</CardContent></Card>
      ) : (
        <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_22rem]">
          {/* 左：canvas 棋盘/牌桌 + 手导航 + 控制条 */}
          <div className="space-y-3">
            <MatchBoard gameId={gameId} events={visible} seats={seats} revealMode="all" />

            {/* 德州手导航器 */}
            {!isBoard && bounds.length >= 2 && (
              <div>
                <div className="mb-1 text-[10px] text-muted-foreground">手导航（点击跳转）</div>
                <div className="flex flex-wrap gap-1 rounded-lg border border-border bg-card p-2">
                  {Array.from({ length: bounds.length - 1 }, (_, h) => (
                    <button key={h} type="button" onClick={() => seek(bounds[h] ?? 0)}
                      className={`relative size-7 rounded text-[10px] font-medium transition ${h === curHandIdx ? 'bg-primary text-primary-foreground' : 'bg-muted text-muted-foreground hover:bg-accent'}`}>
                      {h + 1}
                    </button>
                  ))}
                </div>
              </div>
            )}

            {/* 控制条 */}
            <Card>
              <CardContent className="py-3">
                <div className="flex flex-wrap items-center justify-center gap-1.5">
                  {!isBoard && (
                    <Button variant="outline" size="sm" onClick={() => jumpHand(-1)} className="gap-1"><SkipBack className="size-3.5" />上一手</Button>
                  )}
                  <Button variant="outline" size="sm" onClick={() => step(-1)} className="gap-1"><ChevronLeft className="size-4" />上一步</Button>
                  <Button variant="default" size="sm" onClick={togglePlay} className="gap-1.5">
                    {playing ? <><Pause className="size-4" />暂停</> : <><Play className="size-4" />播放</>}
                  </Button>
                  <Button variant="outline" size="sm" onClick={() => step(1)} className="gap-1">下一步<ChevronRight className="size-4" /></Button>
                  {!isBoard && (
                    <Button variant="outline" size="sm" onClick={() => jumpHand(1)} className="gap-1">下一手<SkipForward className="size-3.5" /></Button>
                  )}
                  <Select value={String(speedIdx)} onValueChange={(v) => setSpeedIdx(Number(v))}>
                    <SelectTrigger size="sm" className="h-8 w-[5rem] text-xs">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {SPEEDS.map((s, i) => (<SelectItem key={i} value={String(i)}>{s.label}</SelectItem>))}
                    </SelectContent>
                  </Select>
                </div>
                <div className="mt-3 flex items-center gap-3">
                  <span className="font-mono text-[10px] text-muted-foreground">步 {cur + 1}/{total}{atLive ? ' ·直播' : ''}</span>
                  <Slider min={0} max={Math.max(0, total - 1)} value={[cur]} onValueChange={(v) => seek(v[0])} className="flex-1" />
                </div>
              </CardContent>
            </Card>
          </div>

          {/* 右：动作时序 */}
          <Card className="flex flex-col">
            <div className="border-b border-border px-4 py-2 text-sm font-semibold text-foreground">
              动作时序 <span className="text-xs font-normal text-muted-foreground">({visible.length})</span>
            </div>
            <div ref={logRef} className="max-h-[60vh] flex-1 overflow-y-auto p-2 text-xs">
              {visible.map((ev, i) => (
                <div key={i} ref={i === cur ? curActionRef : undefined}
                  className={`flex items-center gap-2 rounded px-2 py-1 ${i === cur ? 'bg-primary/10 font-medium text-primary' : 'text-muted-foreground'}`}>
                  <span className="w-8 font-mono opacity-60">{i + 1}</span>
                  <span className="w-14 opacity-70">{ev.type}</span>
                  <span className="flex-1 truncate opacity-80">{eventDesc(ev)}</span>
                </div>
              ))}
            </div>
          </Card>
        </div>
      )}

      <Button asChild variant="ghost" size="sm" className="mt-6 gap-1.5">
        <Link to="/"><ArrowLeft className="size-4" />返回</Link>
      </Button>
      {id && <Comments targetType="match" targetId={id} />}
    </PageStub>
  )
}

function eventDesc(ev: RawEvent): string {
  const ACTION: Record<string, string> = { fold: '弃牌', check: '过牌', call: '跟注', raise: '加注', allin: '全押' }
  if (ev.type === 'action') return `座${ev.player} · ${ACTION[String(ev.action)] ?? ev.action}${ev.amount ? ' ' + String(ev.amount) : ''}`
  if (ev.type === 'move') return `座${ev.player} · (${ev.x},${ev.y})${ev.scored ? ' · 得分连走' : ''}`
  if (ev.type === 'settle') return `赢家 座${(ev.winners as number[] | undefined)?.join('/') ?? '?'} · 底池 ${ev.pot}`
  if (ev.type === 'hand_start') return `第 ${(Number(ev.hand) || 0) + 1} 手开始`
  if (ev.type === 'deal_board') return `${ev.street}: ${(ev.dealt as string[] | undefined)?.join(' ')}`
  if (ev.type === 'deal_hole') return '发底牌'
  if (ev.type === 'match_start') return '对局开始'
  if (ev.type === 'match_end') return `结束 · 胜者 ${ev.winner ?? '平'}`
  if (ev.type === 'turn') return `轮到座${ev.player}`
  return ev.type || '?'
}
