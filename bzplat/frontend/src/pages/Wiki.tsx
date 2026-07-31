import { useEffect, useState } from 'react'
import PageStub from '../components/PageStub'
import { apiGet, errMsg } from '../api'
import { renderMarkdown } from '../lib/markdown'

interface WikiPage {
  slug: string
  title: string
  summary: string
}

interface WikiResp {
  slug: string
  title: string
  markdown: string
  pages: WikiPage[]
}

export default function Wiki() {
  const [pages, setPages] = useState<WikiPage[]>([])
  const [slug, setSlug] = useState('')
  const [md, setMd] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    void (async () => {
      try {
        const d = await apiGet<WikiResp>('/api/wiki')
        if (cancelled) return
        setPages(d.pages || [])
        setSlug(d.slug)
        setMd(d.markdown || '')
      } catch (e) {
        if (!cancelled) setError(errMsg(e, '加载失败'))
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  const loadPage = async (target: string) => {
    if (target === slug) return
    setLoading(true)
    setError('')
    try {
      const d = await apiGet<WikiResp>(`/api/wiki?slug=${encodeURIComponent(target)}`)
      setSlug(d.slug)
      setMd(d.markdown || '')
    } catch (e) {
      setError(errMsg(e, '加载失败'))
    } finally {
      setLoading(false)
    }
  }

  return (
    <PageStub title="Wiki">
      <p className="mb-4 text-sm text-slate-400">
        协议规范、Bot 开发指南、样例与安全说明。
      </p>
      {error && <p className="mb-3 text-sm text-error-500">{error}</p>}

      <div className="grid gap-4 lg:grid-cols-[200px_1fr]">
        {/* 侧栏导航 */}
        {pages.length > 1 && (
          <nav className="flex flex-row flex-wrap gap-1 lg:flex-col">
            {pages.map((p) => (
              <button
                key={p.slug}
                type="button"
                onClick={() => void loadPage(p.slug)}
                className={`w-full rounded-lg px-3 py-2 text-left text-sm transition lg:w-auto ${
                  p.slug === slug
                    ? 'bg-brand-600 font-medium text-white'
                    : 'text-slate-600 hover:bg-slate-100 hover:text-slate-900'
                }`}
              >
                <div>{p.title}</div>
                <div className="mt-0.5 hidden text-xs text-slate-400 lg:block">{p.summary}</div>
              </button>
            ))}
          </nav>
        )}

        {/* 正文 */}
        <div className="min-w-0">
          {loading && !md ? (
            <pre className="whitespace-pre-wrap rounded-xl border border-slate-700/80 bg-slate-900/40 p-4 text-sm text-slate-400">
              加载中…
            </pre>
          ) : (
            <article
              className="card p-4 sm:p-6"
              dangerouslySetInnerHTML={{ __html: renderMarkdown(md) }}
            />
          )}
        </div>
      </div>
    </PageStub>
  )
}
