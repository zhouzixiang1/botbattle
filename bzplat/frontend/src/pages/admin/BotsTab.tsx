import { Fragment, useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { apiGet, apiJson, errMsg } from '../../api'
import { Badge, Button, Table, TableHeader, TableBody, TableHead, TableRow, TableCell,  EmptyState, Loading, ErrorMsg, RefreshBtn, Tooltip, TooltipContent, TooltipTrigger } from './ui'
import { useConfirm } from '@/hooks/use-confirm'
import Pagination from '@/components/Pagination'
import { OverflowText } from '@/components/ui/overflow-text'
import { Input } from '@/components/ui/input'
import { fmtTime } from '@/lib/format'

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
  is_deleted?: boolean
  is_builtin: boolean
  created_at: string
  runnable?: boolean
  unsupported_reason?: string | null
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
  runnable?: boolean
  unsupported_reason?: string | null
}

/** 不可运行诊断：中文说明优先；缺失时回退「格式待检」，原始格式信息收进 Tooltip。 */
function RunnableWarning({ format, os, arch, reason }: { format: string; os: string; arch: string; reason?: string | null }) {
  const content = (
    <span tabIndex={reason ? undefined : 0} className={reason ? 'mt-0.5 block text-xs font-normal text-destructive' : 'mt-0.5 block cursor-help text-xs font-normal text-destructive underline decoration-dotted underline-offset-2'}>
      {reason || '格式待检'}
    </span>
  )
  if (reason) return content
  return (
    <Tooltip>
      <TooltipTrigger asChild>{content}</TooltipTrigger>
      <TooltipContent className="font-mono text-xs">{`${format}/${os}-${arch}`}</TooltipContent>
    </Tooltip>
  )
}

