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
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { EmptyState, ErrorMsg, Loading } from '@/components/ui/status'
import Pagination from '@/components/Pagination'

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
export type PickerPurpose = 'mine' | 'initiator' | 'opponent'

/**
 * Bot 选择大弹窗（参考 botzone）：搜索框 + 列表合一。
 * - "全部/我的"：按 bot 搜索（名称），点选后用于调用方声明的位置。
 * - "按用户"：先搜用户，再展示该用户该游戏的 bot。
 */
export default function OpponentPickerModal({
  gameId,
  myUserId,
  mineOnly = false,
  purpose,
  onClose,
  onPick,
}: {
  gameId: GameId
  myUserId?: number
  /** 普通用户发起 Bot-vs-Bot 时“我的 Bot”位置只允许本人 Bot；管理员由调用方传 false。 */
  mineOnly?: boolean
  /** 选择器的语义用途独立于过滤范围；管理员发起方可看全站 Bot。 */
  purpose: PickerPurpose
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
  const dialogTitle = purpose === 'mine'
    ? '选择我的 Bot'
    : purpose === 'initiator'
      ? '选择发起方 Bot'
      : '选择对手 Bot'
  const searchPlaceholder = purpose === 'mine'
    ? '搜索我的 Bot 名称…'
    : purpose === 'initiator'
      ? '搜索发起方 Bot 名称…'
      : '搜索 Bot 名称…'

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

  return (
    <Dialog open onOpenChange={(o) => { if (!o) onClose() }}>
      <DialogContent className="max-w-2xl gap-0 p-0">
        <DialogHeader className="border-b border-border px-5 py-3">
          <DialogTitle className="flex items-center gap-2 text-base">
            <BotIcon className="size-4 text-primary" />
            {dialogTitle}
            <Badge variant="secondary" className="text-xs">{gameLabel(gameId)}</Badge>
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
            placeholder={tab === 'users' ? '搜索用户名…' : searchPlaceholder}
          />
          {!mineOnly && (
            <Tabs
              value={tab}
              onValueChange={(value) => {
                setTab(value as Tab)
                setSelUser(null)
                setPage(1)
              }}
              className="mt-2"
            >
              <TabsList>
                <TabsTrigger value="all">全部 Bot</TabsTrigger>
                <TabsTrigger value="mine">我的 Bot</TabsTrigger>
                <TabsTrigger value="users">按用户搜索</TabsTrigger>
              </TabsList>
            </Tabs>
          )}
          <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
            只有排行榜 Bot 对局会产生新评分。
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
                            {tab === 'mine' && <Badge variant="outline" className="ml-2 text-xs">自博弈</Badge>}
                            <Badge variant={b.is_ranked ? 'default' : 'outline'} className="text-xs">
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
