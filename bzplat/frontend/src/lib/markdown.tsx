/* 极简 markdown 渲染器（站内 Wiki / 管理端说明共用）。
 * 支持：# ~ #### 标题、代码块、表格、有序/无同时列表、行内 `code`、**bold**、图片、链接、引用块。
 * 仅用于受信任内容（本仓库 wiki/*.md）；输出已转义，防注入。
 */
function escapeHtml(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

/** 标题 slugify：用于生成锚点 id（支持中文标题，空格→-，转小写）。
 * 与 wiki md 内现有的中文锚点（如 #简介、#与-botzone-差异一览）对齐。 */
function headingId(text: string): string {
  return text
    .toLowerCase()
    .replace(/\s+/g, '-')
    .replace(/[^\w\u4e00-\u9fff-]/g, '')
}

/** 行内格式：`code`、**bold**、![alt](src)、[text](href) */
function inline(s: string): string {
  let out = escapeHtml(s)
  out = out.replace(
    /`([^`]+)`/g,
    '<code class="rounded bg-muted px-1 py-0.5 text-[0.85em] text-primary">$1</code>',
  )
  out = out.replace(/\*\*([^*]+)\*\*/g, '<strong class="font-semibold text-foreground">$1</strong>')
  out = out.replace(
    /!\[([^\]]*)\]\(([^)]+)\)/g,
    '<img class="my-3 max-w-full rounded-lg border border-border" alt="$1" src="$2" />',
  )
  out = out.replace(
    /\[([^\]]+)\]\(([^)]+)\)/g,
    '<a class="text-primary underline hover:opacity-80" href="$2">$1</a>',
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
      html: `<pre class="my-3 overflow-x-auto rounded-lg bg-muted p-3 text-xs leading-relaxed text-foreground">${escapeHtml(
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
            `<th class="border border-border bg-muted px-3 py-2 text-left font-semibold text-foreground">${inline(
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
              `<td class="border border-border px-3 py-2 align-top text-muted-foreground">${inline(
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
          '<ol class="my-2 ml-5 list-decimal space-y-1 text-muted-foreground">' +
          items.map((l) => `<li>${inline(l.replace(/^\s*\d+\.\s+/, ''))}</li>`).join('') +
          '</ol>',
      }
    }
    return {
      tag: 'ul',
      html:
        '<ul class="my-2 ml-5 list-disc space-y-1 text-muted-foreground">' +
        items.map((l) => `<li>${inline(l.replace(/^\s*[-*]\s+/, ''))}</li>`).join('') +
        '</ul>',
    }
  }
  // 标题
  const m = /^(#{1,4})\s+(.*)$/.exec(lines[0])
  if (m && lines.length === 1) {
    const level = m[1].length
    const cls = [
      'mt-2 mb-3 text-xl font-bold text-foreground',
      'mt-5 mb-2 text-lg font-semibold text-foreground',
      'mt-4 mb-2 text-base font-semibold text-foreground',
      'mt-3 mb-1.5 text-sm font-semibold text-foreground',
    ][level - 1]
    return { tag: `h${level}`, html: `<h${level} id="${headingId(m[2])}" class="${cls}">${escapeHtml(m[2])}</h${level}>` }
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
        html: `<blockquote class="my-3 border-l-2 border-primary bg-primary/10 px-3 py-2 text-sm text-muted-foreground">${inline(
          text,
        )}</blockquote>`,
      }
    }
  }
  // 段落（多行用 <br/> 连）
  const text = lines.join(' ').trim()
  if (!text) return { tag: 'space', html: '<div class="h-2"></div>' }
  return { tag: 'p', html: `<p class="mb-2 leading-relaxed text-muted-foreground">${inline(text)}</p>` }
}

export function renderMarkdown(md: string): string {
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
