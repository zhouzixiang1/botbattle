import { useCallback, useEffect, useState } from 'react'
import { apiGet, apiJson, errMsg } from '../../api'
import { Table, TableHeader, TableBody, TableHead, TableRow, TableCell,  EmptyState, Loading, ErrorMsg, RefreshBtn, StatusBadge } from './ui'
import { toast } from 'sonner'

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
      toast.success('模板已保存')
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
              view === 'templates' ? 'bg-primary text-primary-foreground' : 'bg-card text-muted-foreground border border-input'
            }`}
          >
            邮件模板
          </button>
          <button
            type="button"
            onClick={() => setView('outbox')}
            className={`rounded-lg px-3 py-1.5 text-sm ${
              view === 'outbox' ? 'bg-primary text-primary-foreground' : 'bg-card text-muted-foreground border border-input'
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
                    ? 'bg-primary/10 font-medium text-primary'
                    : 'text-muted-foreground hover:bg-accent'
                }`}
              >
                <div className="font-mono text-xs">{t.key}</div>
                <div className="mt-0.5 truncate text-xs text-muted-foreground">{t.subject}</div>
              </button>
            ))}
          </div>

          {/* 编辑区 */}
          <div className="min-w-0">
            {draft ? (
              <div className="rounded-xl border border-border bg-card p-4">
                <div className="mb-3 flex items-center justify-between">
                  <h3 className="font-mono text-sm font-semibold text-foreground">{draft.key}</h3>
                  <button
                    type="button"
                    disabled={saving}
                    onClick={() => void save()}
                    className="rounded-lg bg-primary px-4 py-1.5 text-xs font-medium text-primary-foreground hover:bg-primary/90"
                  >
                    {saving ? '保存中…' : '保存'}
                  </button>
                </div>
                <div className="space-y-3">
                  <div>
                    <label className="mb-1 block text-xs text-muted-foreground">主题</label>
                    <input
                      value={draft.subject}
                      onChange={(e) => setDraft({ ...draft, subject: e.target.value })}
                      className="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm text-foreground focus-visible:ring-2 focus-visible:ring-ring outline-none"
                    />
                  </div>
                  <div>
                    <label className="mb-1 block text-xs text-muted-foreground">纯文本正文</label>
                    <textarea
                      value={draft.body_text}
                      onChange={(e) => setDraft({ ...draft, body_text: e.target.value })}
                      rows={4}
                      className="w-full rounded-lg border border-input bg-background px-3 py-2 font-mono text-xs text-foreground focus-visible:ring-2 focus-visible:ring-ring outline-none"
                    />
                  </div>
                  <div>
                    <label className="mb-1 block text-xs text-muted-foreground">HTML 正文</label>
                    <textarea
                      value={draft.body_html}
                      onChange={(e) => setDraft({ ...draft, body_html: e.target.value })}
                      rows={6}
                      className="w-full rounded-lg border border-input bg-background px-3 py-2 font-mono text-xs text-foreground focus-visible:ring-2 focus-visible:ring-ring outline-none"
                    />
                  </div>
                  <p className="text-xs text-muted-foreground">
                    可用占位符：<code className="rounded bg-muted px-1">{'{{username}}'}</code>{' '}
                    <code className="rounded bg-muted px-1">{'{{code}}'}</code>{' '}
                    <code className="rounded bg-muted px-1">{'{{expires_minutes}}'}</code>
                  </p>
                </div>
              </div>
            ) : (
              <EmptyState text="选择左侧模板进行编辑" />
            )}
          </div>
        </div>
      ) : (
        <div className="overflow-x-auto rounded-xl border border-border bg-card">
          <Table className="min-w-[48rem]">
            <TableHeader>
              <TableRow>
                <TableHead className="px-3 py-2.5">时间</TableHead>
                <TableHead className="px-3 py-2.5">收件人</TableHead>
                <TableHead className="px-3 py-2.5">主题</TableHead>
                <TableHead className="px-3 py-2.5">模板</TableHead>
                <TableHead className="px-3 py-2.5">状态</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {outbox.map((o) => (
                <TableRow key={o.id} className="hover:bg-accent">
                  <TableCell className="px-3 py-2 text-xs text-muted-foreground">{o.created_at}</TableCell>
                  <TableCell className="px-3 py-2 text-muted-foreground">{o.to_addr}</TableCell>
                  <TableCell className="px-3 py-2 text-muted-foreground">
                    {o.subject}
                    {o.error && <div className="text-[10px] text-destructive">{o.error}</div>}
                  </TableCell>
                  <TableCell className="px-3 py-2 font-mono text-xs text-muted-foreground">{o.template_key || '—'}</TableCell>
                  <TableCell className="px-3 py-2">
                    <StatusBadge status={o.status} />
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          {outbox.length === 0 && <EmptyState text="无发信记录" />}
        </div>
      )}
    </div>
  )
}
