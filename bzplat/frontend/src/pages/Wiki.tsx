import { useCallback, useEffect, useMemo, useState } from 'react'
import { BookOpen, FileText } from 'lucide-react'

import { apiGet, errMsg } from '@/api'
import { DataRegion, PageFrame, PageHeader, StickyToolbar, SummaryStrip } from '@/components/layout'
import { Button } from '@/components/ui/button'
import { EmptyState, ErrorMsg, Loading } from '@/components/ui/status'
import { renderMarkdown } from '@/lib/markdown'
import { SummaryMetric } from '@/pages/public-page-ui'

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

function markWikiScrollRegions(html: string): string {
  return html
    .replaceAll(
      '<pre class="my-3 overflow-x-auto',
      '<pre data-scroll-region="wiki-code" data-overflow-allowed="x" tabindex="0" class="my-3 overflow-x-auto',
    )
    .replaceAll(
      '<div class="my-3 overflow-x-auto"><table',
      '<div data-scroll-region="wiki-table" data-overflow-allowed="x" tabindex="0" class="my-3 overflow-x-auto"><table',
    )
}

export default function Wiki() {
  const [pages, setPages] = useState<WikiPage[]>([])
  const [slug, setSlug] = useState('')
  const [title, setTitle] = useState('')
  const [markdown, setMarkdown] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)

  const loadPage = useCallback(async (target: string) => {
    if (target === slug) return
    setLoading(true)
    setError('')
    try {
      const data = await apiGet<WikiResp>(`/api/wiki?slug=${encodeURIComponent(target)}`)
      setSlug(data.slug)
      setTitle(data.title)
      setMarkdown(data.markdown || '')
      if (window.location.hash !== `#/wiki?slug=${target}`) {
        window.location.hash = `#/wiki?slug=${target}`
      }
    } catch (cause) {
      setError(errMsg(cause, '加载失败'))
    } finally {
      setLoading(false)
    }
  }, [slug])

  useEffect(() => {
    let cancelled = false
    void (async () => {
      try {
        const data = await apiGet<WikiResp>('/api/wiki')
        if (cancelled) return
        setPages(data.pages || [])
        const hashed = window.location.hash.match(/slug=([\w-]+)/)
        const initial = hashed ? hashed[1] : data.slug
        if (hashed && initial !== data.slug) {
          const page = await apiGet<WikiResp>(`/api/wiki?slug=${encodeURIComponent(initial)}`)
          if (cancelled) return
          setSlug(page.slug)
          setTitle(page.title)
          setMarkdown(page.markdown || '')
        } else {
          setSlug(data.slug)
          setTitle(data.title)
          setMarkdown(data.markdown || '')
        }
      } catch (cause) {
        if (!cancelled) setError(errMsg(cause, '加载失败'))
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    const onHash = () => {
      const match = window.location.hash.match(/slug=([\w-]+)/)
      if (match) void loadPage(match[1])
    }
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [loadPage])

  const rendered = useMemo(() => markWikiScrollRegions(renderMarkdown(markdown)), [markdown])
  const current = pages.find((page) => page.slug === slug)

  return (
    <PageFrame width="default" layout="public-wiki">
      <PageHeader
        eyebrow="开发者文档"
        title="Wiki"
        description="协议规范、Bot 开发指南、游戏规则、样例与安全说明。"
      />

      <SummaryStrip columns={3}>
        <SummaryMetric label="文档页" value={pages.length} detail="当前公开目录" icon={<BookOpen className="size-4" />} />
        <SummaryMetric label="当前页面" value={title || '—'} detail={current?.summary} mono={false} icon={<FileText className="size-4" />} />
        <SummaryMetric label="正文规模" value={markdown.length} detail="字符（含代码）" />
      </SummaryStrip>

      {pages.length > 0 && (
        <StickyToolbar label="Wiki 文档导航" className="items-stretch">
          <nav aria-label="Wiki 目录" className="flex min-w-0 flex-1 flex-wrap items-center gap-1">
            {pages.map((page) => (
              <Button
                key={page.slug}
                type="button"
                size="sm"
                variant={page.slug === slug ? 'default' : 'ghost'}
                onClick={() => void loadPage(page.slug)}
                className="max-w-full"
                aria-current={page.slug === slug ? 'page' : undefined}
              >
                <span className="max-w-48 truncate">{page.title}</span>
              </Button>
            ))}
          </nav>
        </StickyToolbar>
      )}

      <DataRegion
        title={title || '文档正文'}
        description={current?.summary}
        contentClassName="px-4 py-3 sm:px-5 sm:py-4"
      >
        {error ? (
          <ErrorMsg msg={error} className="py-4" />
        ) : loading ? (
          <Loading text="正在加载文档…" className="py-10" />
        ) : !markdown ? (
          <EmptyState text="暂无可显示的文档" icon={<BookOpen className="size-5 opacity-50" />} className="py-8" />
        ) : (
          <article className="min-w-0 break-words" dangerouslySetInnerHTML={{ __html: rendered }} />
        )}
      </DataRegion>
    </PageFrame>
  )
}
