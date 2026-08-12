import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { apiGet, apiJson, errMsg } from '../../api'
import { fmtTime } from '../../lib/format'
import { Button, Table, TableHeader, TableBody, TableHead, TableRow, TableCell,  EmptyState, Loading, ErrorMsg, RefreshBtn, Select, SelectContent, SelectItem, SelectTrigger, SelectValue, Tooltip, TooltipContent, TooltipTrigger } from './ui'
import Pagination from '@/components/Pagination'
import { OverflowText } from '@/components/ui/overflow-text'
import { Input } from '@/components/ui/input'
import { useConfirm } from '@/hooks/use-confirm'
import { toast } from 'sonner'

interface User {
  id: number
  username: string
  email: string
  role: string
  display_name?: string
  is_active: boolean
  email_verified: boolean
  created_at?: string
  last_login_at?: string | null
  // 实名信息（仅 admin 可见；后端 list_users 返回 SELECT *，字段已在数据里）
  real_name?: string
  phone?: string
  school?: string
  student_id?: string
}

/** 是否已完成实名（4 项全填非空，与后端 contests/manager.py 报名校验口径一致）。 */
function hasRealName(u: User): boolean {
  return Boolean(
    (u.real_name || '').trim() &&
      (u.phone || '').trim() &&
      (u.school || '').trim() &&
      (u.student_id || '').trim(),
  )
}

/** 实名详情文本（Tooltip 展示用）。 */
function realNameDetail(u: User): string {
  return [
    `手机：${u.phone || '—'}`,
    `学校：${u.school || '—'}`,
    `学号：${u.student_id || '—'}`,
  ].join('\n')
}

