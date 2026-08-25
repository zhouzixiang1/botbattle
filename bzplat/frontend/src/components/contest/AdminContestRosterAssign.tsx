import { useEffect, useMemo, useRef, useState } from 'react'
import { Bot, Search, Trash2, UserPlus, Users } from 'lucide-react'
import { toast } from 'sonner'

import { apiGet, apiJson, errMsg } from '@/api'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { useConfirm } from '@/hooks/use-confirm'
import { findGame, gameLabel } from '@/lib/games'
import { cn } from '@/lib/utils'

interface AdminUser {
  id: number
  username: string
  display_name?: string | null
  is_active: boolean | number
}

interface AdminBot {
  id: number
  owner_id: number
  name: string
  display_name?: string | null
  current_version?: number
  runnable?: boolean
}

interface StagedAssignment {
  user: AdminUser
  botId: number
  bots: AdminBot[]
}

interface AssignResponse {
  added: number
  skipped: string[]
  total_entries: number
}

interface AdminContestRosterAssignProps {
  contestId: number
  gameId?: string
  existingUserIds?: readonly number[]
  onDone: () => void | Promise<void>
  className?: string
}

const ASSIGN_ALL_CONFIRM_DELAY_MS = 250

function userLabel(user: AdminUser): string {
  const display = (user.display_name || '').trim()
  return display && display !== user.username
    ? `${display}（@${user.username}）`
    : `@${user.username}`
}

function botLabel(bot: AdminBot): string {
  const display = (bot.display_name || '').trim()
  const name = display && display !== bot.name ? `${display}（${bot.name}）` : bot.name
  return bot.current_version ? `${name} · v${bot.current_version}` : name
}

