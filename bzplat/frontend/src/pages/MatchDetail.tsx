import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  Play,
  Pause,
  SkipBack,
  SkipForward,
  ChevronLeft,
  ChevronRight,
  Radio,
  ArrowLeft,
} from 'lucide-react'
import PageStub from '@/components/PageStub'
import MatchBoard from '@/components/MatchBoard'
import { type RawEvent } from '@/components/poker/useMatchState'
import { Card, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Slider } from '@/components/ui/slider'
import { ErrorMsg, Loading } from '@/components/ui/status'
import { apiGet, errMsg } from '@/api'
import { gameLabel, gameIcon, normalizeGameId } from '@/lib/games'
import { isBoardGame } from '@/games'
import Comments from '@/components/Comments'

const SPEEDS = [
  { label: '0.5x', ms: 1400 },
  { label: '1x', ms: 700 },
  { label: '2x', ms: 350 },
  { label: '4x', ms: 175 },
]

const ACTION_LABEL: Record<string, string> = {
  fold: '弃牌', check: '过牌', call: '跟注', raise: '加注', allin: '全押',
}

/** 找到「每手的起始事件索引」，用于逐手跳转与导航器 */
function handBoundaries(events: RawEvent[]): number[] {
  const bounds: number[] = []
  events.forEach((ev, i) => {
    if (ev.type === 'hand_start') bounds.push(i)
  })
  if (events.length) bounds.push(events.length)
  return bounds
}

/** 每手的赢家（从 settle 事件提取），用于导航器绿点 */
function handWinners(events: RawEvent[]): (number[] | null)[] {
  const out: (number[] | null)[] = []
  let cur: number[] | null = null
  for (const ev of events) {
    if (ev.type === 'hand_start') {
      if (out.length === 0 || cur !== null) out.push(cur)
      cur = null
    } else if (ev.type === 'settle') {
      cur = (ev.winners as number[] | undefined) ?? null
    }
  }
  if (out.length === 0 || cur !== null) out.push(cur)
  return out
}

