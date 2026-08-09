import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Copy } from 'lucide-react'
import { toast } from 'sonner'

import { apiGet, errMsg } from '../../api'
import {
  Badge,
  Button,
  ErrorMsg,
  Loading,
  RefreshBtn,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  inp,
} from './ui'

interface LogState {
  lines: string[]
  path?: string
}

type LogLevel = '' | 'CRITICAL' | 'ERROR' | 'WARNING' | 'INFO' | 'DEBUG'

interface ParsedLine {
  raw: string
  timestamp?: string
  level?: Exclude<LogLevel, ''>
  module?: string
  message: string
}

const LEVELS: Array<{ value: LogLevel; label: string }> = [
  { value: '', label: '全部级别' },
  { value: 'CRITICAL', label: '严重' },
  { value: 'ERROR', label: '错误' },
  { value: 'WARNING', label: '警告' },
  { value: 'INFO', label: '信息' },
  { value: 'DEBUG', label: '调试' },
]

const FILES = [
  { key: 'app', label: '应用日志', description: '对局、调度器与运行时' },
  { key: 'access', label: '访问日志', description: 'HTTP 请求、状态与来源' },
  { key: 'audit', label: '审计日志', description: '管理员与安全操作' },
] as const

const STRUCTURED_LOG = /^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})\s+(DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+\[([^\]]+)]\s*(.*)$/

function parseLine(raw: string): ParsedLine {
  const match = raw.match(STRUCTURED_LOG)
  if (!match) return { raw, message: raw }
  return {
    raw,
    timestamp: match[1],
    level: match[2] as ParsedLine['level'],
    module: match[3],
    message: match[4],
  }
}

function parseLines(rawLines: string[], file: (typeof FILES)[number]['key']): ParsedLine[] {
  const parsed: ParsedLine[] = []
  for (const raw of rawLines) {
    const line = parseLine(raw)
    const previous = parsed.at(-1)
    // Python traceback/runner stderr lines follow one structured app-log header.
    // Keep that context together instead of rendering every stack line as an
    // unrelated "raw" record. Access/audit formats remain one row per line.
    if (file === 'app' && !line.timestamp && previous?.timestamp) {
      previous.raw += `\n${raw}`
      previous.message += `\n${raw}`
    } else {
      parsed.push(line)
    }
  }
  return parsed
}

function levelVariant(level?: string): 'default' | 'secondary' | 'destructive' | 'outline' {
  if (level === 'CRITICAL' || level === 'ERROR') return 'destructive'
  if (level === 'WARNING') return 'outline'
  if (level === 'INFO') return 'secondary'
  return 'outline'
}

function levelLabel(level?: string): string {
  return LEVELS.find((item) => item.value === level)?.label || level || '原始'
}

