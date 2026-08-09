import { Fragment, useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { AlertTriangle, CalendarClock } from 'lucide-react'

import { apiGet, apiJson, errMsg } from '../../api'
import {
  Badge,
  Button,
  EmptyState,
  ErrorMsg,
  Loading,
  RefreshBtn,
  StatusBadge,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from './ui'
import Pagination from '@/components/Pagination'
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
import { Switch } from '@/components/ui/switch'
import { useConfirm } from '@/hooks/use-confirm'
import { fmtTime } from '@/lib/format'
import { findGame, gameLabel } from '@/lib/games'

interface Contest {
  id: number
  title: string
  organizer_id: number
  status: string
  created_at: string
  starts_at: string | null
  ends_at: string | null
  registration_opens_at?: string | null
  registration_closes_at?: string | null
  template_id?: string
  game_id?: string
}

interface Entry {
  id: number
  contest_id: number
  user_id: number
  bot_id: number
  username?: string
  bot_name?: string
  registered_at: string
}

interface LifecycleAction {
  label: string
  target: string
}

const PRIMARY_ACTION: Record<string, LifecycleAction> = {
  draft: { label: '开放报名', target: 'open' },
  open: { label: '截止报名并发布排期', target: 'published' },
  published: { label: '开始比赛', target: 'running' },
}

const ROSTER_MUTABLE = new Set(['draft', 'open'])
const CANCELLABLE = new Set(['draft', 'open', 'published'])
const DELETABLE = new Set(['draft', 'cancelled'])
type ScheduleField = 'registration_opens_at' | 'registration_closes_at' | 'starts_at'
const SCHEDULE_EDITABLE: Record<string, ReadonlySet<ScheduleField>> = {
  draft: new Set(['registration_opens_at', 'registration_closes_at', 'starts_at']),
  open: new Set(['registration_closes_at', 'starts_at']),
  published: new Set(['starts_at']),
}

function scheduleIssue(contest: {
  registration_opens_at?: string | null
  registration_closes_at?: string | null
  starts_at?: string | null
}): string | null {
  const opens = contest.registration_opens_at
  const closes = contest.registration_closes_at
  const starts = contest.starts_at
  if (opens && closes && new Date(opens).getTime() > new Date(closes).getTime()) {
    return '报名截止早于报名开放'
  }
  if (closes && starts && new Date(closes).getTime() > new Date(starts).getTime()) {
    return '报名截止晚于比赛开始'
  }
  if (opens && starts && new Date(opens).getTime() > new Date(starts).getTime()) {
    return '报名开放晚于比赛开始'
  }
  return null
}

function toInputValue(value?: string | null): string {
  return value ? value.slice(0, 16) : ''
}

function toIso(value: string): string | null {
  if (!value) return null
  return value.length === 16 ? `${value}:00` : value
}

export default function ContestsTab() {
  const [confirm, confirmDialog] = useConfirm()
  const [contests, setContests] = useState<Contest[]>([])
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [busyId, setBusyId] = useState<number | null>(null)
  const [expand, setExpand] = useState<number | null>(null)
  const [entries, setEntries] = useState<Entry[]>([])
  const [entriesLoading, setEntriesLoading] = useState(false)
  const [scheduleContest, setScheduleContest] = useState<Contest | null>(null)
  const entriesRequestSeq = useRef(0)
  const [page, setPage] = useState(1)
  const [total, setTotal] = useState(0)
  const perPage = 20

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const response = await apiGet<{ contests: Contest[]; total?: number }>(
        `/api/admin/contests?page=${page}&per_page=${perPage}`,
      )
      setContests(response.contests || [])
      if (response.total !== undefined) setTotal(response.total)
    } catch (cause) {
      setError(errMsg(cause, '加载失败'))
    } finally {
      setLoading(false)
    }
  }, [page])

  useEffect(() => {
    void load()
  }, [load])

  const patch = async (id: number, fields: Record<string, unknown>) => {
    setBusyId(id)
    setError('')
    try {
      await apiJson(`/api/admin/contests/${id}`, 'PATCH', fields)
      await load()
    } catch (cause) {
      setError(errMsg(cause, '操作失败'))
      throw cause
    } finally {
      setBusyId(null)
    }
  }

  const runPrimary = async (contest: Contest) => {
    const action = PRIMARY_ACTION[contest.status]
    if (!action) return
    if (action.target === 'running') {
      const ok = await confirm({
        title: '开始比赛',
        desc: `将立即派发「${contest.title}」已到排期的对局。确认开始？`,
        confirmText: '开始比赛',
      })
      if (!ok) return
    }
    await patch(contest.id, { status: action.target }).catch(() => undefined)
  }

  const cancelContest = async (contest: Contest) => {
    const ok = await confirm({
      title: '取消锦标赛',
      desc: `取消「${contest.title}」后不能恢复；已完成和进行中的赛事不允许取消。`,
      confirmText: '取消赛事',
      danger: true,
    })
    if (!ok) return
    await patch(contest.id, { status: 'cancelled' }).catch(() => undefined)
  }

  const forceFinish = async (contest: Contest) => {
    const ok = await confirm({
      title: '恢复性结束赛事',
      desc: '仅用于全部关联对局已经终态、但自动推进未收敛的故障恢复。仍有活跃对局时后端会拒绝。',
      confirmText: '确认结束',
      danger: true,
    })
    if (!ok) return
    await patch(contest.id, { status: 'finished' }).catch(() => undefined)
  }

  const resumeContest = async (contest: Contest) => {
    setBusyId(contest.id)
    setError('')
    try {
      await apiJson(`/api/contests/${contest.id}/resume`, 'POST')
      await load()
    } catch (cause) {
      setError(errMsg(cause, '进入下一阶段失败'))
    } finally {
      setBusyId(null)
    }
  }

  const del = async (contest: Contest) => {
    const cancelled = contest.status === 'cancelled'
    const ok = await confirm({
      title: cancelled ? '清理已取消赛事' : '删除锦标赛草稿',
      desc: cancelled
        ? `将清理未产生正式成绩的已取消赛事「${contest.title}」。该操作不能撤销。`
        : `将删除尚未发布排期的草稿「${contest.title}」。该操作不能撤销。`,
      confirmText: cancelled ? '清理赛事' : '删除草稿',
      danger: true,
    })
    if (!ok) return
    setBusyId(contest.id)
    setError('')
    try {
      await apiJson(`/api/admin/contests/${contest.id}`, 'DELETE')
      await load()
    } catch (cause) {
      setError(errMsg(cause, '删除失败'))
    } finally {
      setBusyId(null)
    }
  }

  const loadEntries = async (contestId: number) => {
    const seq = ++entriesRequestSeq.current
    setEntriesLoading(true)
    try {
      const response = await apiGet<{ entries: Entry[] }>(`/api/admin/contests/${contestId}/entries`)
      if (seq === entriesRequestSeq.current && expand === contestId) {
        setEntries(response.entries || [])
      }
    } catch (cause) {
      if (seq === entriesRequestSeq.current) setError(errMsg(cause, '加载名册失败'))
    } finally {
      if (seq === entriesRequestSeq.current) setEntriesLoading(false)
    }
  }

  const showEntries = (contest: Contest) => {
    if (expand === contest.id) {
      ++entriesRequestSeq.current
      setExpand(null)
      setEntries([])
      return
    }
    setExpand(contest.id)
    setEntries([])
    // React state 尚未提交，loadEntries 的 authority 不能依赖旧 expand 值；本次 ID
    // 由 request sequence 保证，响应时再用 ref-less状态会丢首个结果，因此直接加载并
    // 在调用处提交。后续快速切换仍由 sequence 丢弃旧响应。
    const seq = ++entriesRequestSeq.current
    setEntriesLoading(true)
    void apiGet<{ entries: Entry[] }>(`/api/admin/contests/${contest.id}/entries`)
      .then((response) => {
        if (seq === entriesRequestSeq.current) setEntries(response.entries || [])
      })
      .catch((cause) => {
        if (seq === entriesRequestSeq.current) setError(errMsg(cause, '加载名册失败'))
      })
      .finally(() => {
        if (seq === entriesRequestSeq.current) setEntriesLoading(false)
      })
  }

  const removeEntry = async (contestId: number, userId: number) => {
    const ok = await confirm({
      title: '移除报名',
      desc: `从当前名册移除用户 #${userId}？`,
      confirmText: '移除',
      danger: true,
    })
    if (!ok) return
    try {
      await apiJson(`/api/admin/contests/${contestId}/entries/${userId}`, 'DELETE')
      await loadEntries(contestId)
    } catch (cause) {
      setError(errMsg(cause, '移除失败'))
    }
  }

  if (loading && !contests.length) return <Loading />
  return (
    <div>
      <div className="mb-3 flex items-center justify-between gap-3">
        <span className="text-xs text-muted-foreground">
          共 {total || contests.length} 个锦标赛；生命周期操作会经过后端状态机校验。
        </span>
        <RefreshBtn onClick={load} />
      </div>
      <ErrorMsg msg={error} />

      <div className="overflow-x-auto rounded-xl border border-border bg-card">
        <Table className="min-w-[64rem]">
          <TableHeader>
            <TableRow>
              <TableHead className="px-3 py-2.5">ID</TableHead>
              <TableHead className="px-3 py-2.5">标题</TableHead>
              <TableHead className="px-3 py-2.5">游戏 / 模板</TableHead>
              <TableHead className="px-3 py-2.5">状态</TableHead>
              <TableHead className="px-3 py-2.5">时间编排</TableHead>
              <TableHead className="px-3 py-2.5">创建时间</TableHead>
              <TableHead className="px-3 py-2.5">当前阶段操作</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {contests.map((contest) => {
              const primary = PRIMARY_ACTION[contest.status]
              const mutableRoster = ROSTER_MUTABLE.has(contest.status)
              const timeIssue = scheduleIssue(contest)
              return (
                <Fragment key={contest.id}>
                  <TableRow className={timeIssue ? 'bg-destructive/5 hover:bg-destructive/10' : 'hover:bg-accent'}>
                    <TableCell className="px-3 py-2 font-mono text-muted-foreground">{contest.id}</TableCell>
                    <TableCell className="max-w-64 px-3 py-2 font-medium text-foreground">
                      <Link to={`/contests/${contest.id}`} className="block break-words text-primary hover:underline">{contest.title}</Link>
                    </TableCell>
                    <TableCell className="px-3 py-2 text-xs text-muted-foreground">
                      <div className="text-foreground">{gameLabel(contest.game_id)}</div>
                      <div className="font-mono">{contest.template_id || '未指定模板'}</div>
                    </TableCell>
                    <TableCell className="px-3 py-2"><StatusBadge status={contest.status} /></TableCell>
                    <TableCell className="px-3 py-2 text-xs text-muted-foreground">
                      <div>开放报名：{contest.registration_opens_at ? fmtTime(contest.registration_opens_at) : '手动'}</div>
                      <div>报名截止：{contest.registration_closes_at ? fmtTime(contest.registration_closes_at) : '手动'}</div>
                      <div className="font-medium text-foreground">
                        比赛开始：{contest.starts_at ? fmtTime(contest.starts_at) : '手动'}
                      </div>
                      {timeIssue && (
                        <div className="mt-1 flex items-center gap-1 text-destructive">
                          <AlertTriangle className="size-3.5" />{timeIssue}
                        </div>
                      )}
                    </TableCell>
                    <TableCell className="px-3 py-2 text-xs text-muted-foreground">{fmtTime(contest.created_at)}</TableCell>
                    <TableCell className="px-3 py-2">
                      <div className="flex flex-wrap gap-1.5">
                        {primary && (
                          <Button type="button" size="sm" disabled={busyId === contest.id} onClick={() => void runPrimary(contest)}>
                            {primary.label}
                          </Button>
                        )}
                        {contest.status === 'rest' && (
                          <Button type="button" size="sm" disabled={busyId === contest.id} onClick={() => void resumeContest(contest)}>
                            进入下一阶段
                          </Button>
                        )}
                        {(contest.status === 'running' || contest.status === 'rest') && (
                          <Button type="button" variant="destructive" size="sm" disabled={busyId === contest.id} onClick={() => void forceFinish(contest)}>
                            恢复性结束
                          </Button>
                        )}
                        {CANCELLABLE.has(contest.status) && (
                          <Button type="button" variant="outline" size="sm" disabled={busyId === contest.id} onClick={() => void cancelContest(contest)} className="text-destructive">
                            取消赛事
                          </Button>
                        )}
                        <Button type="button" variant="outline" size="sm" onClick={() => showEntries(contest)}>
                          {mutableRoster ? '管理名册' : '查看名册'}
                        </Button>
                        {SCHEDULE_EDITABLE[contest.status] && (
                          <Button type="button" variant="outline" size="sm" onClick={() => setScheduleContest(contest)}>
                            <CalendarClock className="size-3.5" />{timeIssue ? '修正时间' : '编辑时间'}
                          </Button>
                        )}
                        {DELETABLE.has(contest.status) && (
                          <Button type="button" variant="destructive" size="sm" disabled={busyId === contest.id} onClick={() => void del(contest)}>
                            {contest.status === 'cancelled' ? '清理已取消赛事' : '删除草稿'}
                          </Button>
                        )}
                        {contest.status === 'finished' && (
                          <Badge variant="secondary" className="self-center">成绩已归档 · 只读</Badge>
                        )}
                        {contest.status === 'cancelled' && (
                          <Badge variant="secondary" className="self-center">已取消 · 可清理</Badge>
                        )}
                      </div>
                    </TableCell>
                  </TableRow>
                  {expand === contest.id && (
                    <TableRow key={`${contest.id}-entries`} className="bg-muted/60">
                      <TableCell colSpan={7} className="px-6 py-4">
                        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                          <div>
                            <h3 className="text-sm font-medium text-foreground">参赛名册</h3>
                            <p className="text-xs text-muted-foreground">
                              {mutableRoster ? '草稿和报名阶段可以调整；发布排期后名册只读。' : '当前阶段名册只读，避免已发布对阵与参赛者不一致。'}
                            </p>
                          </div>
                          <Link to={`/contests/${contest.id}`} className="text-xs text-primary hover:underline">查看赛事详情</Link>
                        </div>
                        {mutableRoster && (
                          <AssignPanel contestId={contest.id} gameId={contest.game_id} onDone={() => void loadEntries(contest.id)} />
                        )}
                        {entriesLoading ? (
                          <Loading text="加载名册…" />
                        ) : entries.length === 0 ? (
                          <EmptyState text="暂无报名" />
                        ) : (
                          <Table className="min-w-[48rem]">
                            <TableHeader>
                              <TableRow>
                                <TableHead className="px-2 py-1">用户</TableHead>
                                <TableHead className="px-2 py-1">Bot</TableHead>
                                <TableHead className="px-2 py-1">报名时间</TableHead>
                                {mutableRoster && <TableHead className="px-2 py-1">操作</TableHead>}
                              </TableRow>
                            </TableHeader>
                            <TableBody>
                              {entries.map((entry) => (
                                <TableRow key={entry.id} className="text-muted-foreground">
                                  <TableCell className="px-2 py-1">
                                    {entry.username
                                      ? <Link to={`/user/${encodeURIComponent(entry.username)}`} className="text-primary hover:underline">{entry.username}</Link>
                                      : `用户 #${entry.user_id}`}
                                  </TableCell>
                                  <TableCell className="px-2 py-1">
                                    <Link to={`/bot/${entry.bot_id}`} className="text-primary hover:underline">{entry.bot_name || `Bot #${entry.bot_id}`}</Link>
                                  </TableCell>
                                  <TableCell className="px-2 py-1">{fmtTime(entry.registered_at)}</TableCell>
                                  {mutableRoster && (
                                    <TableCell className="px-2 py-1">
                                      <Button type="button" variant="destructive" size="xs" onClick={() => void removeEntry(contest.id, entry.user_id)}>移除</Button>
                                    </TableCell>
                                  )}
                                </TableRow>
                              ))}
                            </TableBody>
                          </Table>
                        )}
                      </TableCell>
                    </TableRow>
                  )}
                </Fragment>
              )
            })}
          </TableBody>
        </Table>
        {contests.length === 0 && <EmptyState text="暂无锦标赛" />}
      </div>
      <Pagination page={page} perPage={perPage} total={total} onPageChange={setPage} />
      {scheduleContest && (
        <ScheduleDialog
          key={scheduleContest.id}
          contest={scheduleContest}
          busy={busyId === scheduleContest.id}
          onClose={() => setScheduleContest(null)}
          onSave={async (fields) => {
            await patch(scheduleContest.id, fields)
            setScheduleContest(null)
          }}
        />
      )}
      {confirmDialog}
    </div>
  )
}

