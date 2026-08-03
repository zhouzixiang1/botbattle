import { Fragment, useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { apiGet, apiJson, errMsg } from '../../api'
import { EmptyState, Loading, ErrorMsg, RefreshBtn, StatusBadge, Tooltip, TooltipContent, TooltipTrigger } from './ui'
import { useConfirm } from '@/hooks/use-confirm'

interface Bot {
  id: number
  owner_id: number
  name: string
  display_name: string
  os: string
  arch: string
  format: string
  current_version: number
  is_active: boolean
  is_builtin: boolean
  created_at: string
}
interface Version {
  id: number
  version: number
  size_bytes: number
  checksum: string
  os: string
  arch: string
  format: string
  uploaded_at: string
}

export default function BotsTab() {
  const [confirm, confirmDialog] = useConfirm()
  const [bots, setBots] = useState<Bot[]>([])
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [q, setQ] = useState('')
  const [expand, setExpand] = useState<number | null>(null)
  const [versions, setVersions] = useState<Version[]>([])
  const [busyId, setBusyId] = useState<number | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const d = await apiGet<{ bots: Bot[] }>('/api/admin/bots')
      setBots(d.bots || [])
    } catch (e) {
      setError(errMsg(e, '加载失败'))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const patch = async (id: number, fields: Record<string, unknown>) => {
    setBusyId(id)
    setError('')
    try {
      await apiJson(`/api/admin/bots/${id}`, 'PATCH', fields)
      await load()
    } catch (e) {
      setError(errMsg(e, '操作失败'))
    } finally {
      setBusyId(null)
    }
  }

  const del = async (id: number) => {
    if (!await confirm({
      title: '删除 Bot',
      desc: `确认删除 Bot #${id}？同时删除其版本与文件。`,
      confirmText: '删除',
      danger: true,
    })) return
    setBusyId(id)
    try {
      await apiJson(`/api/admin/bots/${id}`, 'DELETE')
      await load()
    } catch (e) {
      setError(errMsg(e, '删除失败'))
    } finally {
      setBusyId(null)
    }
  }

  const showVersions = async (b: Bot) => {
    if (expand === b.id) {
      setExpand(null)
      return
    }
    setExpand(b.id)
    setVersions([])
    try {
      const d = await apiGet<{ versions: Version[] }>(`/api/admin/bots/${b.id}/versions`)
      setVersions(d.versions || [])
    } catch (e) {
      setError(errMsg(e, '加载版本失败'))
    }
  }

  const filtered = bots.filter(
    (b) => !q || b.name.toLowerCase().includes(q.toLowerCase()) || b.display_name.toLowerCase().includes(q.toLowerCase()),
  )

  if (loading && !bots.length) return <Loading />
  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="搜索 Bot 名称"
          className="rounded-lg border border-input bg-background px-3 py-1.5 text-sm text-foreground focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50 focus:outline-none"
        />
        <span className="text-xs text-muted-foreground">共 {filtered.length} 个</span>
        <div className="ml-auto">
          <RefreshBtn onClick={load} />
        </div>
      </div>
      <ErrorMsg msg={error} />

      <div className="overflow-x-auto rounded-xl border border-border bg-card">
        <table className="w-full min-w-[50rem] text-left text-sm">
          <thead className="border-b border-border bg-muted text-xs uppercase text-muted-foreground">
            <tr>
              <th className="px-3 py-2.5">ID</th>
              <th className="px-3 py-2.5">名称</th>
              <th className="px-3 py-2.5">所有者</th>
              <th className="px-3 py-2.5">格式/架构</th>
              <th className="px-3 py-2.5">版本</th>
              <th className="px-3 py-2.5">状态</th>
              <th className="px-3 py-2.5">操作</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {filtered.map((b) => (
              <Fragment key={b.id}>
                <tr className="hover:bg-accent">
                  <td className="px-3 py-2 font-mono text-muted-foreground">{b.id}</td>
                  <td className="px-3 py-2 font-medium text-foreground">
                    {b.display_name || b.name}
                    {b.is_builtin && <span className="ml-1 text-[10px] text-primary">内置</span>}
                  </td>
                  <td className="px-3 py-2">
                    <Link to={`/user/${b.owner_id}`} className="text-primary hover:underline">
                      #{b.owner_id}
                    </Link>
                  </td>
                  <td className="px-3 py-2 font-mono text-xs text-muted-foreground">
                    {b.format}/{b.arch}
                  </td>
                  <td className="px-3 py-2 font-mono text-xs text-muted-foreground">v{b.current_version}</td>
                  <td className="px-3 py-2">
                    <div className="flex gap-1">
                      {b.is_active ? <StatusBadge status="running" /> : <StatusBadge status="aborted" />}
                    </div>
                  </td>
                  <td className="px-3 py-2">
                    <div className="flex flex-wrap gap-1">
                      <button
                        type="button"
                        disabled={busyId === b.id}
                        onClick={() => void patch(b.id, { is_active: !b.is_active })}
                        className="rounded border border-input bg-card px-2 py-0.5 text-xs text-muted-foreground hover:bg-accent"
                      >
                        {b.is_active ? '下架' : '上架'}
                      </button>
                      <button
                        type="button"
                        onClick={() => void showVersions(b)}
                        className="rounded border border-input bg-card px-2 py-0.5 text-xs text-muted-foreground hover:bg-accent"
                      >
                        版本
                      </button>
                      <button
                        type="button"
                        disabled={busyId === b.id}
                        onClick={() => void del(b.id)}
                        className="rounded border border-input bg-card px-2 py-0.5 text-xs text-destructive hover:bg-destructive/10"
                      >
                        删除
                      </button>
                    </div>
                  </td>
                </tr>
                {expand === b.id && (
                  <tr key={`${b.id}-v`} className="bg-muted/60">
                    <td colSpan={7} className="px-6 py-3">
                      {versions.length === 0 ? (
                        <EmptyState text="无版本" />
                      ) : (
                        <table className="w-full text-xs">
                          <thead className="text-muted-foreground">
                            <tr>
                              <th className="px-2 py-1 text-left">版本</th>
                              <th className="px-2 py-1 text-left">大小</th>
                              <th className="px-2 py-1 text-left">格式/架构</th>
                              <th className="px-2 py-1 text-left">校验和</th>
                              <th className="px-2 py-1 text-left">上传时间</th>
                            </tr>
                          </thead>
                          <tbody className="divide-y divide-border">
                            {versions.map((v) => (
                              <tr key={v.id} className="font-mono text-muted-foreground">
                                <td className="px-2 py-1">v{v.version}</td>
                                <td className="px-2 py-1">{(v.size_bytes / 1024).toFixed(1)} KB</td>
                                <td className="px-2 py-1">{v.format}/{v.arch}</td>
                                <td className="px-2 py-1">
                                  <Tooltip>
                                    <TooltipTrigger asChild>
                                      <span className="cursor-help">{v.checksum.slice(0, 12)}…</span>
                                    </TooltipTrigger>
                                    <TooltipContent className="font-mono">{v.checksum}</TooltipContent>
                                  </Tooltip>
                                </td>
                                <td className="px-2 py-1">{v.uploaded_at}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      )}
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
        {filtered.length === 0 && <EmptyState text="无 Bot" />}
      </div>
      {confirmDialog}
    </div>
  )
}
