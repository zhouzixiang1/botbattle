import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { apiGet, apiJson, errMsg } from '../../api'
import { fmtTime } from '../../lib/format'
import { EmptyState, Loading, ErrorMsg, RefreshBtn, Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from './ui'
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
}

export default function UsersTab() {
  const [users, setUsers] = useState<User[]>([])
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [q, setQ] = useState('')
  const [busyId, setBusyId] = useState<number | null>(null)
  const [confirmDel, setConfirmDel] = useState<number | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const d = await apiGet<{ users: User[] }>('/api/admin/users')
      setUsers(d.users || [])
    } catch (e) {
      setError(errMsg(e, '加载失败'))
    } finally {
      setLoading(false)
    }
  }, [])

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
    setBusyId(uid)
    try {
      await apiJson(`/api/admin/users/${uid}`, 'DELETE')
      setConfirmDel(null)
      await load()
    } catch (e) {
      setError(errMsg(e, '删除失败'))
    } finally {
      setBusyId(null)
    }
  }

  const filtered = users.filter(
    (u) =>
      !q ||
      u.username.toLowerCase().includes(q.toLowerCase()) ||
      u.email.toLowerCase().includes(q.toLowerCase()),
  )

  if (loading && !users.length) return <Loading />
  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="搜索用户名/邮箱"
          className="rounded-lg border border-input bg-background px-3 py-1.5 text-sm text-foreground focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50 focus:outline-none"
        />
        <span className="text-xs text-muted-foreground">共 {filtered.length} 人</span>
        <div className="ml-auto">
          <RefreshBtn onClick={load} />
        </div>
      </div>
      <ErrorMsg msg={error} />

      <div className="overflow-x-auto rounded-xl border border-border bg-card">
        <table className="w-full min-w-[48rem] text-left text-sm">
          <thead className="border-b border-border bg-muted text-xs uppercase text-muted-foreground">
            <tr>
              <th className="px-3 py-2.5">ID</th>
              <th className="px-3 py-2.5">用户名</th>
              <th className="px-3 py-2.5">邮箱</th>
              <th className="px-3 py-2.5">角色</th>
              <th className="px-3 py-2.5">状态</th>
              <th className="px-3 py-2.5">注册时间</th>
              <th className="px-3 py-2.5">操作</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {filtered.map((u) => (
              <tr key={u.id} className="hover:bg-accent">
                <td className="px-3 py-2 font-mono text-muted-foreground">{u.id}</td>
                <td className="px-3 py-2">
                  <Link to={`/user/${encodeURIComponent(u.username)}`} className="font-medium text-primary hover:underline">
                    {u.username}
                  </Link>
                </td>
                <td className="max-w-[16rem] truncate px-3 py-2 text-muted-foreground">
                  <span className="block truncate" title={u.email}>{u.email}</span>
                </td>
                <td className="px-3 py-2">
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
                </td>
                <td className="px-3 py-2 text-xs">
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
                </td>
                <td className="px-3 py-2 text-xs text-muted-foreground">{fmtTime(u.created_at)}</td>
                <td className="px-3 py-2">
                  <div className="flex flex-wrap gap-1">
                    <button
                      type="button"
                      disabled={busyId === u.id}
                      onClick={() => void toggleActive(u)}
                      className="rounded border border-input bg-card px-2 py-0.5 text-xs text-muted-foreground hover:bg-accent"
                    >
                      {u.is_active ? '停用' : '启用'}
                    </button>
                    <button
                      type="button"
                      disabled={busyId === u.id}
                      onClick={() => void revokeSessions(u.id)}
                      className="rounded border border-input bg-card px-2 py-0.5 text-xs text-muted-foreground hover:bg-accent"
                    >
                      下线
                    </button>
                    {confirmDel === u.id ? (
                      <>
                        <button
                          type="button"
                          disabled={busyId === u.id}
                          onClick={() => void delUser(u.id)}
                          className="rounded border border-destructive/30 bg-destructive/10 px-2 py-0.5 text-xs text-destructive hover:bg-destructive/20"
                        >
                          确认删除
                        </button>
                        <button
                          type="button"
                          onClick={() => setConfirmDel(null)}
                          className="rounded border border-input bg-card px-2 py-0.5 text-xs text-muted-foreground"
                        >
                          取消
                        </button>
                      </>
                    ) : (
                      <button
                        type="button"
                        disabled={busyId === u.id}
                        onClick={() => setConfirmDel(u.id)}
                        className="rounded border border-input bg-card px-2 py-0.5 text-xs text-destructive hover:bg-destructive/10"
                      >
                        删除
                      </button>
                    )}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {filtered.length === 0 && <EmptyState text="无用户" />}
      </div>
    </div>
  )
}
