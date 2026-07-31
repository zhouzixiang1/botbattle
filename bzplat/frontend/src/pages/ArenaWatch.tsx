import { useEffect, useRef, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import PageStub from '../components/PageStub'
import PokerTable from '../components/poker/PokerTable'
import { useMatchState, type RawEvent } from '../components/poker/useMatchState'

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
  const endRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!id) return
    setEvents([])
    setStatus('connecting')
    const es = new EventSource(`/api/matches/${encodeURIComponent(id)}/events`)
    es.onopen = () => setStatus('live')
    es.onmessage = (msg) => {
      try {
        const ev = JSON.parse(msg.data) as RawEvent
        setEvents((prev) => [...prev, ev].slice(-400))
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
  }, [id])

  useEffect(() => {
    if (showLog) endRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [events, showLog])

  const vm = useMatchState(events)
  // 用最新事件的状态覆盖
  vm.status = status

  if (!id) {
    return (
      <PageStub title="观赛">
        <div className="felt-table mt-4 overflow-hidden rounded-2xl px-4 py-16 text-center text-white/90">
          <p className="text-lg font-medium tracking-wide">牌桌观赛区</p>
          <p className="mt-2 text-sm text-white/60">选择对局后可在此 SSE 实时观战</p>
          <p className="mt-4">
            <Link to="/history" className="text-brand-200 hover:text-brand-100">
              从对局历史选择 →
            </Link>
          </p>
        </div>
      </PageStub>
    )
  }

  return (
    <PageStub title="实时观赛">
      <p className="mb-4 text-sm text-slate-400">
        <span className="font-mono text-brand-300">{id}</span>
        {' · '}状态{' '}
        <span className={status === 'error' ? 'text-error-400' : 'text-brand-300'}>
          {STATUS_LABEL[status] ?? status}
        </span>
      </p>

      {events.length === 0 ? (
        <div className="felt-table rounded-2xl px-4 py-16 text-center text-white/70">
          {status === 'connecting' ? '连接中…' : '暂无事件'}
        </div>
      ) : (
        <PokerTable vm={vm} revealMode="all" />
      )}

      {/* 原始事件流（调试用，可折叠） */}
      <div className="mt-4">
        <button
          type="button"
          onClick={() => setShowLog((v) => !v)}
          className="text-xs text-slate-500 hover:text-slate-700"
        >
          {showLog ? '▼' : '▶'} 原始事件流（{events.length}）
        </button>
        {showLog && (
          <div className="mt-2 max-h-80 overflow-y-auto rounded-lg bg-slate-900/60 p-3 font-mono text-xs text-white/70">
            {events.map((ev, i) => (
              <div key={i} className="border-b border-white/5 py-1">
                <span className="mr-2 text-brand-200">{String(ev.type || '?')}</span>
                <span className="break-all">{JSON.stringify(ev)}</span>
              </div>
            ))}
            <div ref={endRef} />
          </div>
        )}
      </div>

      <Link
        className="mt-4 inline-block text-sm text-brand-600 hover:text-brand-700"
        to={`/match/${id}`}
      >
        查看详情页 →
      </Link>
    </PageStub>
  )
}