function ScheduleDialog({
  contest,
  busy,
  onClose,
  onSave,
}: {
  contest: Contest
  busy: boolean
  onClose: () => void
  onSave: (fields: Record<string, string | null>) => Promise<void>
}) {
  const editable = SCHEDULE_EDITABLE[contest.status] || new Set<ScheduleField>()
  const [opensAt, setOpensAt] = useState(toInputValue(contest.registration_opens_at))
  const [closesAt, setClosesAt] = useState(toInputValue(contest.registration_closes_at))
  const [startsAt, setStartsAt] = useState(toInputValue(contest.starts_at))
  const [autoStart, setAutoStart] = useState(Boolean(contest.starts_at))
  const [saveError, setSaveError] = useState('')
  const fullCandidate: Record<ScheduleField, string | null> = {
    registration_opens_at: toIso(opensAt),
    registration_closes_at: toIso(closesAt),
    starts_at: autoStart ? toIso(startsAt) : null,
  }
  const candidate = Object.fromEntries(
    [...editable].map((field) => [field, fullCandidate[field]]),
  ) as Record<string, string | null>
  const orderIssue = scheduleIssue({ ...contest, ...fullCandidate })
  const futureIssue = contest.status === 'open'
    ? [
        ['报名截止', fullCandidate.registration_closes_at],
        ['比赛开始', fullCandidate.starts_at],
      ].find((entry) => entry[1] && new Date(entry[1]).getTime() <= Date.now())?.[0]
    : undefined
  const issue = autoStart && !startsAt
    ? '选择自动开赛后必须填写比赛开始时间'
    : futureIssue
      ? `报名中赛事的${futureIssue}必须晚于当前时间，或清空为手动`
      : orderIssue

  const updateValue = (setter: (value: string) => void, value: string) => {
    setSaveError('')
    setter(value)
  }

  const save = async () => {
    if (issue) return
    // onSave already surfaces API failures in the parent error panel. Keep the
    // dialog open for correction, but do not leak a rejected promise from the
    // fire-and-forget button handler into the browser console.
    setSaveError('')
    try {
      // 只发送当前阶段仍可修改的字段；其中空输入必须显式发送 null，
      // 否则管理员无法把自动开赛恢复为手动。
      await onSave(candidate)
    } catch (cause) {
      // 页面顶层保留聚合错误，同时 Dialog 内直接显示，避免管理员在长表格
      // 底部保存失败后还要退出弹窗才能发现原因。
      setSaveError(errMsg(cause, '保存失败'))
    }
  }

  return (
    <Dialog open onOpenChange={(open) => { if (!open && !busy) onClose() }}>
      <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto break-words">
        <DialogHeader>
          <DialogTitle>编辑赛事时间</DialogTitle>
          <DialogDescription>
            {contest.title} · 空时间表示对应阶段由组织者手动推进。已生效的阶段时间只读；自动开赛时应满足“开放报名 ≤ 报名截止 ≤ 比赛开始”。
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          {editable.has('registration_opens_at') ? (
            <div className="space-y-1.5">
              <Label htmlFor="admin-registration-opens-at">开放报名</Label>
              <Input id="admin-registration-opens-at" type="datetime-local" value={opensAt} onChange={(event) => updateValue(setOpensAt, event.target.value)} />
            </div>
          ) : (
            <div className="space-y-1.5">
              <Label>开放报名（已生效，只读）</Label>
              <p className="rounded-md bg-muted px-3 py-2 text-sm text-muted-foreground">
                {contest.registration_opens_at ? fmtTime(contest.registration_opens_at) : '手动开放'}
              </p>
            </div>
          )}
          {editable.has('registration_closes_at') ? (
            <div className="space-y-1.5">
              <Label htmlFor="admin-registration-closes-at">报名截止</Label>
              <Input id="admin-registration-closes-at" type="datetime-local" value={closesAt} onChange={(event) => updateValue(setClosesAt, event.target.value)} />
            </div>
          ) : (
            <div className="space-y-1.5">
              <Label>报名截止（已发布，只读）</Label>
              <p className="rounded-md bg-muted px-3 py-2 text-sm text-muted-foreground">
                {contest.registration_closes_at ? fmtTime(contest.registration_closes_at) : '手动截止'}
              </p>
            </div>
          )}
          <div className="space-y-3 rounded-lg border border-border p-3">
            <div className="flex items-start justify-between gap-4">
              <div className="space-y-1">
                <Label htmlFor="admin-auto-start">按时间自动开赛</Label>
                <p className="text-xs text-muted-foreground">
                  关闭后保存为手动开赛；系统不会在报名截止后立即启动比赛。
                </p>
              </div>
              <Switch
                id="admin-auto-start"
                checked={autoStart}
                onCheckedChange={(checked) => {
                  setSaveError('')
                  setAutoStart(checked)
                }}
                disabled={busy}
              />
            </div>
            {autoStart ? (
              <div className="space-y-1.5">
                <Label htmlFor="admin-starts-at">比赛开始</Label>
                <Input
                  id="admin-starts-at"
                  type="datetime-local"
                  value={startsAt}
                  onChange={(event) => updateValue(setStartsAt, event.target.value)}
                />
              </div>
            ) : (
              <p className="rounded-md bg-muted px-3 py-2 text-sm text-muted-foreground">
                比赛开始：手动。发布排期后等待组织者点击“开始比赛”。
              </p>
            )}
          </div>
          {issue && (
            <p className="flex items-center gap-1.5 text-sm text-destructive"><AlertTriangle className="size-4" />{issue}</p>
          )}
          <ErrorMsg msg={saveError} />
        </div>
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose} disabled={busy}>取消</Button>
          <Button type="button" onClick={() => void save()} disabled={busy || Boolean(issue)}>{busy ? '保存中…' : '保存时间'}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function AssignPanel({ contestId, gameId, onDone }: { contestId: number; gameId?: string; onDone: () => void }) {
  const [query, setQuery] = useState('')
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const gameSpec = findGame(gameId)

  const assignAll = async () => {
    if (!gameSpec) {
      setMessage('该赛事的游戏类型不受支持，无法批量指派。')
      return
    }
    setBusy(true)
    setMessage('')
    try {
      const response = await apiJson<{ added: number; skipped: string[]; total_entries: number }>(
        `/api/admin/contests/${contestId}/entries/bulk`,
        'POST',
        { assign_all: true, game_id: gameSpec.id, name_prefix: query || undefined },
      )
      setMessage(`已指派 ${response.added} 人（共 ${response.total_entries} 人${response.skipped.length ? `，跳过 ${response.skipped.length}` : ''}）`)
      onDone()
    } catch (cause) {
      setMessage(errMsg(cause))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="mb-3 flex flex-wrap items-center gap-2 rounded-lg border border-border bg-card p-3 text-xs">
      <span className="font-medium text-foreground">批量指派</span>
      <Input
        type="text"
        placeholder="按 Bot 名关键字过滤（可选）"
        value={query}
        onChange={(event) => setQuery(event.target.value)}
        className="h-9 w-64"
      />
      <Button type="button" size="sm" onClick={() => void assignAll()} disabled={busy || !gameSpec}>
        {busy ? '指派中…' : `指派全部 ${gameLabel(gameId)} Bot`}
      </Button>
      {message && <span className="text-muted-foreground">{message}</span>}
    </div>
  )
}