export default function LogsTab() {
  const [data, setData] = useState<LogState | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [file, setFile] = useState<(typeof FILES)[number]['key']>('app')
  const [level, setLevel] = useState<LogLevel>('')
  const [q, setQ] = useState('')
  const [debouncedQ, setDebouncedQ] = useState('')
  const [limit, setLimit] = useState(300)
  const requestSeq = useRef(0)

  useEffect(() => {
    const timer = setTimeout(() => setDebouncedQ(q.trim()), 250)
    return () => clearTimeout(timer)
  }, [q])

  const load = useCallback(async () => {
    const seq = ++requestSeq.current
    setLoading(true)
    setError('')
    try {
      const params = new URLSearchParams({ file, limit: String(limit) })
      if (level) params.set('level', level)
      if (debouncedQ) params.set('q', debouncedQ)
      const next = await apiGet<LogState>(`/api/admin/logs?${params.toString()}`)
      if (seq === requestSeq.current) setData(next)
    } catch (cause) {
      if (seq === requestSeq.current) setError(errMsg(cause, '加载失败'))
    } finally {
      if (seq === requestSeq.current) setLoading(false)
    }
  }, [file, level, debouncedQ, limit])

  useEffect(() => {
    void load()
  }, [load])

  const lines = useMemo(() => parseLines(data?.lines || [], file), [data, file])
  const rawLineCount = data?.lines.length || 0
  const levelCounts = useMemo(() => {
    const counts: Record<string, number> = {}
    for (const line of lines) counts[line.level || 'RAW'] = (counts[line.level || 'RAW'] || 0) + 1
    return counts
  }, [lines])
  const selectedFile = FILES.find((item) => item.key === file) || FILES[0]

  const copyVisible = async () => {
    try {
      await navigator.clipboard.writeText(lines.map((line) => line.raw).join('\n'))
      toast.success(`已复制 ${lines.length} 条日志记录`)
    } catch {
      setError('浏览器拒绝访问剪贴板，请手动选择复制')
    }
  }

  if (loading && !data) return <Loading />
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <div className="inline-flex rounded-lg border border-border p-0.5">
          {FILES.map((item) => (
            <button
              key={item.key}
              type="button"
              onClick={() => setFile(item.key)}
              className={`rounded-md px-3 py-1.5 text-xs font-medium transition-colors ${
                file === item.key
                  ? 'bg-primary text-primary-foreground'
                  : 'text-muted-foreground hover:bg-accent hover:text-accent-foreground'
              }`}
            >
              {item.label}
            </button>
          ))}
        </div>
        <span className="text-xs text-muted-foreground">{selectedFile.description}</span>
        <div className="ml-auto flex gap-2">
          <Button type="button" variant="outline" size="sm" onClick={() => void copyVisible()} disabled={lines.length === 0}>
            <Copy className="size-3.5" />复制当前结果
          </Button>
          <RefreshBtn onClick={load} />
        </div>
      </div>

      <div className="grid gap-2 rounded-xl border border-border bg-card p-3 sm:grid-cols-[9rem_minmax(14rem,1fr)_8rem]">
        <Select value={level || 'all'} onValueChange={(value) => setLevel(value === 'all' ? '' : value as LogLevel)}>
          <SelectTrigger size="sm" className="h-9 w-full text-xs">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {LEVELS.map((item) => (
              <SelectItem key={item.value || 'all'} value={item.value || 'all'}>{item.label}</SelectItem>
            ))}
          </SelectContent>
        </Select>
        <input
          value={q}
          onChange={(event) => setQ(event.target.value)}
          placeholder="对局 ID / Bot ID / 模块 / IP / 操作"
          aria-label="日志关键字"
          className={`${inp} mt-0 h-9 px-3 py-1 text-xs`}
        />
        <input
          type="number"
          min={50}
          max={2000}
          value={limit}
          aria-label="日志行数"
          onChange={(event) => setLimit(Math.min(2000, Math.max(50, Number(event.target.value) || 300)))}
          className={`${inp} mt-0 h-9 px-3 py-1 text-xs`}
        />
      </div>

      <ErrorMsg msg={error} />

      <div className="flex flex-wrap gap-2 text-xs text-muted-foreground">
        <span>匹配 {lines.length} 条记录 / {rawLineCount} 行</span>
        {Object.entries(levelCounts).map(([name, count]) => (
          <span key={name}>{levelLabel(name)} {count}</span>
        ))}
        {loading && <span>刷新中…</span>}
      </div>

      <div role="log" aria-live="polite" className="max-h-[70vh] overflow-auto rounded-xl border border-border bg-card">
        {lines.length === 0 ? (
          <div className="py-10 text-center text-sm text-muted-foreground">没有符合条件的日志</div>
        ) : (
          <div className="divide-y divide-border">
            {lines.map((line, index) => (
              <div key={`${index}-${line.raw.slice(0, 24)}`} className="grid gap-1 px-3 py-2 font-mono text-xs hover:bg-accent/50 lg:grid-cols-[9.5rem_5rem_16rem_minmax(0,1fr)] lg:items-start">
                <span className="text-muted-foreground">{line.timestamp || '—'}</span>
                <span><Badge variant={levelVariant(line.level)} className="text-[10px]">{levelLabel(line.level)}</Badge></span>
                <span className="break-all text-muted-foreground">{line.module || '未结构化'}</span>
                <span className="whitespace-pre-wrap break-words text-foreground">{line.message}</span>
              </div>
            ))}
          </div>
        )}
      </div>
      <p className="text-xs text-muted-foreground">
        当前只展示所选日志文件的末尾结果；对局异常请优先使用完整对局 ID 搜索，并在“对局记录”查看持久化错误样本。
      </p>
    </div>
  )
}
