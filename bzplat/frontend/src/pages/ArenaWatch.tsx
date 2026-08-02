import { useEffect, useRef, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { Radio, ArrowRight } from 'lucide-react'
import PageStub from '@/components/PageStub'
import MatchBoard from '@/components/MatchBoard'
import { type RawEvent } from '@/components/poker/useMatchState'
import { usePlayback } from '@/components/use-playback'
import { PlaybackControls } from '@/components/playback-controls'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Loading } from '@/components/ui/status'
import { apiGet } from '@/api'
import { gameLabel, gameIcon, normalizeGameId } from '@/lib/games'
import { isBoardGame } from '@/games'

const STATUS_VARIANT: Record<string, 'default' | 'secondary' | 'destructive' | 'outline'> = {
  idle: 'secondary', connecting: 'secondary', live: 'default', match_end: 'outline', error: 'destructive',
}
const STATUS_LABEL: Record<string, string> = {
  idle: '空闲', connecting: '连接中', live: '直播中', match_end: '已结束', error: '出错',
}

/** 事件 → 可读描述（动作时序侧栏用） */
function eventDesc(ev: RawEvent): string {
  const ACTION: Record<string, string> = { fold: '弃牌', check: '过牌', call: '跟注', raise: '加注', allin: '全押' }
  if (ev.type === 'action') return `座${ev.player} · ${ACTION[String(ev.action)] ?? ev.action}${ev.amount ? ' ' + ev.amount : ''}`
  if (ev.type === 'move') return `座${ev.player} · (${ev.x},${ev.y})${ev.scored ? ' · 得分连走' : ''}`
  if (ev.type === 'settle') return `赢家 座${(ev.winners as number[] | undefined)?.join('/') ?? '?'} · 底池${ev.pot}`
  if (ev.type === 'hand_start') return `第 ${(Number(ev.hand) || 0) + 1} 手开始`
  if (ev.type === 'deal_board') return `${ev.street}: ${(ev.dealt as string[] | undefined)?.join(' ')}`
  if (ev.type === 'deal_hole') return `发底牌`
  if (ev.type === 'match_start') return `对局开始`
  if (ev.type === 'match_end') return `结束 · 胜者 ${ev.winner ?? '平'}`
  if (ev.type === 'turn') return `轮到座${ev.player}`
  return ev.type || '?'
}