export default function UsersTab() {
  const [confirm, confirmDialog] = useConfirm()
  const [users, setUsers] = useState<User[]>([])
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [q, setQ] = useState('')
  // 实名筛选：'all' = 不过滤（哨兵，符合项目 Select 规范——空串被 Radix 当占位）
  const [realNameFilter, setRealNameFilter] = useState<'all' | 'yes' | 'no'>('all')
  const [busyId, setBusyId] = useState<number | null>(null)
  // 分页
  const [page, setPage] = useState(1)
  const [total, setTotal] = useState(0)
  const perPage = 20

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const d = await apiGet<{ users: User[]; total?: number }>(
        `/api/admin/users?page=${page}&per_page=${perPage}` +
          `${q ? `&q=${encodeURIComponent(q)}` : ''}` +
          `${realNameFilter === 'all' ? '' : `&real_name=${realNameFilter === 'yes'}`}`,
      )
      setUsers(d.users || [])
      if (d.total !== undefined) setTotal(d.total)
    } catch (e) {
      setError(errMsg(e, '加载失败'))
    } finally {
      setLoading(false)
    }
  }, [page, q, realNameFilter])

  useEffect(() => {
    void load()
  }, [load])

  const patch = async (uid: number, fields: Record<string, unknown>, msg?: string) => {
    setBusyId(uid)
    setError('')
    try {
      await apiJson(`/api/admin/users/${uid}`, 'PATCH', fields)
      await load()
    } catch (e) {
      setError(errMsg(e, msg || '操作失败'))
    } finally {
      setBusyId(null)
    }
  }

  const setRole = (uid: number, role: string) => patch(uid, { role }, '设置角色失败')
  const toggleActive = (u: User) => patch(u.id, { is_active: !u.is_active }, '操作失败')

  const revokeSessions = async (uid: number) => {
    setBusyId(uid)
    try {
      await apiJson(`/api/admin/users/${uid}/sessions`, 'DELETE')
      toast.success('已强制下线该用户全部会话')
    } catch (e) {
      setError(errMsg(e, '操作失败'))
    } finally {
      setBusyId(null)
    }
  }

  const delUser = async (uid: number) => {
    if (!await confirm({
      title: '删除用户',
      desc: `确认删除内部用户 ID ${uid}？若存在活跃对局或赛事引用，后端会拒绝。`,
      confirmText: '删除',
      danger: true,
    })) return
    setBusyId(uid)
    try {
      await apiJson(`/api/admin/users/${uid}`, 'DELETE')
      await load()
    } catch (e) {
      setError(errMsg(e, '删除失败'))
    } finally {
      setBusyId(null)
    }
  }

  const filtered = users.filter((u) => {
    // 文本搜索（用户名/邮箱）
    const matchQ =
      !q ||
      u.username.toLowerCase().includes(q.toLowerCase()) ||
      u.email.toLowerCase().includes(q.toLowerCase())
    // 实名筛选
    const matchRealName =
      realNameFilter === 'all' ||
      (realNameFilter === 'yes' ? hasRealName(u) : !hasRealName(u))
    return matchQ && matchRealName
  })

  if (loading && !users.length) return <Loading />
  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <Input
          value={q}
          onChange={(e) => {
            setQ(e.target.value)
            setPage(1)
          }}
          placeholder="搜索用户名/邮箱"
          className="h-9 w-auto min-w-48"
        />
        <Select
          value={realNameFilter}
          onValueChange={(v) => {
            setRealNameFilter(v as 'all' | 'yes' | 'no')
            setPage(1)
          }}
        >
          <SelectTrigger size="sm" className="h-9 w-32 text-sm">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">全部用户</SelectItem>
            <SelectItem value="yes">已实名</SelectItem>
            <SelectItem value="no">未实名</SelectItem>
          </SelectContent>
        </Select>
        <span className="text-xs text-muted-foreground">共 {total || filtered.length} 人</span>
        <div className="ml-auto">
          <RefreshBtn onClick={load} />
        </div>
      </div>
      <ErrorMsg msg={error} />

      <div className="overflow-x-auto rounded-xl border border-border bg-card">
        <Table className="min-w-[48rem]">
          <TableHeader>
            <TableRow>
              <TableHead className="px-3 py-2.5">序号</TableHead>
              <TableHead className="px-3 py-2.5">用户名</TableHead>
              <TableHead className="px-3 py-2.5">邮箱</TableHead>
              <TableHead className="px-3 py-2.5">实名</TableHead>
              <TableHead className="px-3 py-2.5">角色</TableHead>
              <TableHead className="px-3 py-2.5">状态</TableHead>
              <TableHead className="px-3 py-2.5">注册时间</TableHead>
              <TableHead className="px-3 py-2.5">操作</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {filtered.map((u, index) => (
              <TableRow key={u.id} className="hover:bg-accent">
                <TableCell className="px-3 py-2 font-mono tabular-nums text-muted-foreground">{(page - 1) * perPage + index + 1}</TableCell>
                <TableCell className="px-3 py-2">
                  <Link to={`/user/${encodeURIComponent(u.username)}`} className="font-medium text-primary hover:underline">
                    {u.username}
                  </Link>
                </TableCell>
                <TableCell className="max-w-[16rem] px-3 py-2 text-muted-foreground">
                  <OverflowText>{u.email}</OverflowText>
                </TableCell>
                <TableCell className="px-3 py-2 text-sm">
                  {hasRealName(u) ? (
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <span className="cursor-help underline decoration-dotted underline-offset-2">
                          {u.real_name}
                        </span>
                      </TooltipTrigger>
                      <TooltipContent className="whitespace-pre-line text-xs">
                        {realNameDetail(u)}
                      </TooltipContent>
                    </Tooltip>
                  ) : (
                    <span className="text-muted-foreground">—</span>
                  )}
                </TableCell>
                <TableCell className="px-3 py-2">
                  <Select
                    value={u.role}
                    disabled={busyId === u.id}
                    onValueChange={(v) => void setRole(u.id, v)}
                  >
                    <SelectTrigger size="sm" className="h-8 w-full text-xs">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="user">user</SelectItem>
                      <SelectItem value="organizer">organizer</SelectItem>
                      <SelectItem value="admin">admin</SelectItem>
                    </SelectContent>
                  </Select>
                </TableCell>
                <TableCell className="px-3 py-2 text-xs">
                  {u.email_verified ? (
                    <span className="text-success">已验证</span>
                  ) : (
                    <span className="text-muted-foreground">未验证</span>
                  )}
                  {u.is_active ? (
                    ''
                  ) : (
                    <span className="ml-1 text-destructive">· 停用</span>
                  )}
                </TableCell>
                <TableCell className="px-3 py-2 text-xs text-muted-foreground">{fmtTime(u.created_at)}</TableCell>
                <TableCell className="px-3 py-2">
                  <div className="flex flex-wrap gap-1">
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      disabled={busyId === u.id}
                      onClick={() => void toggleActive(u)}
                    >
                      {u.is_active ? '停用' : '启用'}
                    </Button>
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      disabled={busyId === u.id}
                      onClick={() => void revokeSessions(u.id)}
                    >
                      下线
                    </Button>
                    <Button type="button" variant="destructive" size="sm" disabled={busyId === u.id} onClick={() => void delUser(u.id)}>删除</Button>
                  </div>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
        {filtered.length === 0 && <EmptyState text="无用户" />}
      </div>
      <Pagination page={page} perPage={perPage} total={total} onPageChange={setPage} />
      {confirmDialog}
    </div>
  )
}
