import { Fragment, useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { apiGet, apiJson, errMsg } from '../../api'
import { Badge,  Table, TableHeader, TableBody, TableHead, TableRow, TableCell,  EmptyState, Loading, ErrorMsg, RefreshBtn, Tooltip, TooltipContent, TooltipTrigger } from './ui'
import { useConfirm } from '@/hooks/use-confirm'
import Pagination from '@/components/Pagination'

interface Bot {
  id: number
  owner_id: number
  owner_name?: string
  owner_display?: string
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
  // 分页
  const [page, setPage] = useState(1)
  const [total, setTotal] = useState(0)
  const perPage = 20

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const d = await apiGet<{ bots: Bot[]; total?: number }>(
        `/api/admin/bots?page=${page}&per_page=${perPage}${q ? `&q=${encodeURIComponent(q)}` : ''}`,
      )
      setBots(d.bots || [])
      if (d.total !== undefined) setTotal(d.total)
    } catch (e) {
      setError(errMsg(e, '加载失败'))
    } finally {
      setLoading(false)
    }
  }, [page, q])

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
          onChange={(e) => {
            setQ(e.target.value)
            setPage(1)
          }}
          placeholder="搜索 Bot 名称"
          className="h-9 rounded-lg border border-input bg-background px-3 py-2 text-sm text-foreground focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring outline-none"
        />
        <span className="text-xs text-muted-foreground">共 {total || filtered.length} 个</span>
        <div className="ml-auto">
          <RefreshBtn onClick={load} />
        </div>
      </div>
      <ErrorMsg msg={error} />

      <div className="overflow-x-auto rounded-xl border border-border bg-card">
        <Table className="min-w-[48rem]">
          <TableHeader>
            <TableRow>
              <TableHead className="px-3 py-2.5">ID</TableHead>
              <TableHead className="px-3 py-2.5">名称</TableHead>
              <TableHead className="px-3 py-2.5">所有者</TableHead>
              <TableHead className="px-3 py-2.5">格式/架构</TableHead>
              <TableHead className="px-3 py-2.5">版本</TableHead>
              <TableHead className="px-3 py-2.5">状态</TableHead>
              <TableHead className="px-3 py-2.5">操作</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {filtered.map((b) => (
              <Fragment key={b.id}>
                <TableRow className="hover:bg-accent">
                  <TableCell className="px-3 py-2 font-mono text-muted-foreground">{b.id}</TableCell>
                  <TableCell className="max-w-[16rem] px-3 py-2 font-medium text-foreground">
                    <span className="block truncate" title={b.display_name || b.name}>
                      {b.display_name || b.name}
                    </span>
                    {b.is_builtin && <span className="ml-1 text-[10px] text-primary">内置</span>}
                  </TableCell>
                  <TableCell className="px-3 py-2">
                    {b.owner_name ? (
                      <Link to={`/user/${encodeURIComponent(b.owner_name)}`} className="text-primary hover:underline">
                        {b.owner_display || b.owner_name}
                      </Link>
                    ) : <span className="text-muted-foreground">#{b.owner_id}</span>}
                  </TableCell>
                  <TableCell className="px-3 py-2 font-mono text-xs text-muted-foreground">
                    {b.format}/{b.arch}
                  </TableCell>
                  <TableCell className="px-3 py-2 font-mono text-xs text-muted-foreground">v{b.current_version}</TableCell>
                  <TableCell className="px-3 py-2">
                    <div className="flex gap-1">
                      {b.is_active ? <Badge variant="secondary" className="text-[10px]">启用</Badge> : <Badge variant="outline" className="text-[10px] text-muted-foreground">停用</Badge>}
                    </div>
                  </TableCell>
                  <TableCell className="px-3 py-2">
                    <div className="flex flex-wrap gap-1">
                      <button
                        type="button"
                        disabled={busyId === b.id}
                        onClick={() => void patch(b.id, { is_active: !b.is_active })}
                        className="inline-flex h-8 items-center rounded-md border border-input bg-background px-3 text-xs text-foreground hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring"
                      >
                        {b.is_active ? '下架' : '上架'}
                      </button>
                      <button
                        type="button"
                        onClick={() => void showVersions(b)}
                        className="inline-flex h-8 items-center rounded-md border border-input bg-background px-3 text-xs text-foreground hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring"
                      >
                        版本
                      </button>
                      <button
                        type="button"
                        disabled={busyId === b.id}
                        onClick={() => void del(b.id)}
                        className="inline-flex h-8 items-center rounded-md border border-destructive/30 bg-destructive/10 px-3 text-xs text-destructive hover:bg-destructive/20 focus-visible:ring-2 focus-visible:ring-ring"
                      >
                        删除
                      </button>
                    </div>
                  </TableCell>
                </TableRow>
                {expand === b.id && (
                  <TableRow key={`${b.id}-v`} className="bg-muted/60">
                    <TableCell colSpan={7} className="px-6 py-3">
                      {versions.length === 0 ? (
                        <EmptyState text="无版本" />
                      ) : (
                        <Table className="min-w-[48rem]">
                          <TableHeader>
                            <TableRow>
                              <TableHead className="px-2 py-1 text-left">版本</TableHead>
                              <TableHead className="px-2 py-1 text-left">大小</TableHead>
                              <TableHead className="px-2 py-1 text-left">格式/架构</TableHead>
                              <TableHead className="px-2 py-1 text-left">校验和</TableHead>
                              <TableHead className="px-2 py-1 text-left">上传时间</TableHead>
                            </TableRow>
                          </TableHeader>
                          <TableBody>
                            {versions.map((v) => (
                              <TableRow key={v.id} className="font-mono text-muted-foreground">
                                <TableCell className="px-2 py-1">v{v.version}</TableCell>
                                <TableCell className="px-2 py-1">{(v.size_bytes / 1024).toFixed(1)} KB</TableCell>
                                <TableCell className="px-2 py-1">{v.format}/{v.arch}</TableCell>
                                <TableCell className="px-2 py-1">
                                  <Tooltip>
                                    <TooltipTrigger asChild>
                                      <span className="cursor-help">{v.checksum.slice(0, 12)}…</span>
                                    </TooltipTrigger>
                                    <TooltipContent className="font-mono">{v.checksum}</TooltipContent>
                                  </Tooltip>
                                </TableCell>
                                <TableCell className="px-2 py-1">{v.uploaded_at}</TableCell>
                              </TableRow>
                            ))}
                          </TableBody>
                        </Table>
                      )}
                    </TableCell>
                  </TableRow>
                )}
              </Fragment>
            ))}
          </TableBody>
        </Table>
        {filtered.length === 0 && <EmptyState text="无 Bot" />}
      </div>
      <Pagination page={page} perPage={perPage} total={total} onPageChange={setPage} />
      {confirmDialog}
    </div>
  )
}
