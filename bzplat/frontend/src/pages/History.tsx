import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Swords, ChevronLeft, ChevronRight } from 'lucide-react'
import PageStub from '@/components/PageStub'
import { Card } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { EmptyState, ErrorMsg, StatusBadge } from '@/components/ui/status'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { apiGet, errMsg } from '@/api'
import { GAMES, gameLabel } from '@/lib/games'
import { fmtTime } from '@/lib/format'

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

const PAGE_SIZE = 20

export default function History() {
  const [matches, setMatches] = useState<Match[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(0)
  const [status, setStatus] = useState('')
  const [gameId, setGameId] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    const params = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(page * PAGE_SIZE) })
    if (status) params.set('status', status)
    if (gameId) params.set('game_id', gameId)
    apiGet<{ matches: Match[]; total: number }>(`/api/matches?${params}`)
      .then((d) => {
        setMatches(d.matches || [])
        setTotal(d.total ?? 0)
        setError('')
      })
      .catch((e) => setError(errMsg(e)))
  }, [status, gameId, page])

  // 筛选变化时回到第 1 页
  const onStatus = (v: string) => { setStatus(v); setPage(0) }
  const onGame = (v: string) => { setGameId(v); setPage(0) }

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE))
  const rangeStart = total === 0 ? 0 : page * PAGE_SIZE + 1
  const rangeEnd = Math.min(total, (page + 1) * PAGE_SIZE)

  return (
    <PageStub
      title="对局历史"
      subtitle="全部对局记录，可按状态与游戏筛选"
      actions={
        <div className="flex flex-wrap gap-4">
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            状态
            <Select value={status || 'all'} onValueChange={(v) => onStatus(v === 'all' ? '' : v)}>
              <SelectTrigger className="h-9 w-[8.5rem]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">全部</SelectItem>
                <SelectItem value="pending">排队中</SelectItem>
                <SelectItem value="running">进行中</SelectItem>
                <SelectItem value="completed">已完成</SelectItem>
                <SelectItem value="aborted">已中止</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            游戏
            <Select value={gameId || 'all'} onValueChange={(v) => onGame(v === 'all' ? '' : v)}>
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
                        <span>{fmtTime(m.created_at)}</span>
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

      {/* 分页器 */}
      {total > PAGE_SIZE && (
        <div className="mt-4 flex flex-wrap items-center justify-between gap-2 text-sm text-muted-foreground">
          <span>
            第 <span className="font-medium text-foreground">{rangeStart}-{rangeEnd}</span> 条，共 {total} 条
          </span>
          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              className="gap-1"
              disabled={page === 0}
              onClick={() => setPage((p) => Math.max(0, p - 1))}
            >
              <ChevronLeft className="size-4" />上一页
            </Button>
            <span className="font-mono text-xs">第 {page + 1} / {totalPages} 页</span>
            <Button
              variant="outline"
              size="sm"
              className="gap-1"
              disabled={page + 1 >= totalPages}
              onClick={() => setPage((p) => p + 1)}
            >
              下一页<ChevronRight className="size-4" />
            </Button>
          </div>
        </div>
      )}
    </PageStub>
  )
}
