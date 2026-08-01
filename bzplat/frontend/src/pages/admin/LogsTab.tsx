import { useCallback, useEffect, useState } from 'react'
import { apiGet, errMsg } from '../../api'
import { ErrorMsg, Loading, RefreshBtn } from './ui'

interface LogState {
  lines: string[]
  path: string
}

const LEVELS = ['', 'ERROR', 'WARNING', 'INFO'] as const

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
  const [level, setLevel] = useState<string>('')
  const [q, setQ] = useState('')
  const [limit, setLimit] = useState(300)

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const params = new URLSearchParams()
      if (level) params.set('level', level)
      if (q) params.set('q', q)
      params.set('limit', String(limit))
      const d = await apiGet<LogState>(`/api/admin/logs?${params.toString()}`)
      setData(d)
    } catch (e) {
      setError(errMsg(e, '加载失败'))
    } finally {
      setLoading(false)
    }
  }, [level, q, limit])

  useEffect(() => {
    void load()
  }, [load])

  if (loading && !data) return <Loading />
  const lines = data?.lines || []
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <p className="text-xs text-muted-foreground">日志文件：{data?.path ?? '—'}</p>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          <select
            value={level}
            onChange={(e) => setLevel(e.target.value)}
            className="rounded-lg border border-input bg-background px-2 py-1 text-xs text-foreground"
          >
            {LEVELS.map((l) => (
              <option key={l} value={l}>{l || '全部级别'}</option>
            ))}
          </select>
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="关键字/match_id"
            className="w-44 rounded-lg border border-input bg-background px-2 py-1 text-xs text-foreground placeholder:text-muted-foreground"
          />
          <input
            type="number" min={50} max={2000}
            value={limit}
            onChange={(e) => setLimit(Number(e.target.value))}
            className="w-20 rounded-lg border border-input bg-background px-2 py-1 text-xs text-foreground"
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