export default function BotsTab() {
  const [confirm, confirmDialog] = useConfirm()
  const [bots, setBots] = useState<Bot[]>([])
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [q, setQ] = useState('')
  const [expand, setExpand] = useState<number | null>(null)
  const [versions, setVersions] = useState<Version[]>([])
  const [versionsLoading, setVersionsLoading] = useState<number | null>(null)
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

  const del = async (bot: Bot) => {
    if (!await confirm({
      title: '删除 Bot',
      desc: `确认删除「${bot.display_name || bot.name}」？将同时删除其全部版本与文件。`,
      confirmText: '删除',
      danger: true,
    })) return
    setBusyId(bot.id)
    try {
      await apiJson(`/api/admin/bots/${bot.id}`, 'DELETE')
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
    setVersionsLoading(b.id)
    try {
      const d = await apiGet<{ versions: Version[] }>(`/api/admin/bots/${b.id}/versions`)
      setVersions(d.versions || [])
    } catch (e) {
      setError(errMsg(e, '加载版本失败'))
    } finally {
      setVersionsLoading(null)
    }
  }

  const filtered = bots.filter(
    (b) => !q || b.name.toLowerCase().includes(q.toLowerCase()) || b.display_name.toLowerCase().includes(q.toLowerCase()),
  )

  if (loading && !bots.length) return <Loading />
  return (
    <div>
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <Input
          value={q}
          onChange={(e) => {
            setQ(e.target.value)
            setPage(1)
          }}
          placeholder="搜索 Bot 名称"
          className="h-9 w-auto min-w-48"
        />
        <span className="text-xs text-muted-foreground">共 {total || filtered.length} 个</span>
        <div className="ml-auto">
          <RefreshBtn onClick={load} />
        </div>
      </div>
      <ErrorMsg msg={error} />

      <div className="overflow-x-auto rounded-xl border border-border bg-card">
        <Table className="min-w-[42rem]">
          <TableHeader>
            <TableRow>
              <TableHead className="px-2.5 py-2">序号</TableHead>
              <TableHead className="px-2.5 py-2">名称</TableHead>
              <TableHead className="px-2.5 py-2">所有者</TableHead>
              <TableHead className="px-2.5 py-2">版本</TableHead>
              <TableHead className="px-2.5 py-2">状态</TableHead>
              <TableHead className="px-2.5 py-2">操作</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {filtered.map((b, index) => (
              <Fragment key={b.id}>
                <TableRow className="hover:bg-accent">
                  <TableCell className="px-2.5 py-1.5 font-mono tabular-nums text-muted-foreground">{(page - 1) * perPage + index + 1}</TableCell>
                  <TableCell className="max-w-[13rem] px-2.5 py-1.5 font-medium text-foreground">
                    <OverflowText tooltip={b.display_name || b.name}>
                      {b.display_name || b.name}
                      {b.is_builtin ? <span className="ml-1 text-xs font-normal text-primary">内置</span> : null}
                    </OverflowText>
                    {b.runnable === false && (
                      <RunnableWarning format={b.format} os={b.os} arch={b.arch} reason={b.unsupported_reason} />
                    )}
                  </TableCell>
                  <TableCell className="px-2.5 py-1.5">
                    {b.owner_name ? (
                      <Link to={`/user/${encodeURIComponent(b.owner_name)}`} className="text-primary hover:underline">
                        {b.owner_display || b.owner_name}
                      </Link>
                    ) : <span className="text-muted-foreground">所属用户已注销</span>}
                  </TableCell>
                  <TableCell className="px-2.5 py-1.5 font-mono text-xs text-muted-foreground">v{b.current_version}</TableCell>
                  <TableCell className="px-2.5 py-1.5">
                    <div className="flex gap-1">
                      {b.is_deleted
                        ? <Badge variant="destructive" className="text-xs">所有者已删除</Badge>
                        : b.is_active
                          ? <Badge variant="secondary" className="text-xs">启用</Badge>
                          : <Badge variant="outline" className="text-xs text-muted-foreground">停用</Badge>}
                      {b.runnable === false && <Badge variant="destructive" className="text-xs">不可运行</Badge>}
                    </div>
                  </TableCell>
                  <TableCell className="px-2.5 py-1.5">
                    <div className="flex flex-wrap gap-1">
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        className="max-lg:min-h-11 px-2"
                        disabled={busyId === b.id || b.is_deleted || (!b.is_active && b.runnable === false)}
                        onClick={() => void patch(b.id, { is_active: !b.is_active })}
                      >
                        {b.is_deleted ? '不可上架' : b.is_active ? '下架' : b.runnable === false ? '不可上架' : '上架'}
                      </Button>
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        className="max-lg:min-h-11 px-2"
                        onClick={() => void showVersions(b)}
                      >
                        版本
                      </Button>
                      <Button
                        type="button"
                        variant="destructive"
                        size="sm"
                        className="max-lg:min-h-11 px-2"
                        disabled={busyId === b.id}
                        onClick={() => void del(b)}
                      >
                        删除
                      </Button>
                    </div>
                  </TableCell>
                </TableRow>
                {expand === b.id && (
                  <TableRow key={`${b.id}-v`} className="bg-muted/60">
                    <TableCell colSpan={6} className="px-3 py-2">
                      {versionsLoading === b.id ? (
                        <Loading text="加载版本…" />
                      ) : versions.length === 0 ? (
                        <EmptyState text="无版本" />
                      ) : (
                        <Table className="min-w-[36rem]">
                          <TableHeader>
                            <TableRow>
                              <TableHead className="px-2 py-1 text-left">版本</TableHead>
                              <TableHead className="px-2 py-1 text-left">大小</TableHead>
                              <TableHead className="px-2 py-1 text-left">校验和</TableHead>
                              <TableHead className="px-2 py-1 text-left">上传时间</TableHead>
                            </TableRow>
                          </TableHeader>
                          <TableBody>
                            {versions.map((v) => (
                              <TableRow key={v.id} className="font-mono text-muted-foreground">
                                <TableCell className="px-2 py-1">
                                  v{v.version}
                                  {v.runnable === false && (
                                    <RunnableWarning format={v.format} os={v.os} arch={v.arch} reason={v.unsupported_reason} />
                                  )}
                                </TableCell>
                                <TableCell className="px-2 py-1">{(v.size_bytes / 1024).toFixed(1)} KB</TableCell>
                                <TableCell className="px-2 py-1">
                                  <Tooltip>
                                    <TooltipTrigger asChild>
                                      <span tabIndex={0} className="cursor-help">{v.checksum.slice(0, 12)}…</span>
                                    </TooltipTrigger>
                                    <TooltipContent className="font-mono">{v.checksum}</TooltipContent>
                                  </Tooltip>
                                </TableCell>
                                <TableCell className="px-2 py-1 whitespace-nowrap">{fmtTime(v.uploaded_at)}</TableCell>
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
