import { useEffect, useState } from 'react'
import { ArrowLeft, Bot as BotIcon, Trophy, User as UserIcon } from 'lucide-react'
import { apiGet, errMsg } from '@/api'
import { gameLabel, type GameId } from '@/lib/games'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
import { EmptyState, ErrorMsg, Loading } from '@/components/ui/status'
import Pagination from '@/components/Pagination'
import { cn } from '@/lib/utils'

export interface PickBot {
  id: number
  name: string
  display_name?: string
  owner_id?: number
  owner_name?: string
  owner_display?: string
  game_id?: string
  is_active?: number | boolean
  is_ranked?: number | boolean
  runnable?: boolean
}

interface User {
  id: number
  username: string
  display_name?: string
}

type Tab = 'all' | 'mine' | 'users'

/**
 * 对手选择大弹窗（参考 botzone）：搜索框 + 列表合一。
 * - "全部/我的"：按 bot 搜索（名称），点选即定为对手。
 * - "按用户"：先搜用户，再展示该用户该游戏的 bot。
 */
export default function OpponentPickerModal({
  gameId,
  myUserId,
  mineOnly = false,
  onClose,
  onPick,
}: {
  gameId: GameId
  myUserId?: number
  /** 普通用户发起 Bot-vs-Bot 时座位 1 必须属于本人；管理员由调用方传 false。 */
  mineOnly?: boolean
  onClose: () => void
  onPick: (bot: PickBot) => void
}) {
  const [tab, setTab] = useState<Tab>(mineOnly ? 'mine' : 'all')
  const [q, setQ] = useState('')
  const [bots, setBots] = useState<PickBot[]>([])
  const [users, setUsers] = useState<User[]>([])
  const [selUser, setSelUser] = useState<User | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  // 分页（picker 弹窗每页较大，避免频繁翻页；q 仍为客户端过滤当前页）
  const [page, setPage] = useState(1)
  const [total, setTotal] = useState(0)
  const perPage = 50

  // bot 列表（全部 / 我的 / 选定用户的）——服务端分页
  useEffect(() => {
    if (tab === 'users' && !selUser) return
    setLoading(true)
    const params = new URLSearchParams({ game_id: gameId })
    if (tab === 'mine' || mineOnly) params.set('owner_id', String(myUserId ?? ''))
    if (tab === 'users' && selUser) params.set('owner_id', String(selUser.id))
    params.set('page', String(page))
    params.set('per_page', String(perPage))
    apiGet<{ bots: PickBot[]; total?: number }>(`/api/bots/public?${params.toString()}`)
      .then((d) => {
        let rows = (d.bots || []).filter((b) => b.is_active !== 0 && b.runnable !== false)
        if (tab === 'mine' || mineOnly) rows = rows.filter((b) => b.owner_id === myUserId)
        setBots(rows)
        if (d.total !== undefined) setTotal(d.total)
      })
      .catch((e) => setError(errMsg(e, '加载失败')))
      .finally(() => setLoading(false))
  }, [gameId, tab, selUser, page, myUserId, mineOnly])

  // 用户搜索
  useEffect(() => {
    if (tab !== 'users') return
    const t = setTimeout(() => {
      if (!q.trim()) {
        setUsers([])
        return
      }
      apiGet<{ users: User[] }>(`/api/users?q=${encodeURIComponent(q.trim())}`)
        .then((d) => setUsers(d.users || []))
        .catch(() => setUsers([]))
    }, 250)
    return () => clearTimeout(t)
  }, [q, tab])

  const tabBtn = (t: Tab) =>
    cn(
      'rounded-full px-3 py-1 text-xs font-medium transition-colors max-sm:min-h-[44px]',
      tab === t ? 'bg-primary text-primary-foreground' : 'bg-muted text-muted-foreground hover:bg-accent'
    )

  return (
    <Dialog open onOpenChange={(o) => { if (!o) onClose() }}>
      <DialogContent className="max-w-2xl gap-0 p-0">
        <DialogHeader className="border-b border-border px-5 py-3">
          <DialogTitle className="flex items-center gap-2 text-base">
            <BotIcon className="size-4 text-primary" />
            选择对手
            <Badge variant="secondary" className="text-[10px]">{gameLabel(gameId)}</Badge>
          </DialogTitle>
        </DialogHeader>

        {/* 搜索框 + tab */}
        <div className="border-b border-border px-5 py-3">
          <Input
            autoFocus
            value={q}
            onChange={(e) => {
              setQ(e.target.value)
              if (tab === 'users') { setSelUser(null); setPage(1) }
            }}
            placeholder={tab === 'users' ? '搜索用户名…' : mineOnly ? '搜索我的 Bot 名称…' : '搜索 Bot 名称…'}
          />
          {!mineOnly && (
            <div className="mt-2 flex gap-2">
              {([['all', '全部 Bot'], ['mine', '我的 Bot（自博弈）'], ['users', '按用户搜索']] as [Tab, string][]).map(
                ([t, label]) => (
                  <button key={t} type="button" onClick={() => { setTab(t); setSelUser(null); setPage(1) }} className={tabBtn(t)}>
                    {label}
                  </button>
                ),
              )}
            </div>
          )}
          <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
            排行榜 Bot 与练习 Bot 均可挑战；只有符合平台计分规则的排行榜 Bot 对局才会产生新评分。
          </p>
        </div>

        {/* 内容区 */}
        <div className="max-h-[50vh] overflow-auto px-5 py-3">
          {error && <ErrorMsg msg={error} className="py-4" />}

          {/* 按用户：先显示用户列表 */}
          {tab === 'users' && !selUser && (
            users.length === 0 ? (
              <EmptyState text={q.trim() ? '无匹配用户' : '输入用户名前缀搜索…'} icon={<UserIcon className="size-7 opacity-40" />} />
            ) : (
              <ul className="divide-y divide-border">
                {users.map((u) => (
                  <li key={u.id}>
                    <button
                      type="button"
                      onClick={() => { setSelUser(u); setPage(1) }}
                      className="flex min-h-[44px] w-full items-center justify-between px-2 py-2.5 text-left transition-colors hover:bg-accent"
                    >
                      <span className="text-sm font-medium text-foreground">{u.display_name || u.username}</span>
                      <span className="text-xs text-muted-foreground">@{u.username}</span>
                    </button>
                  </li>
                ))}
              </ul>
            )
          )}

          {tab === 'users' && selUser && (
            <div className="mb-2 flex items-center gap-2 text-sm">
              <button type="button" onClick={() => setSelUser(null)} className="inline-flex min-h-[44px] items-center gap-1 text-primary hover:underline">
                <ArrowLeft className="size-3.5" />返回用户搜索
              </button>
              <span className="text-muted-foreground">已选：{selUser.display_name || selUser.username}</span>
            </div>
          )}

          {/* bot 列表（q 为客户端过滤，仅作用于当前页） */}
          {!(tab === 'users' && !selUser) && (
            loading ? (
              <Loading />
            ) : bots.length === 0 ? (
              <EmptyState text="无可选 Bot" icon={<BotIcon className="size-7 opacity-40" />} />
            ) : (
              <>
                <ul className="divide-y divide-border">
                  {bots
                    .filter((b) => {
                      if (tab === 'users') return true
                      if (!q.trim()) return true
                      return (b.name + (b.display_name || '')).toLowerCase().includes(q.toLowerCase())
                    })
                    .map((b) => (
                      <li key={b.id}>
                        <button
                          type="button"
                          onClick={() => onPick(b)}
                          className="flex min-h-[44px] w-full items-center justify-between gap-3 px-2 py-2.5 text-left transition-colors hover:bg-primary/5"
                        >
                          <span className="flex min-w-0 flex-wrap items-center gap-1.5 text-sm font-medium text-foreground">
                            <span className="min-w-0 break-words [overflow-wrap:anywhere]">{b.display_name || b.name}</span>
                            {tab === 'mine' && <Badge variant="outline" className="ml-2 text-[10px]">自博弈</Badge>}
                            <Badge variant={b.is_ranked ? 'default' : 'outline'} className="text-[10px]">
                              <Trophy className="size-3" aria-hidden="true" />
                              {b.is_ranked ? '排行榜 Bot' : '练习 Bot'}
                            </Badge>
                          </span>
                          <span className="max-w-[45%] shrink-0 break-words text-right text-xs text-muted-foreground [overflow-wrap:anywhere]">
                            {b.owner_display || b.owner_name || '所属用户不可用'}
                          </span>
                        </button>
                      </li>
                    ))}
                </ul>
                <Pagination page={page} perPage={perPage} total={total} onPageChange={setPage} />
              </>
            )
          )}
        </div>
      </DialogContent>
    </Dialog>
  )
}
