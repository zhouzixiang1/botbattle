import { useEffect, useRef, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { Radio, ChevronRight, ChevronDown, Terminal, ArrowRight } from 'lucide-react'
import PageStub from '@/components/PageStub'
import MatchBoard from '@/components/MatchBoard'
import { type RawEvent } from '@/components/poker/useMatchState'
import { Card, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Loading } from '@/components/ui/status'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { apiGet } from '@/api'
import { gameLabel, gameIcon, normalizeGameId } from '@/lib/games'

const STATUS_VARIANT: Record<string, 'default' | 'secondary' | 'destructive' | 'outline'> = {
  idle: 'secondary',
  connecting: 'secondary',
  live: 'default',
  match_end: 'outline',
  error: 'destructive',
}
const STATUS_LABEL: Record<string, string> = {
  idle: '空闲',
  connecting: '连接中',
  live: '直播中',
  match_end: '已结束',
  error: '出错',
}

export default function ArenaWatch() {
  const { id: paramId } = useParams()
  const [sp] = useSearchParams()
  const id = paramId || sp.get('id') || ''
  const [events, setEvents] = useState<RawEvent[]>([])
  const [status, setStatus] = useState('idle')
  const [showLog, setShowLog] = useState(false)
  const [gameId, setGameId] = useState('holdem')
  const endRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!id) return
    setEvents([])
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
          setEvents(hist.slice(-400))
        } else {
          if (ev.type === 'match_start' && ev.game_id) {
            setGameId(normalizeGameId(String(ev.game_id)))
          }
          setEvents((prev) => [...prev, ev].slice(-400))
        }
        if (ev.type === 'match_end' || ev.type === 'error') {
          setStatus(String(ev.type))
          if (ev.type === 'match_end' || ev.type === 'error') es.close()
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
  }, [id])

  useEffect(() => {
    if (showLog) endRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [events, showLog])

  const GameIcon = gameIcon(gameId)

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
      </div>

      {events.length === 0 ? (
        <Loading text={status === 'connecting' ? '连接中…' : '暂无事件'} />
      ) : (
        <MatchBoard gameId={gameId} events={events} revealMode="all" />
      )}

      {/* 原始事件流（折叠） */}
      <Collapsible open={showLog} onOpenChange={setShowLog} className="mt-4">
        <CollapsibleTrigger asChild>
          <Button variant="ghost" size="sm" className="gap-1.5 text-muted-foreground">
            {showLog ? <ChevronDown className="size-4" /> : <ChevronRight className="size-4" />}
            <Terminal className="size-3.5" />
            原始事件流（{events.length}）
          </Button>
        </CollapsibleTrigger>
        <CollapsibleContent>
          <div className="mt-2 max-h-80 overflow-y-auto rounded-lg border border-border bg-muted/40 p-3 font-mono text-xs">
            {events.map((ev, i) => (
              <div key={i} className="border-b border-border/50 py-1">
                <span className="mr-2 font-semibold text-primary">{String(ev.type || '?')}</span>
                <span className="break-all text-muted-foreground">{JSON.stringify(ev)}</span>
              </div>
            ))}
            <div ref={endRef} />
          </div>
        </CollapsibleContent>
      </Collapsible>

      <Button asChild variant="ghost" size="sm" className="mt-4 gap-1.5">
        <Link to={`/match/${id}`}>查看详情页<ArrowRight className="size-3.5" /></Link>
      </Button>
    </PageStub>
  )
}
