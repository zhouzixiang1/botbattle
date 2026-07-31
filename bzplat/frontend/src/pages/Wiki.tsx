import { useEffect, useState } from 'react'
import PageStub from '../components/PageStub'
import { apiGet, errMsg } from '../api'

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

/* ── 极简 markdown 渲染器 ───────────────────────────────────────
 * 支持：# ~ #### 标题、代码块、表格、有序/无序列表、行内 `code`、段落。
 * 仅用于站内 Wiki；无需完整 markdown 引擎。输出已转义，防注入。
 */

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

/** 行内格式：`code`、**bold**、[text](href) */
function inline(s: string): string {
  let out = escapeHtml(s)
  out = out.replace(
    /`([^`]+)`/g,
    '<code class="rounded bg-slate-100 px-1 py-0.5 text-[0.85em] text-brand-700">$1</code>',
  )
  out = out.replace(/\*\*([^*]+)\*\*/g, '<strong class="font-semibold text-slate-900">$1</strong>')
  out = out.replace(
    /\[([^\]]+)\]\(([^)]+)\)/g,
    '<a class="text-brand-600 underline hover:text-brand-700" href="$2">$1</a>',
  )
  return out
}

interface Block {
  tag: string
  html: string
}

function parseBlock(block: string): Block {
  const lines = block.replace(/\r\n/g, '\n').split('\n')
  // 代码块
  if (lines[0].startsWith('```')) {
    const code = lines.slice(1, lines.length - 1).join('\n')
    return {
      tag: 'pre',
      html: `<pre class="my-3 overflow-x-auto rounded-lg bg-slate-900 p-3 text-xs leading-relaxed text-emerald-300">${escapeHtml(
        code,
      )}</pre>`,
    }
  }
  // 表格：第二行是 |---| 分隔
  if (
    lines.length >= 2 &&
    /\|/.test(lines[0]) &&
    /^\s*\|?[\s:|-]+\|?\s*$/.test(lines[1]) &&
    lines[1].includes('-')
  ) {
    const splitRow = (l: string) =>
      l.replace(/^\s*\|/, '').replace(/\|\s*$/, '').split('|').map((c) => c.trim())
    const head = splitRow(lines[0])
    const body = lines.slice(2).filter((l) => l.trim() !== '').map(splitRow)
    let html =
      '<div class="my-3 overflow-x-auto"><table class="w-full border-collapse text-sm">'
    html +=
      '<thead><tr>' +
      head
        .map(
          (h) =>
            `<th class="border border-slate-200 bg-slate-50 px-3 py-2 text-left font-semibold text-slate-700">${inline(
              h,
            )}</th>`,
        )
        .join('') +
      '</tr></thead><tbody>'
    for (const row of body) {
      html +=
        '<tr>' +
        row
          .map(
            (c) =>
              `<td class="border border-slate-200 px-3 py-2 align-top text-slate-600">${inline(
                c,
              )}</td>`,
          )
          .join('') +
        '</tr>'
    }
    html += '</tbody></table></div>'
    return { tag: 'table', html }
  }
  // 列表（合并相邻 -/* 或 数字. 行）
  if (lines.every((l) => /^\s*([-*]|\d+\.)\s+/.test(l) || l.trim() === '')) {
    const items = lines.filter((l) => l.trim() !== '')
    if (items.length && items.every((l) => /^\s*\d+\.\s+/.test(l))) {
      return {
        tag: 'ol',
        html:
          '<ol class="my-2 ml-5 list-decimal space-y-1 text-slate-600">' +
          items.map((l) => `<li>${inline(l.replace(/^\s*\d+\.\s+/, ''))}</li>`).join('') +
          '</ol>',
      }
    }
    return {
      tag: 'ul',
      html:
        '<ul class="my-2 ml-5 list-disc space-y-1 text-slate-600">' +
        items.map((l) => `<li>${inline(l.replace(/^\s*[-*]\s+/, ''))}</li>`).join('') +
        '</ul>',
    }
  }
  // 标题
  const m = /^(#{1,4})\s+(.*)$/.exec(lines[0])
  if (m && lines.length === 1) {
    const level = m[1].length
    const cls = [
      'mt-2 mb-3 text-xl font-bold text-slate-900',
      'mt-5 mb-2 text-lg font-semibold text-slate-900',
      'mt-4 mb-2 text-base font-semibold text-slate-900',
      'mt-3 mb-1.5 text-sm font-semibold text-brand-200',
    ][level - 1]
    return { tag: `h${level}`, html: `<h${level} class="${cls}">${escapeHtml(m[2])}</h${level}>` }
  }
  // 引用块
  if (lines.every((l) => l.trim() === '' || /^\s*>\s?/.test(l))) {
    const text = lines
      .filter((l) => l.trim() !== '')
      .map((l) => l.replace(/^\s*>\s?/, ''))
      .join('<br/>')
    if (text) {
      return {
        tag: 'blockquote',
        html: `<blockquote class="my-3 border-l-2 border-brand-400 bg-brand-50 px-3 py-2 text-sm text-slate-600">${inline(
          text,
        )}</blockquote>`,
      }
    }
  }
  // 段落（多行用 <br/> 连）
  const text = lines.join(' ').trim()
  if (!text) return { tag: 'space', html: '<div class="h-2"></div>' }
  return { tag: 'p', html: `<p class="mb-2 leading-relaxed text-slate-600">${inline(text)}</p>` }
}

function renderMarkdown(md: string): string {
  // 先按空行 + 代码块边界切分成块
  const blocks: string[] = []
  let buf: string[] = []
  let inCode = false
  for (const line of md.split('\n')) {
    if (line.startsWith('```')) {
      if (inCode) {
        buf.push(line)
        blocks.push(buf.join('\n'))
        buf = []
        inCode = false
      } else {
        if (buf.length) {
          blocks.push(buf.join('\n'))
          buf = []
        }
        buf.push(line)
        inCode = true
      }
      continue
    }
    if (inCode) {
      buf.push(line)
      continue
    }
    // 空行：块边界
    if (line.trim() === '') {
      if (buf.length) {
        blocks.push(buf.join('\n'))
        buf = []
      }
      buf = []
    } else {
      buf.push(line)
    }
  }
  if (buf.length) blocks.push(buf.join('\n'))
  return blocks.map(parseBlock).map((b) => b.html).join('\n')
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
