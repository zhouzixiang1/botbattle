import { useEffect, useState } from 'react'
import PageStub from '@/components/PageStub'
import { Card } from '@/components/ui/card'
import { ErrorMsg, Loading } from '@/components/ui/status'
import { cn } from '@/lib/utils'
import { apiGet, errMsg } from '@/api'
import { renderMarkdown } from '@/lib/markdown'

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
        // 初始化：若 URL hash 指向某 slug（#/wiki?slug=xxx），优先加载它，否则用首页。
        const hashed = window.location.hash.match(/slug=([\w-]+)/)
        const initial = hashed ? hashed[1] : d.slug
        if (hashed && initial !== d.slug) {
          const p = await apiGet<WikiResp>(`/api/wiki?slug=${encodeURIComponent(initial)}`)
          if (cancelled) return
          setSlug(p.slug)
          setMd(p.markdown || '')
        } else {
          setSlug(d.slug)
          setMd(d.markdown || '')
        }
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

  // 监听 hash 变化：跨页链接（#/wiki?slug=xxx）点击时跳转到对应 wiki 页。
  useEffect(() => {
    const onHash = () => {
      const m = window.location.hash.match(/slug=([\w-]+)/)
      if (m && m[1] !== slug) void loadPage(m[1])
    }
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [slug])

  const loadPage = async (target: string) => {
    if (target === slug) return
    setLoading(true)
    setError('')
    try {
      const d = await apiGet<WikiResp>(`/api/wiki?slug=${encodeURIComponent(target)}`)
      setSlug(d.slug)
      setMd(d.markdown || '')
      // 同步 URL hash（可分享 / 刷新保持当前页）
      if (window.location.hash !== `#/wiki?slug=${target}`) {
        window.location.hash = `#/wiki?slug=${target}`
      }
    } catch (e) {
      setError(errMsg(e, '加载失败'))
    } finally {
      setLoading(false)
    }
  }

  return (
    <PageStub title="Wiki" subtitle="协议规范、Bot 开发指南、样例与安全说明">
      {error && <ErrorMsg msg={error} className="mb-3" />}

      <div className="grid gap-4 lg:grid-cols-[200px_1fr]">
        {/* 侧栏导航 */}
        {pages.length > 1 && (
          <nav className="flex flex-row flex-wrap gap-1 lg:flex-col">
            {pages.map((p) => (
              <button
                key={p.slug}
                type="button"
                onClick={() => void loadPage(p.slug)}
                className={cn(
                  'w-full rounded-lg px-3 py-2 text-left text-sm transition lg:w-auto',
                  p.slug === slug
                    ? 'bg-primary font-medium text-primary-foreground'
                    : 'text-muted-foreground hover:bg-accent hover:text-foreground',
                )}
              >
                <div>{p.title}</div>
                <div className="mt-0.5 hidden text-xs opacity-80 lg:block">{p.summary}</div>
              </button>
            ))}
          </nav>
        )}

        {/* 正文 */}
        <div className="min-w-0">
          {loading && !md ? (
            <Loading text="加载中…" />
          ) : (
            <Card className="p-4 sm:p-6">
              <article dangerouslySetInnerHTML={{ __html: renderMarkdown(md) }} />
            </Card>
          )}
        </div>
      </div>
    </PageStub>
  )
}
