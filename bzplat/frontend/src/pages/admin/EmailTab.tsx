import { useCallback, useEffect, useState } from 'react'
import { apiGet, apiJson, errMsg } from '../../api'
import { EmptyState, Loading, ErrorMsg, RefreshBtn, StatusBadge } from './ui'

interface Template {
  key: string
  subject: string
  body_html: string
  body_text: string
  updated_at: string
}
interface OutboxRow {
  id: number
  to_addr: string
  subject: string
  template_key: string
  status: string
  error: string
  created_at: string
}

export default function EmailTab() {
  const [templates, setTemplates] = useState<Template[]>([])
  const [outbox, setOutbox] = useState<OutboxRow[]>([])
  const [activeKey, setActiveKey] = useState<string | null>(null)
  const [draft, setDraft] = useState<Template | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [view, setView] = useState<'templates' | 'outbox'>('templates')

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const [t, o] = await Promise.all([
        apiGet<{ templates: Template[] }>('/api/admin/email/templates'),
        apiGet<{ outbox: OutboxRow[] }>('/api/admin/email/outbox?limit=50'),
      ])
      setTemplates(t.templates || [])
      setOutbox(o.outbox || [])
    } catch (e) {
      setError(errMsg(e, '加载失败'))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const selectTemplate = async (key: string) => {
    setActiveKey(key)
    setError('')
    try {
      const d = await apiGet<{ template: Template }>(`/api/admin/email/templates/${key}`)
      setDraft({ ...d.template })
    } catch (e) {
      setError(errMsg(e, '加载模板失败'))
    }
  }

  const save = async () => {
    if (!draft) return
    setSaving(true)
    setError('')
    try {
      await apiJson(`/api/admin/email/templates/${draft.key}`, 'PUT', {
        subject: draft.subject,
        body_html: draft.body_html,
        body_text: draft.body_text,
      })
      await load()
      alert('模板已保存')
    } catch (e) {
      setError(errMsg(e, '保存失败'))
    } finally {
      setSaving(false)
    }
  }

  if (loading) return <Loading />
  return (
    <div>
      <div className="mb-3 flex items-center gap-2">
        <div className="flex gap-1">
          <button
            type="button"
            onClick={() => setView('templates')}
            className={`rounded-lg px-3 py-1.5 text-sm ${
              view === 'templates' ? 'bg-brand-600 text-white' : 'bg-white text-slate-600 border border-slate-300'
            }`}
          >
            邮件模板
          </button>
          <button
            type="button"
            onClick={() => setView('outbox')}
            className={`rounded-lg px-3 py-1.5 text-sm ${
              view === 'outbox' ? 'bg-brand-600 text-white' : 'bg-white text-slate-600 border border-slate-300'
            }`}
          >
            发件箱（{outbox.length}）
          </button>
        </div>
        <div className="ml-auto">
          <RefreshBtn onClick={load} />
        </div>
      </div>
      <ErrorMsg msg={error} />

      {view === 'templates' ? (
        <div className="grid gap-4 lg:grid-cols-[200px_1fr]">
          {/* 模板列表 */}
          <div className="flex flex-row flex-wrap gap-1 lg:flex-col">
            {templates.map((t) => (
              <button
                key={t.key}
                type="button"
                onClick={() => void selectTemplate(t.key)}
                className={`w-full rounded-lg px-3 py-2 text-left text-sm transition lg:w-auto ${
                  activeKey === t.key
                    ? 'bg-brand-50 font-medium text-brand-700'
                    : 'text-slate-600 hover:bg-slate-100'
                }`}
              >
                <div className="font-mono text-xs">{t.key}</div>
                <div className="mt-0.5 truncate text-xs text-slate-400">{t.subject}</div>
              </button>
            ))}
          </div>

          {/* 编辑区 */}
          <div className="min-w-0">
            {draft ? (
              <div className="card p-4">
                <div className="mb-3 flex items-center justify-between">
                  <h3 className="font-mono text-sm font-semibold text-slate-700">{draft.key}</h3>
                  <button
                    type="button"
                    disabled={saving}
                    onClick={() => void save()}
                    className="rounded-lg bg-brand-600 px-4 py-1.5 text-xs font-medium text-white hover:bg-brand-500"
                  >
                    {saving ? '保存中…' : '保存'}
                  </button>
                </div>
                <div className="space-y-3">
                  <div>
                    <label className="mb-1 block text-xs text-slate-500">主题</label>
                    <input
                      value={draft.subject}
                      onChange={(e) => setDraft({ ...draft, subject: e.target.value })}
                      className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-700 focus:border-brand-400 focus:outline-none"
                    />
                  </div>
                  <div>
                    <label className="mb-1 block text-xs text-slate-500">纯文本正文</label>
                    <textarea
                      value={draft.body_text}
                      onChange={(e) => setDraft({ ...draft, body_text: e.target.value })}
                      rows={4}
                      className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 font-mono text-xs text-slate-700 focus:border-brand-400 focus:outline-none"
                    />
                  </div>
                  <div>
                    <label className="mb-1 block text-xs text-slate-500">HTML 正文</label>
                    <textarea
                      value={draft.body_html}
                      onChange={(e) => setDraft({ ...draft, body_html: e.target.value })}
                      rows={6}
                      className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 font-mono text-xs text-slate-700 focus:border-brand-400 focus:outline-none"
                    />
                  </div>
                  <p className="text-xs text-slate-400">
                    可用占位符：<code className="rounded bg-slate-100 px-1">{'{{username}}'}</code>{' '}
                    <code className="rounded bg-slate-100 px-1">{'{{code}}'}</code>{' '}
                    <code className="rounded bg-slate-100 px-1">{'{{expires_minutes}}'}</code>
                  </p>
                </div>
              </div>
            ) : (
              <EmptyState text="选择左侧模板进行编辑" />
            )}
          </div>
        </div>
      ) : (
        <div className="overflow-x-auto rounded-xl border border-slate-200 bg-white">
          <table className="w-full min-w-[44rem] text-left text-sm">
            <thead className="border-b border-slate-200 bg-slate-50 text-xs uppercase text-slate-400">
              <tr>
                <th className="px-3 py-2.5">时间</th>
                <th className="px-3 py-2.5">收件人</th>
                <th className="px-3 py-2.5">主题</th>
                <th className="px-3 py-2.5">模板</th>
                <th className="px-3 py-2.5">状态</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {outbox.map((o) => (
                <tr key={o.id} className="hover:bg-slate-50">
                  <td className="px-3 py-2 text-xs text-slate-400">{o.created_at}</td>
                  <td className="px-3 py-2 text-slate-600">{o.to_addr}</td>
                  <td className="px-3 py-2 text-slate-600">
                    {o.subject}
                    {o.error && <div className="text-[10px] text-error-500">{o.error}</div>}
                  </td>
                  <td className="px-3 py-2 font-mono text-xs text-slate-400">{o.template_key || '—'}</td>
                  <td className="px-3 py-2">
                    <StatusBadge status={o.status} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {outbox.length === 0 && <EmptyState text="无发信记录" />}
        </div>
      )}
    </div>
  )
}