export default function MatchDetail() {
  const { id } = useParams()
  const [data, setData] = useState<{
    match: Record<string, unknown>
    replay: { events_json?: string }
  } | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [stepIdx, setStepIdx] = useState(-1)
  const [playing, setPlaying] = useState(false)
  const [speedIdx, setSpeedIdx] = useState(1)

  useEffect(() => {
    if (!id) return
    setLoading(true)
    apiGet<{ match: Record<string, unknown>; replay: { events_json?: string } }>(
      `/api/matches/${encodeURIComponent(id)}`,
    )
      .then((d) => {
        setData(d)
        setError('')
      })
      .catch((e) => setError(errMsg(e)))
      .finally(() => setLoading(false))
  }, [id])

  const events = useMemo<RawEvent[]>(() => {
    try {
      return JSON.parse(data?.replay?.events_json || '[]') as RawEvent[]
    } catch {
      return []
    }
  }, [data])

  const bounds = useMemo(() => handBoundaries(events), [events])
  const winners = useMemo(() => handWinners(events), [events])
  const total = events.length
  const cur = stepIdx < 0 ? total - 1 : Math.min(stepIdx, total - 1)
  const visible = cur >= 0 ? events.slice(0, cur + 1) : []
  const gameId = normalizeGameId(
    data?.match?.game_id != null ? String(data.match.game_id) : 'holdem',
  )
  const isBoard = isBoardGame(gameId)

  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => {
    if (!playing || total === 0) return
    if (cur >= total - 1) {
      setPlaying(false)
      return
    }
    timerRef.current = setTimeout(() => {
      setStepIdx((s) => {
        const next = (s < 0 ? total - 1 : s) + 1
        return next >= total - 1 ? -1 : next
      })
    }, SPEEDS[speedIdx].ms)
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current)
    }
  }, [playing, cur, total, speedIdx])

  useEffect(() => {
    if (total > 0 && stepIdx === -1) setStepIdx(total - 1)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [total])

  const pause = () => setPlaying(false)

  const jumpHand = (delta: number) => {
    pause()
    if (!bounds.length) return
    let hIdx = 0
    for (let i = 0; i < bounds.length - 1; i++) {
      if (cur >= bounds[i] && cur < bounds[i + 1]) {
        hIdx = i
        break
      }
    }
    const target = Math.max(0, Math.min(bounds.length - 2, hIdx + delta))
    setStepIdx(bounds[target] ?? 0)
  }

  const jumpToHand = (hIdx: number) => {
    pause()
    setStepIdx(bounds[hIdx] ?? 0)
  }

  const actionListRef = useRef<HTMLDivElement>(null)
  const curActionRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const container = actionListRef.current
    const row = curActionRef.current
    if (!container || !row) return
    const cTop = container.scrollTop
    const cBottom = cTop + container.clientHeight
    const rTop = row.offsetTop
    const rBottom = rTop + row.offsetHeight
    if (rTop < cTop) container.scrollTop = rTop
    else if (rBottom > cBottom) container.scrollTop = rBottom - container.clientHeight
  }, [cur])

  const match = data?.match
  const isLive = match?.status === 'running' || match?.status === 'pending'
  const curHandIdx = (() => {
    for (let i = 0; i < bounds.length - 1; i++) {
      if (cur >= bounds[i] && cur < bounds[i + 1]) return i
    }
    return bounds.length >= 2 ? bounds.length - 2 : 0
  })()
  const GameIcon = gameIcon(gameId)

  return (
    <PageStub title="对局详情">
      <p className="font-mono text-xs text-muted-foreground">{id}</p>
      {error && <ErrorMsg msg={error} className="mt-4" />}

      {match && (
        <div className="mb-4 flex flex-wrap items-center gap-2 text-sm">
          <GameIcon className="size-4 text-muted-foreground" />
          <span className="font-medium text-foreground">{gameLabel(gameId)}</span>
          <Badge variant={match.status === 'completed' ? 'default' : match.status === 'aborted' ? 'destructive' : 'secondary'} className="text-[10px]">
            {String(match.status)}
          </Badge>
          <span className="text-muted-foreground">
            {isBoard ? '步数' : '手数'}：<span className="font-mono text-foreground">{String(match.hands_played)}</span>
            {!isBoard && <span className="text-muted-foreground">/{String(match.total_hands)}</span>}
          </span>
          <span className="text-muted-foreground">胜者：<span className="text-foreground">{match.winner == null ? '—' : `座${String(match.winner)}`}</span></span>
          {isLive && (
            <Button asChild variant="default" size="sm" className="ml-auto gap-1.5">
              <Link to={`/watch/${id}`}><Radio className="size-3.5" />实时观赛</Link>
            </Button>
          )}
        </div>
      )}

      {loading ? (
        <Loading text="加载回放…" />
      ) : visible.length === 0 ? (
        <Card>
          <CardContent className="py-12 text-center text-sm text-muted-foreground">
            {isLive ? (
              <>对局进行中，暂无完整回放。{' '}<Link to={`/watch/${id}`} className="font-medium text-primary hover:underline">去观赛</Link></>
            ) : events.length === 0 ? '暂无回放数据' : ''}
          </CardContent>
        </Card>
      ) : (
        <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_22rem]">
          {/* 左：棋盘/牌桌 + 手导航 + 控制条 */}
          <div className="space-y-3">
            <MatchBoard gameId={gameId} events={visible} revealMode="all" />

            {/* 手导航器（扑克） */}
            {!isBoard && bounds.length >= 2 && (
              <div>
                <div className="mb-1 text-[10px] text-muted-foreground">手导航（点击跳转）</div>
                <div className="flex flex-wrap gap-1 rounded-lg border border-border bg-card p-2">
                  {Array.from({ length: bounds.length - 1 }, (_, h) => {
                    const ws = winners[h]
                    const isCur = h === curHandIdx
                    return (
                      <button
                        key={h}
                        type="button"
                        onClick={() => jumpToHand(h)}
                        title={`第 ${h + 1} 手${ws ? `：胜者座位 ${ws.join('/')}` : ''}`}
                        className={`relative size-7 rounded text-[10px] font-medium transition ${
                          isCur ? 'bg-primary text-primary-foreground' : 'bg-muted text-muted-foreground hover:bg-accent'
                        }`}
                      >
                        {h + 1}
                        {ws && !isCur && <span className="absolute -right-0.5 -top-0.5 size-2 rounded-full bg-success" />}
                      </button>
                    )
                  })}
                </div>
              </div>
            )}

            {/* 回放控制条 */}
            <Card>
              <CardContent className="py-3">
                <div className="flex flex-wrap items-center justify-center gap-1.5">
                  {!isBoard && (
                    <Button variant="outline" size="sm" onClick={() => jumpHand(-1)} className="gap-1">
                      <SkipBack className="size-3.5" />上一手
                    </Button>
                  )}
                  <Button variant="outline" size="sm" onClick={() => { pause(); setStepIdx((s) => Math.max(0, (s < 0 ? total - 1 : s) - 1)) }} className="gap-1">
                    <ChevronLeft className="size-4" />上一步
                  </Button>
                  <Button variant="default" size="sm" onClick={() => { if (cur >= total - 1) setStepIdx(0); setPlaying((p) => !p) }} className="gap-1.5">
                    {playing ? <><Pause className="size-4" />暂停</> : <><Play className="size-4" />播放</>}
                  </Button>
                  <Button variant="outline" size="sm" onClick={() => { pause(); setStepIdx((s) => Math.min(total - 1, (s < 0 ? total - 1 : s) + 1)) }} className="gap-1">
                    下一步<ChevronRight className="size-4" />
                  </Button>
                  {!isBoard && (
                    <Button variant="outline" size="sm" onClick={() => jumpHand(1)} className="gap-1">
                      下一手<SkipForward className="size-3.5" />
                    </Button>
                  )}
                  <select value={speedIdx} onChange={(e) => setSpeedIdx(Number(e.target.value))} className="h-8 rounded-lg border border-input bg-background px-2 text-xs text-foreground focus:outline-none focus:ring-2 focus:ring-ring">
                    {SPEEDS.map((s, i) => (<option key={i} value={i}>{s.label}</option>))}
                  </select>
                </div>
                <div className="mt-3 flex items-center gap-3">
                  <span className="font-mono text-[10px] text-muted-foreground">步 {cur + 1}/{total}</span>
                  <Slider min={0} max={Math.max(0, total - 1)} value={[cur]} onValueChange={(v) => { pause(); setStepIdx(v[0]) }} className="flex-1" />
                </div>
              </CardContent>
            </Card>
          </div>

          {/* 右：动作时序列表 */}
          <Card className="flex flex-col">
            <div className="border-b border-border px-4 py-2 text-sm font-semibold text-foreground">
              动作时序 <span className="text-xs font-normal text-muted-foreground">({visible.length})</span>
            </div>
            <div ref={actionListRef} className="max-h-[60vh] flex-1 overflow-y-auto p-2 text-xs">
              {visible.map((ev, i) => (
                <div
                  key={i}
                  ref={i === cur ? curActionRef : undefined}
                  className={`flex items-center gap-2 rounded px-2 py-1 ${
                    i === cur ? 'bg-primary/10 font-medium text-primary' : 'text-muted-foreground'
                  }`}
                >
                  <span className="w-8 font-mono opacity-60">{i + 1}</span>
                  <span className="w-14 opacity-70">{ev.type}</span>
                  <span className="flex-1 truncate opacity-80">
                    {ev.type === 'action'
                      ? `座位 ${ev.player} · ${ACTION_LABEL[String(ev.action)] ?? ev.action}${ev.amount ? ' ' + String(ev.amount) : ''}`
                      : ev.type === 'move'
                        ? `座位 ${ev.player} · (${ev.x},${ev.y})${ev.scored ? ' · 得分连走' : ''}`
                        : ev.type === 'settle'
                          ? `赢家 座位 ${(ev.winners as number[] | undefined)?.join('/') ?? '?'} · 底池 ${ev.pot}`
                          : ev.type === 'hand_start'
                            ? `第 ${(Number(ev.hand) || 0) + 1} 手开始`
                            : ev.type === 'deal_board'
                              ? `${ev.street}：${(ev.dealt as string[] | undefined)?.join(' ')}`
                              : ev.type === 'match_end'
                                ? `结束 · 胜者 ${ev.winner ?? '平'} · ${ev.reason || ''}`
                                : JSON.stringify(ev).slice(0, 60)}
                  </span>
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