export default function ArenaWatch() {
  const { id: paramId } = useParams()
  const [sp] = useSearchParams()
  const id = paramId || sp.get('id') || ''
  const [status, setStatus] = useState('idle')
  const [gameId, setGameId] = useState('holdem')
  const pb = usePlayback(1)
  const logRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!id) return
    pb.clear()
    setStatus('connecting')
    void apiGet<{ match: { game_id?: string } }>(`/api/matches/${encodeURIComponent(id)}`)
      .then((d) => setGameId(normalizeGameId(d.match?.game_id)))
      .catch(() => undefined)

    const es = new EventSource(`/api/matches/${encodeURIComponent(id)}/events`)
    es.onopen = () => setStatus('live')
    es.onmessage = (msg) => {
      try {
        const ev = JSON.parse(msg.data) as RawEvent
        if (ev.type === 'snapshot') {
          const m = ev.match as { game_id?: string } | undefined
          if (m?.game_id) setGameId(normalizeGameId(m.game_id))
          const hist = Array.isArray(ev.events) ? (ev.events as RawEvent[]) : []
          pb.setAll(hist.slice(-400))
        } else {
          if (ev.type === 'match_start' && ev.game_id) setGameId(normalizeGameId(String(ev.game_id)))
          pb.append(ev)
        }
        if (ev.type === 'match_end' || ev.type === 'error') {
          setStatus(String(ev.type))
          es.close()
        } else {
          setStatus('live')
        }
      } catch {
        /* ignore */
      }
    }
    es.onerror = () => {
      setStatus((s) => (s === 'match_end' ? s : 'error'))
      es.close()
    }
    return () => es.close()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id])

  // 动作日志自动滚动到当前步
  useEffect(() => {
    const c = logRef.current
    if (!c) return
    const active = c.querySelector('[data-active="true"]')
    if (active) active.scrollIntoView({ block: 'nearest' })
  }, [pb.cur])

  const GameIcon = gameIcon(gameId)
  const events = pb.buffer as RawEvent[]
  const isBoard = isBoardGame(gameId)

  if (!id) {
    return (
      <PageStub title="观赛">
        <Card className="mt-4 overflow-hidden border-primary/20">
          <CardContent className="flex flex-col items-center gap-3 bg-gradient-to-br from-primary/5 via-card to-card px-4 py-16 text-center">
            <Radio className="size-10 text-primary/60" />
            <p className="text-lg font-medium tracking-wide text-foreground">对局观赛区</p>
            <p className="text-sm text-muted-foreground">选择对局后可在此 SSE 实时观战</p>
            <Button asChild variant="outline" size="sm" className="mt-2 gap-1.5">
              <Link to="/history">从对局历史选择<ArrowRight className="size-3.5" /></Link>
            </Button>
          </CardContent>
        </Card>
      </PageStub>
    )
  }

  return (
    <PageStub title="实时观赛">
      {/* 状态条 */}
      <div className="mb-4 flex flex-wrap items-center gap-2 text-sm">
        <span className="font-mono text-xs text-muted-foreground">{id}</span>
        <Badge variant="secondary" className="gap-1">
          <GameIcon className="size-3" />
          {gameLabel(gameId)}
        </Badge>
        <Badge variant={STATUS_VARIANT[status] ?? 'secondary'} className="gap-1">
          {status === 'live' && <span className="size-1.5 animate-pulse rounded-full bg-current" />}
          {STATUS_LABEL[status] ?? status}
        </Badge>
        <Button asChild variant="ghost" size="sm" className="ml-auto gap-1">
          <Link to={`/match/${id}`}>详情页<ArrowRight className="size-3.5" /></Link>
        </Button>
      </div>

      {events.length === 0 ? (
        <Loading text={status === 'connecting' ? '连接中…' : '暂无事件'} />
      ) : (
        <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_22rem]">
          {/* 左：棋盘/牌桌 + 控制条 */}
          <div className="space-y-3">
            <MatchBoard gameId={gameId} events={pb.visible as RawEvent[]} revealMode="all" />
            <Card>
              <CardContent className="py-3">
                <PlaybackControls
                  cur={pb.cur} total={pb.total} playing={pb.playing} speedIdx={pb.speedIdx}
                  atLive={pb.atLive} lag={pb.lag} isBoard={isBoard}
                  onTogglePlay={pb.togglePlay} onStep={pb.step} onSeek={pb.seek}
                  onSpeedChange={pb.setSpeedIdx}
                />
              </CardContent>
            </Card>
          </div>

          {/* 右：动作时序日志 */}
          <Card className="flex flex-col">
            <CardHeader className="pb-2">
              <CardTitle className="flex items-center gap-2 text-sm">
                动作时序
                <Badge variant="secondary" className="text-[10px]">{pb.total}</Badge>
              </CardTitle>
            </CardHeader>
            <CardContent className="flex-1 overflow-hidden p-0">
              <div ref={logRef} className="max-h-[60vh] overflow-y-auto px-3 pb-3 text-xs">
                {events.map((ev, i) => (
                  <div
                    key={i}
                    data-active={i === pb.cur}
                    className={`flex items-center gap-2 rounded px-2 py-1 ${
                      i === pb.cur ? 'bg-primary/10 font-medium text-primary' : 'text-muted-foreground'
                    }`}
                  >
                    <span className="w-7 shrink-0 text-right font-mono opacity-50">{i + 1}</span>
                    <span className="w-12 shrink-0 opacity-60">{ev.type}</span>
                    <span className="min-w-0 flex-1 truncate">{eventDesc(ev)}</span>
                  </div>
                ))}
              </div>
            </CardContent>
          </Card>
        </div>
      )}
    </PageStub>
  )
}
