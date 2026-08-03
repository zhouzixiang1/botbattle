import { useCallback, useEffect, useState } from 'react'
import { apiGet, apiJson, errMsg } from '../../api'
import { renderMarkdown } from '../../lib/markdown'
import { ErrorMsg, Loading, RefreshBtn, inp } from './ui'

interface JudgeParam {
  key: string
  label: string
  field: string
  value: number
  min: number
  max: number
}

interface JudgeGame {
  game_id: string
  label: string
  code_path: string
  summary: string
  params: JudgeParam[]
  docstring: string
}

interface JudgesResp {
  games: JudgeGame[]
  markdown: string
}

export default function JudgeTab() {
  const [games, setGames] = useState<JudgeGame[]>([])
  const [markdown, setMarkdown] = useState('')
  const [draft, setDraft] = useState<Record<string, number>>({})
  const [showCode, setShowCode] = useState(false)
  const [error, setError] = useState('')
  const [ok, setOk] = useState('')
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const d = await apiGet<JudgesResp>('/api/admin/judges')
      setGames(d.games || [])
      setMarkdown(d.markdown || '')
      const m: Record<string, number> = {}
      for (const g of d.games || []) {
        for (const p of g.params) m[p.key] = p.value
      }
      setDraft(m)
    } catch (e) {
      setError(errMsg(e, '加载失败'))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const save = async () => {
    setBusy(true)
    setError('')
    setOk('')
    // 仅提交相对当前值有变化的参数
    const params: Record<string, number> = {}
    for (const g of games) {
      for (const p of g.params) {
        if (draft[p.key] !== undefined && draft[p.key] !== p.value) {
          params[p.key] = draft[p.key]
        }
      }
    }
    if (Object.keys(params).length === 0) {
      setError('无变化')
      setBusy(false)
      return
    }
    try {
      await apiJson('/api/admin/judges/params', 'PATCH', { params })
      setOk('已保存并热生效（下一局即用新规则参数）')
      await load()
    } catch (e) {
      setError(errMsg(e, '保存失败'))
    } finally {
      setBusy(false)
    }
  }

  if (loading && games.length === 0) return <Loading />
  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <p className="text-xs text-muted-foreground">
          裁判代码只读展示；规则参数可热调，下一局对局立即生效。代码逻辑改动需走业务代码流程。
        </p>
        <RefreshBtn onClick={load} />
      </div>
      <ErrorMsg msg={error} />
      {ok && <p className="text-sm text-success">{ok}</p>}

      {games.map((g) => (
        <div key={g.game_id} className="rounded-xl border border-border bg-card p-4">
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <h3 className="text-sm font-medium text-foreground">{g.label}</h3>
            <span className="font-mono text-xs text-muted-foreground">{g.code_path}</span>
          </div>
          <p className="mt-1 text-sm text-muted-foreground">{g.summary}</p>

          {g.params.length > 0 ? (
            <div className="mt-3 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
              {g.params.map((p) => (
                <label key={p.key} className="text-sm text-muted-foreground">
                  {p.label}
                  <input
                    type="number"
                    min={p.min}
                    max={p.max}
                    className={inp}
                    value={draft[p.key] ?? p.value}
                    onChange={(e) =>
                      setDraft({ ...draft, [p.key]: Number(e.target.value) })
                    }
                  />
                  <span className="mt-0.5 block text-xs text-muted-foreground">
                    范围 {p.min}–{p.max}
                  </span>
                </label>
              ))}
            </div>
          ) : (
            <p className="mt-3 text-xs text-muted-foreground">
              无全局可调参数（点格棋 N 由各对局 match 配置决定）。
            </p>
          )}

          {g.docstring && (
            <details className="mt-3">
              <summary className="cursor-pointer text-xs text-muted-foreground">
                裁判代码 docstring（只读）
              </summary>
              <pre className="mt-2 whitespace-pre-wrap rounded-lg bg-muted p-3 text-xs leading-relaxed text-muted-foreground">
                {g.docstring}
              </pre>
            </details>
          )}
        </div>
      ))}

      <button
        type="button"
        disabled={busy}
        onClick={() => void save()}
        className="rounded-lg bg-primary px-4 py-2 text-sm text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
      >
        保存参数
      </button>

      {markdown && (
        <div className="rounded-xl border border-border bg-card p-4">
          <button
            type="button"
            onClick={() => setShowCode((v) => !v)}
            className="text-sm font-medium text-primary hover:opacity-80"
          >
            {showCode ? '▾ 收起裁判代码说明' : '▸ 展开裁判代码说明（仅管理员可见）'}
          </button>
          {showCode && (
            <article
              className="mt-3 card p-4 sm:p-6"
              dangerouslySetInnerHTML={{ __html: renderMarkdown(markdown) }}
            />
          )}
        </div>
      )}
    </div>
  )
}