export function AdminContestRosterAssign({
  contestId,
  gameId,
  existingUserIds = [],
  onDone,
  className,
}: AdminContestRosterAssignProps) {
  const game = findGame(gameId)
  const [confirm, confirmDialog] = useConfirm()
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [users, setUsers] = useState<AdminUser[]>([])
  const [usersLoading, setUsersLoading] = useState(false)
  const [usersError, setUsersError] = useState('')
  const [userPage, setUserPage] = useState(1)
  const [userTotal, setUserTotal] = useState(0)
  const [rosterUserIds, setRosterUserIds] = useState<Set<number>>(
    () => new Set(existingUserIds),
  )
  const [rosterLoading, setRosterLoading] = useState(false)
  const [rosterReady, setRosterReady] = useState(false)
  const [rosterError, setRosterError] = useState('')
  const [rosterReloadKey, setRosterReloadKey] = useState(0)
  const [selectedUser, setSelectedUser] = useState<AdminUser | null>(null)
  const [availableBots, setAvailableBots] = useState<AdminBot[]>([])
  const [selectedBotId, setSelectedBotId] = useState('')
  const [botQuery, setBotQuery] = useState('')
  const [botPage, setBotPage] = useState(1)
  const [botTotal, setBotTotal] = useState(0)
  const [botsLoading, setBotsLoading] = useState(false)
  const [botsError, setBotsError] = useState('')
  const [botReloadKey, setBotReloadKey] = useState(0)
  const [userReloadKey, setUserReloadKey] = useState(0)
  const [staged, setStaged] = useState<StagedAssignment[]>([])
  const [submitIssues, setSubmitIssues] = useState<string[]>([])
  const [submitting, setSubmitting] = useState(false)
  const [preparingAll, setPreparingAll] = useState(false)
  const [assigningAll, setAssigningAll] = useState(false)
  const rosterRequestSeq = useRef(0)
  const userRequestSeq = useRef(0)
  const botRequestSeq = useRef(0)
  const actionLockRef = useRef(false)
  const busy = submitting || preparingAll || assigningAll
  const userTotalPages = Math.max(1, Math.ceil(userTotal / 20))
  const botTotalPages = Math.max(1, Math.ceil(botTotal / 50))

  const stagedUserIds = useMemo(
    () => new Set(staged.map((assignment) => assignment.user.id)),
    [staged],
  )
  const selectableUsers = users.filter(
    (user) => Boolean(user.is_active) && !rosterUserIds.has(user.id) && !stagedUserIds.has(user.id),
  )

  useEffect(() => {
    if (!open) return
    setQuery('')
    setUsers([])
    setUsersError('')
    setUserPage(1)
    setUserTotal(0)
    setRosterUserIds(new Set(existingUserIds))
    setRosterReady(false)
    setRosterError('')
    setSelectedUser(null)
    setAvailableBots([])
    setSelectedBotId('')
    setBotQuery('')
    setBotPage(1)
    setBotTotal(0)
    setBotsError('')
    setStaged([])
    setSubmitIssues([])
    return () => {
      ++rosterRequestSeq.current
      ++userRequestSeq.current
      ++botRequestSeq.current
    }
    // Opening the dialog is the authority boundary. Parent roster refreshes are
    // picked up the next time it opens, without resetting a mapping mid-edit.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, contestId])

  useEffect(() => {
    if (!open) return
    const seq = ++rosterRequestSeq.current
    setRosterLoading(true)
    setRosterReady(false)
    setRosterError('')
    void apiGet<{ entries: Array<{ user_id: number }> }>(
      `/api/admin/contests/${contestId}/entries?identity=false`,
    )
      .then((response) => {
        if (rosterRequestSeq.current !== seq) return
        const next = new Set(response.entries.map((entry) => entry.user_id))
        setRosterUserIds(next)
        setStaged((current) => current.filter((assignment) => !next.has(assignment.user.id)))
        setRosterReady(true)
      })
      .catch((cause) => {
        if (rosterRequestSeq.current !== seq) return
        setRosterError(errMsg(cause, '核对现有名册失败'))
      })
      .finally(() => {
        if (rosterRequestSeq.current === seq) setRosterLoading(false)
      })
  }, [contestId, open, rosterReloadKey])

  useEffect(() => {
    if (!open) return
    const seq = ++userRequestSeq.current
    setUsersLoading(true)
    setUsersError('')
    setUsers([])
    const timer = window.setTimeout(() => {
      const params = new URLSearchParams({ active: 'true', page: String(userPage), per_page: '20' })
      const trimmed = query.trim()
      if (trimmed) params.set('q', trimmed)
      void apiGet<{ users: AdminUser[]; total?: number }>(`/api/admin/users?${params.toString()}`)
        .then((response) => {
          if (userRequestSeq.current !== seq) return
          const next = response.users || []
          const total = response.total ?? next.length
          const totalPages = Math.max(1, Math.ceil(total / 20))
          setUserTotal(total)
          if (userPage > totalPages) {
            setUserPage(totalPages)
            return
          }
          setUsers(next)
        })
        .catch((cause) => {
          if (userRequestSeq.current !== seq) return
          setUsers([])
          setUserTotal(0)
          setUsersError(errMsg(cause, '搜索用户失败'))
        })
        .finally(() => {
          if (userRequestSeq.current === seq) setUsersLoading(false)
        })
    }, 250)
    return () => window.clearTimeout(timer)
  }, [open, query, userPage, userReloadKey])

  useEffect(() => {
    if (!open || !selectedUser || !game) return
    const seq = ++botRequestSeq.current
    setBotsLoading(true)
    setBotsError('')
    setAvailableBots([])
    setSelectedBotId('')
    const timer = window.setTimeout(() => {
      const params = new URLSearchParams({
        owner_id: String(selectedUser.id),
        active: 'true',
        runnable: 'true',
        game_id: game.id,
        page: String(botPage),
        per_page: '50',
      })
      const trimmed = botQuery.trim()
      if (trimmed) params.set('q', trimmed)
      void apiGet<{ bots: AdminBot[]; total?: number }>(`/api/admin/bots?${params.toString()}`)
        .then((response) => {
          if (botRequestSeq.current !== seq) return
          const next = (response.bots || []).filter(
            (bot) => bot.owner_id === selectedUser.id && bot.runnable === true,
          )
          const total = response.total ?? next.length
          const totalPages = Math.max(1, Math.ceil(total / 50))
          setBotTotal(total)
          if (botPage > totalPages) {
            setBotPage(totalPages)
            return
          }
          setAvailableBots(next)
          if (next.length === 1) setSelectedBotId(String(next[0].id))
        })
        .catch((cause) => {
          if (botRequestSeq.current !== seq) return
          setBotTotal(0)
          setBotsError(errMsg(cause, '加载该用户的 Bot 失败'))
        })
        .finally(() => {
          if (botRequestSeq.current === seq) setBotsLoading(false)
        })
    }, 250)
    return () => window.clearTimeout(timer)
  }, [botPage, botQuery, botReloadKey, game, open, selectedUser])

  const chooseUser = (candidate: AdminUser) => {
    setSelectedUser(candidate)
    setBotQuery('')
    setBotPage(1)
    setBotTotal(0)
  }

  const addSelected = () => {
    if (!rosterReady || busy || !selectedUser || !selectedBotId) return
    const botId = Number(selectedBotId)
    if (!availableBots.some((bot) => bot.id === botId)) return
    setStaged((current) => [
      ...current,
      { user: selectedUser, botId, bots: availableBots },
    ])
    setSelectedUser(null)
    setAvailableBots([])
    setSelectedBotId('')
    setBotQuery('')
    setBotPage(1)
    setBotTotal(0)
    setBotsError('')
    setSubmitIssues([])
  }

  const replaceBot = (userId: number, value: string) => {
    if (busy) return
    const botId = Number(value)
    setStaged((current) => current.map((assignment) => (
      assignment.user.id === userId && assignment.bots.some((bot) => bot.id === botId)
        ? { ...assignment, botId }
        : assignment
    )))
    setSubmitIssues([])
  }

  const removeStaged = (userId: number) => {
    if (busy) return
    setStaged((current) => current.filter((assignment) => assignment.user.id !== userId))
    setSubmitIssues([])
  }

  const submit = async () => {
    if (!game || !rosterReady || staged.length === 0 || busy || actionLockRef.current) return
    actionLockRef.current = true
    setSubmitting(true)
    setSubmitIssues([])
    try {
      const response = await apiJson<AssignResponse>(
        `/api/admin/contests/${contestId}/entries/bulk`,
        'POST',
        {
          entries: staged.map((assignment) => ({
            user_id: assignment.user.id,
            bot_id: assignment.botId,
          })),
        },
      )
      await onDone()
      if (response.skipped.length > 0) {
        let remaining = staged
        try {
          const roster = await apiGet<{ entries: Array<{ user_id: number }> }>(
            `/api/admin/contests/${contestId}/entries?identity=false`,
          )
          const currentRoster = new Set(roster.entries.map((entry) => entry.user_id))
          setRosterUserIds(currentRoster)
          remaining = staged.filter((assignment) => !currentRoster.has(assignment.user.id))
        } catch {
          const mappedFailures = staged.filter((assignment) => response.skipped.some((reason) => (
            reason.includes(`user ${assignment.user.id}`) || reason.includes(`bot ${assignment.botId}`)
          )))
          if (mappedFailures.length > 0) remaining = mappedFailures
        }
        setStaged(remaining)
        setSelectedUser(null)
        setAvailableBots([])
        setSelectedBotId('')
        setBotQuery('')
        setBotPage(1)
        setBotTotal(0)
        setSubmitIssues(response.skipped)
        toast.warning(
          response.added > 0
            ? `已指派 ${response.added} 人；请处理 ${response.skipped.length} 条未加入项`
            : `没有新增参赛者；请处理 ${response.skipped.length} 条未加入项`,
        )
        return
      }
      setOpen(false)
      toast.success(`已指派 ${response.added} 人`)
    } catch (cause) {
      toast.error(errMsg(cause, '指派失败'))
    } finally {
      actionLockRef.current = false
      setSubmitting(false)
    }
  }

  const assignAll = async () => {
    if (!game || busy || actionLockRef.current) return
    actionLockRef.current = true
    setPreparingAll(true)
    try {
      // Keep the trigger in place until a pointer double-click sequence has
      // finished, so its second click cannot land on and dismiss the overlay.
      await new Promise<void>((resolve) => {
        window.setTimeout(resolve, ASSIGN_ALL_CONFIRM_DELAY_MS)
      })
      setPreparingAll(false)
      const ok = await confirm({
        title: '指派全部可用用户？',
        desc: `将为所有尚未报名、且拥有可运行 ${gameLabel(game.id)} Bot 的用户各指派一个 Bot。请只在确实需要全员参赛时使用。`,
        confirmText: '确认全员指派',
        buttonClassName: 'max-sm:min-h-11',
      })
      if (!ok) return
      setAssigningAll(true)
      const response = await apiJson<AssignResponse>(
        `/api/admin/contests/${contestId}/entries/bulk`,
        'POST',
        { assign_all: true, game_id: game.id },
      )
      await onDone()
      if (response.skipped.length) {
        toast.warning(
          response.added > 0
            ? `已指派 ${response.added} 人，跳过 ${response.skipped.length} 人`
            : `没有新增参赛者，跳过 ${response.skipped.length} 人`,
        )
      } else {
        toast.success(`已指派 ${response.added} 人`)
      }
    } catch (cause) {
      toast.error(errMsg(cause, '全员指派失败'))
    } finally {
      actionLockRef.current = false
      setPreparingAll(false)
      setAssigningAll(false)
    }
  }

  return (
    <>
      <div
        data-testid="admin-contest-roster-assign"
        className={cn(
          'flex min-w-0 flex-col gap-2 rounded-lg border border-border bg-card p-3 sm:flex-row sm:items-center sm:justify-between',
          className,
        )}
      >
        <div className="min-w-0">
          <p className="font-medium text-foreground">指派参赛者</p>
          <p className="text-xs leading-relaxed text-muted-foreground">
            逐个核对用户与其 {gameLabel(gameId)} Bot；确认后一次加入名册。
          </p>
        </div>
        <div className="flex shrink-0 flex-col gap-2 sm:flex-row">
          <Button
            type="button"
            className="min-h-11 max-sm:w-full"
            disabled={!game || busy}
            onClick={() => setOpen(true)}
          >
            <UserPlus aria-hidden="true" className="size-4" />
            选择参赛用户与 Bot
          </Button>
          <Button
            type="button"
            variant="outline"
            className="min-h-11 max-sm:w-full"
            disabled={!game || busy}
            aria-busy={preparingAll || assigningAll}
            onClick={() => void assignAll()}
          >
            <Users aria-hidden="true" className="size-4" />
            {preparingAll ? '准备确认…' : assigningAll ? '指派中…' : '指派全部可用用户'}
          </Button>
        </div>
      </div>

      <Dialog open={open} onOpenChange={(next) => !busy && setOpen(next)}>
        <DialogContent className="max-h-[calc(100dvh-1.5rem)] overflow-y-auto sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>选择参赛用户与 Bot</DialogTitle>
            <DialogDescription>
              搜索用户后，只显示其当前可运行且与赛事同游戏的 Bot。待指派列表可在提交前逐条更换或移除。
            </DialogDescription>
          </DialogHeader>

          {rosterLoading && (
            <p className="rounded-md bg-muted px-3 py-2 text-sm text-muted-foreground" aria-live="polite">
              正在核对现有名册…
            </p>
          )}
          {rosterError && (
            <div className="flex flex-col gap-2 rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm sm:flex-row sm:items-center sm:justify-between" role="alert">
              <span className="text-destructive">{rosterError}。为避免重复指派，暂不能加入或提交。</span>
              <Button
                type="button"
                variant="outline"
                className="min-h-11 shrink-0"
                onClick={() => setRosterReloadKey((value) => value + 1)}
              >
                重新核对名册
              </Button>
            </div>
          )}

          <div className="grid min-w-0 gap-4 md:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
            <section className="min-w-0 space-y-2" aria-labelledby={`contest-${contestId}-user-search`}>
              <div className="space-y-1.5">
                <Label id={`contest-${contestId}-user-search`}>搜索参赛用户</Label>
                <div className="relative">
                  <Search aria-hidden="true" className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
                  <Input
                    value={query}
                    onChange={(event) => {
                      setQuery(event.target.value)
                      setUserPage(1)
                    }}
                    placeholder="输入用户名或邮箱"
                    aria-label="搜索参赛用户"
                    className="min-h-11 pl-9"
                    disabled={busy}
                    autoFocus
                  />
                </div>
              </div>
              <div className="flex min-h-6 flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground" aria-live="polite">
                <span>{usersLoading ? '正在搜索…' : `共 ${userTotal} 个活跃用户`}</span>
                {userTotalPages > 1 && (
                  <div className="flex items-center gap-1">
                    <Button
                      type="button"
                      variant="outline"
                      size="xs"
                      className="min-h-11 px-3"
                      disabled={busy || usersLoading || userPage <= 1}
                      onClick={() => setUserPage((page) => Math.max(1, page - 1))}
                    >
                      上一页用户
                    </Button>
                    <span className="px-1 tabular-nums">{userPage} / {userTotalPages}</span>
                    <Button
                      type="button"
                      variant="outline"
                      size="xs"
                      className="min-h-11 px-3"
                      disabled={busy || usersLoading || userPage >= userTotalPages}
                      onClick={() => setUserPage((page) => Math.min(userTotalPages, page + 1))}
                    >
                      下一页用户
                    </Button>
                  </div>
                )}
              </div>
              <div
                className="max-h-52 min-h-24 overflow-y-auto rounded-md border border-border p-1"
                data-scroll-region="contest-user-search-results"
                data-overflow-allowed="y"
                aria-live="polite"
              >
                {usersLoading ? (
                  <p className="px-3 py-4 text-sm text-muted-foreground">正在搜索…</p>
                ) : usersError ? (
                  <div className="space-y-2 px-3 py-3" role="alert">
                    <p className="text-sm text-destructive">{usersError}</p>
                    <Button
                      type="button"
                      variant="outline"
                      className="min-h-11"
                      onClick={() => setUserReloadKey((value) => value + 1)}
                    >
                      重新搜索
                    </Button>
                  </div>
                ) : selectableUsers.length === 0 ? (
                  <p className="px-3 py-4 text-sm leading-relaxed text-muted-foreground">
                    {users.length ? '当前结果没有可指派的活跃用户，或用户已在名册/待指派列表中。' : '没有匹配的用户。'}
                  </p>
                ) : (
                  <ul aria-label="可选择用户">
                    {selectableUsers.map((candidate) => {
                      const selected = selectedUser?.id === candidate.id
                      return (
                        <li key={candidate.id}>
                          <button
                            type="button"
                            disabled={!rosterReady || busy}
                            aria-pressed={selected}
                            aria-label={`选择用户 ${candidate.username}`}
                            onClick={() => chooseUser(candidate)}
                            className={cn(
                              'flex min-h-11 w-full min-w-0 touch-manipulation items-center justify-between gap-2 rounded-sm px-3 py-2 text-left text-sm outline-none hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50',
                              selected && 'bg-primary/10 text-primary',
                            )}
                          >
                            <span className="min-w-0 break-words [overflow-wrap:anywhere]">{userLabel(candidate)}</span>
                          </button>
                        </li>
                      )
                    })}
                  </ul>
                )}
              </div>

              <div className="space-y-1.5">
                {selectedUser && (
                  <p className="break-words rounded-md bg-muted px-3 py-2 text-sm text-foreground [overflow-wrap:anywhere]">
                    已选用户：<span className="font-medium">{userLabel(selectedUser)}</span>
                  </p>
                )}
                <Label>该用户的可运行 Bot</Label>
                <Input
                  value={botQuery}
                  onChange={(event) => {
                    setBotQuery(event.target.value)
                    setBotPage(1)
                  }}
                  placeholder="按 Bot 名称筛选"
                  aria-label="搜索该用户的 Bot"
                  className="min-h-11"
                  disabled={!rosterReady || busy || !selectedUser}
                />
                {selectedUser && (
                  <div className="flex min-h-6 flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground" aria-live="polite">
                    <span>{botsLoading ? '正在加载…' : `共 ${botTotal} 个可用 Bot`}</span>
                    {botTotalPages > 1 && (
                      <div className="flex items-center gap-1">
                        <Button
                          type="button"
                          variant="outline"
                          size="xs"
                          className="min-h-11 px-3"
                          disabled={busy || botsLoading || botPage <= 1}
                          onClick={() => setBotPage((page) => Math.max(1, page - 1))}
                        >
                          上一页
                        </Button>
                        <span className="px-1 tabular-nums">{botPage} / {botTotalPages}</span>
                        <Button
                          type="button"
                          variant="outline"
                          size="xs"
                          className="min-h-11 px-3"
                          disabled={busy || botsLoading || botPage >= botTotalPages}
                          onClick={() => setBotPage((page) => Math.min(botTotalPages, page + 1))}
                        >
                          下一页
                        </Button>
                      </div>
                    )}
                  </div>
                )}
                <Select
                  value={selectedBotId}
                  onValueChange={setSelectedBotId}
                  disabled={!rosterReady || busy || !selectedUser || botsLoading || availableBots.length === 0}
                >
                  <SelectTrigger className="min-h-11 w-full" aria-label="选择该用户的 Bot">
                    <SelectValue
                      placeholder={
                        !selectedUser
                          ? '先选择用户'
                          : botsLoading
                            ? '正在加载 Bot…'
                            : availableBots.length
                              ? '选择 Bot'
                              : '没有可用 Bot'
                      }
                    />
                  </SelectTrigger>
                  <SelectContent>
                    {availableBots.map((bot) => (
                      <SelectItem key={bot.id} value={String(bot.id)} className="min-h-11">
                        {botLabel(bot)}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                {botsError && (
                  <div className="space-y-2" role="alert">
                    <p className="text-sm text-destructive">{botsError}</p>
                    <Button
                      type="button"
                      variant="outline"
                      className="min-h-11"
                      onClick={() => setBotReloadKey((value) => value + 1)}
                    >
                      重新加载 Bot
                    </Button>
                  </div>
                )}
              </div>
              <Button
                type="button"
                variant="outline"
                className="min-h-11 w-full"
                disabled={!rosterReady || busy || !selectedUser || !selectedBotId || botsLoading}
                onClick={addSelected}
              >
                加入待指派
              </Button>
            </section>

            <section className="min-w-0 space-y-2" aria-labelledby={`contest-${contestId}-staged-title`}>
              <div className="flex min-h-6 items-center justify-between gap-2">
                <Label id={`contest-${contestId}-staged-title`}>待指派映射</Label>
                <span className="text-xs tabular-nums text-muted-foreground" aria-live="polite">
                  {staged.length} 人
                </span>
              </div>
              <div className="min-h-36 rounded-md border border-border md:min-h-52">
                {staged.length === 0 ? (
                  <div className="flex min-h-36 flex-col items-center justify-center gap-2 px-4 text-center text-sm text-muted-foreground md:min-h-52">
                    <Bot aria-hidden="true" className="size-6 opacity-50" />
                    <p>选择用户和 Bot 后，会在这里逐条列出。</p>
                  </div>
                ) : (
                  <ol aria-label="待指派用户与 Bot" className="divide-y divide-border">
                    {staged.map((assignment, index) => (
                      <li key={assignment.user.id} className="grid min-w-0 grid-cols-[minmax(0,1fr)_2.75rem] gap-2 p-2.5">
                        <div className="min-w-0 space-y-1.5">
                          <p className="break-words text-sm font-medium text-foreground [overflow-wrap:anywhere]">
                            <span className="mr-1 font-mono text-xs text-muted-foreground">{index + 1}.</span>
                            {userLabel(assignment.user)}
                          </p>
                          <Select
                            value={String(assignment.botId)}
                            onValueChange={(value) => replaceBot(assignment.user.id, value)}
                            disabled={busy}
                          >
                            <SelectTrigger
                              className="min-h-11 w-full"
                              aria-label={`更换 ${assignment.user.username} 的 Bot`}
                            >
                              <SelectValue />
                            </SelectTrigger>
                            <SelectContent>
                              {assignment.bots.map((bot) => (
                                <SelectItem key={bot.id} value={String(bot.id)} className="min-h-11">
                                  {botLabel(bot)}
                                </SelectItem>
                              ))}
                            </SelectContent>
                          </Select>
                        </div>
                        <Button
                          type="button"
                          variant="ghost"
                          size="icon-sm"
                          className="min-h-11 min-w-11 self-end text-destructive"
                          disabled={busy}
                          aria-label={`移除 ${assignment.user.username} 的待指派项`}
                          onClick={() => removeStaged(assignment.user.id)}
                        >
                          <Trash2 aria-hidden="true" className="size-4" />
                        </Button>
                      </li>
                    ))}
                  </ol>
                )}
              </div>
            </section>
          </div>

          {submitIssues.length > 0 && (
            <div className="rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2" role="alert">
              <p className="text-sm font-medium text-destructive">以下项目未加入，请核对后重试：</p>
              <ul className="mt-1 list-disc space-y-1 pl-5 text-sm text-destructive">
                {submitIssues.map((issue, index) => <li key={`${issue}-${index}`}>{issue}</li>)}
              </ul>
            </div>
          )}

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              className="min-h-11"
              disabled={busy}
              onClick={() => setOpen(false)}
            >
              取消
            </Button>
            <Button
              type="button"
              className="min-h-11"
              disabled={!game || !rosterReady || staged.length === 0 || busy}
              aria-busy={submitting}
              onClick={() => void submit()}
            >
              {submitting ? '指派中…' : `确认指派 ${staged.length} 人`}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      {confirmDialog}
    </>
  )
}
