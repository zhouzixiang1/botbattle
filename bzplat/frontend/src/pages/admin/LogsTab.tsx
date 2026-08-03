import { useCallback, useEffect, useState } from 'react'
import { apiGet, errMsg } from '../../api'
import { ErrorMsg, Loading, RefreshBtn, Select, SelectContent, SelectItem, SelectTrigger, SelectValue, inp } from './ui'

interface LogState {
  lines: string[]
  path: string
}

const LEVELS = ['', 'ERROR', 'WARNING', 'INFO'] as const

/** 日志文件：app（业务/系统）/ access（HTTP 访问）/ audit（安全审计）。 */
const FILES = [
  { key: 'app', label: '应用日志' },
  { key: 'access', label: '访问日志' },
  { key: 'audit', label: '审计日志' },
] as const

function levelOf(line: string): string {
  if (line.includes(' ERROR ')) return 'ERROR'
  if (line.includes(' WARNING ')) return 'WARNING'
  if (line.includes(' INFO ')) return 'INFO'
  if (line.includes(' DEBUG ')) return 'DEBUG'
  return ''
}

// 日志面板刻意采用深色控制台样式（bg-slate-900 终端），其内部级别配色
// 针对该深色底校准，两种主题下保持一致，不随主题切换。
function levelColor(lv: string): string {
  if (lv === 'ERROR') return 'text-red-400'
  if (lv === 'WARNING') return 'text-amber-400'
  if (lv === 'INFO') return 'text-slate-300'
  return 'text-slate-500'
}

export default function LogsTab() {
  const [data, setData] = useState<LogState | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [file, setFile] = useState<string>('app')
  const [level, setLevel] = useState<string>('')
  const [q, setQ] = useState('')
  const [debouncedQ, setDebouncedQ] = useState('')
  const [limit, setLimit] = useState(300)

  // q 防抖 250ms（避免每按键触发 /api/admin/logs 请求）
  useEffect(() => {
    const t = setTimeout(() => setDebouncedQ(q), 250)
    return () => clearTimeout(t)
  }, [q])

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const params = new URLSearchParams()
      params.set('file', file)
      if (level) params.set('level', level)
      if (debouncedQ) params.set('q', debouncedQ)
      params.set('limit', String(limit))
      const d = await apiGet<LogState>(`/api/admin/logs?${params.toString()}`)
      setData(d)
    } catch (e) {
      setError(errMsg(e, '加载失败'))
    } finally {
      setLoading(false)
    }
  }, [file, level, debouncedQ, limit])

  useEffect(() => {
    void load()
  }, [load])

  if (loading && !data) return <Loading />
  const lines = data?.lines || []
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        {/* 日志文件切换 */}
        <div className="inline-flex rounded-lg border border-border p-0.5">
          {FILES.map((f) => (
            <button
              key={f.key}
              type="button"
              onClick={() => setFile(f.key)}
              className={`rounded-md px-3 py-1 text-xs font-medium transition-colors ${
                file === f.key
                  ? 'bg-primary text-primary-foreground'
                  : 'text-muted-foreground hover:bg-accent hover:text-accent-foreground'
              }`}
            >
              {f.label}
            </button>
          ))}
        </div>
        <p className="text-xs text-muted-foreground">{data?.path ?? '—'}</p>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          <Select value={level || 'all'} onValueChange={(v) => setLevel(v === 'all' ? '' : v)}>
            <SelectTrigger size="sm" className="h-8 w-[8.5rem] text-xs">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {LEVELS.map((l) => (
                <SelectItem key={l || 'all'} value={l || 'all'}>{l || '全部级别'}</SelectItem>
              ))}
            </SelectContent>
          </Select>
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="关键字 / IP / action"
            className={`${inp} w-44 px-2 py-1 text-xs`}
          />
          <input
            type="number" min={50} max={2000}
            value={limit}
            onChange={(e) => setLimit(Number(e.target.value))}
            className={`${inp} w-20 px-2 py-1 text-xs`}
          />
          <RefreshBtn onClick={load} />
        </div>
      </div>
      <ErrorMsg msg={error} />
      <div className="max-h-[70vh] overflow-auto rounded-xl border border-border bg-slate-900 p-3 font-mono text-xs leading-relaxed">
        {lines.length === 0 ? (
          <div className="py-8 text-center text-slate-500">无匹配日志</div>
        ) : (
          lines.map((ln, i) => (
            <div key={i} className={`whitespace-pre-wrap break-all ${levelColor(levelOf(ln))}`}>
              {ln}
            </div>
          ))
        )}
      </div>
      <p className="text-xs text-muted-foreground">共 {lines.length} 行（末尾 {limit} 条过滤后）</p>
    </div>
  )
}
