import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Swords } from 'lucide-react'
import PageStub from '@/components/PageStub'
import { Card } from '@/components/ui/card'
import { EmptyState, ErrorMsg, StatusBadge } from '@/components/ui/status'
import { apiGet, errMsg } from '@/api'
import { GAMES, gameLabel } from '@/lib/games'

interface Match {
  id: string
  status: string
  bot_a_id: number
  bot_b_id: number
  bot_a_name?: string
  bot_b_name?: string
  bot_a_display?: string
  bot_b_display?: string
  earnings_a?: number
  earnings_b?: number
  created_at?: string
  hands_played?: number
  total_hands?: number
  match_type?: string
  game_id?: string
}

const selectCls =
  'ml-2 h-9 rounded-md border border-input bg-transparent px-3 text-sm text-foreground shadow-xs focus:outline-none focus:ring-2 focus:ring-ring'

export default function History() {
  const [matches, setMatches] = useState<Match[]>([])
  const [status, setStatus] = useState('')
  const [gameId, setGameId] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    const params = new URLSearchParams({ limit: '100' })
    if (status) params.set('status', status)
    if (gameId) params.set('game_id', gameId)
    apiGet<{ matches: Match[] }>(`/api/matches?${params}`)
      .then((d) => setMatches(d.matches || []))
      .catch((e) => setError(errMsg(e)))
  }, [status, gameId])

  return (
    <PageStub
      title="对局历史"
      subtitle="全部对局记录，可按状态与游戏筛选"
      actions={
        <div className="flex flex-wrap gap-4">
          <label className="flex items-center text-sm text-muted-foreground">
            状态
            <select
              value={status}
              onChange={(e) => setStatus(e.target.value)}
              className={selectCls}
            >
              <option value="">全部</option>
              <option value="pending">排队中</option>
              <option value="running">进行中</option>
              <option value="completed">已完成</option>
              <option value="aborted">已中止</option>
            </select>
          </label>
          <label className="flex items-center text-sm text-muted-foreground">
            游戏
            <select
              value={gameId}
              onChange={(e) => setGameId(e.target.value)}
              className={selectCls}
            >
              <option value="">全部</option>
              {GAMES.map((g) => (
                <option key={g.id} value={g.id}>
                  {g.label}
                </option>
              ))}
            </select>
          </label>
        </div>
      }
    >
      {error && <ErrorMsg msg={error} className="mb-3" />}
      <Card className="gap-0 py-0">
        {matches.length === 0 ? (
          <EmptyState text="暂无对局" icon={<Swords className="size-7 opacity-40" />} />
        ) : (
          <ul className="divide-y divide-border">
            {matches.map((m) => (
              <li key={m.id} className="flex flex-wrap items-center gap-3 px-4 py-3">
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2 font-medium text-foreground">
                    <Link to={`/bot/${m.bot_a_id}`} className="hover:text-primary">
                      {m.bot_a_display || m.bot_a_name || `#${m.bot_a_id}`}
                    </Link>
                    <span className="text-muted-foreground">vs</span>
                    <Link to={`/bot/${m.bot_b_id}`} className="hover:text-primary">
                      {m.bot_b_display || m.bot_b_name || `#${m.bot_b_id}`}
                    </Link>
                  </div>
                  <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                    <span>{gameLabel(m.game_id)}</span>
                    <span>·</span>
                    <StatusBadge status={m.status} />
                    {m.created_at && (
                      <>
                        <span>·</span>
                        <span>{m.created_at}</span>
                      </>
                    )}
                  </div>
                </div>
                <Link className="text-sm font-medium text-primary hover:underline" to={`/match/${m.id}`}>
                  {(m.status === 'running' || m.status === 'pending') ? '观赛' : '打开'}
                </Link>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </PageStub>
  )
}
