import { useEffect, useState } from 'react'
import { ChevronDown, FileCode2, Scale } from 'lucide-react'

import { apiGet, errMsg } from '@/api'
import { DataRegion, PageFrame, PageHeader, StickyToolbar, SummaryStrip } from '@/components/layout'
import { Button } from '@/components/ui/button'
import { Identifier } from '@/components/ui/overflow-text'
import { EmptyState, ErrorMsg, Loading } from '@/components/ui/status'
import { CopyIdentifier, SummaryMetric } from '@/pages/public-page-ui'

interface JudgeGameMeta {
  game_id: string
  label: string
  code_path: string
  summary: string
  source_files: string[]
}

interface SourceFile {
  name: string
  path: string
  source: string
}

interface SourceResp {
  game_id: string
  label: string
  summary: string
  files: SourceFile[]
}

export default function Judges() {
  const [games, setGames] = useState<JudgeGameMeta[]>([])
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let alive = true
    apiGet<{ games: JudgeGameMeta[] }>('/api/judges')
      .then((data) => {
        if (alive) setGames(data.games || [])
      })
      .catch((cause) => {
        if (alive) setError(errMsg(cause, '加载失败'))
      })
      .finally(() => {
        if (alive) setLoading(false)
      })
    return () => {
      alive = false
    }
  }, [])

  const sourceCount = games.reduce((sum, game) => sum + game.source_files.length, 0)

  return (
    <PageFrame width="default" layout="public-judges">
      <PageHeader
        eyebrow="公开审计"
        title="裁判源码"
        description="每款游戏的权威裁判以明文公开；规则定义、协议适配与共享实现均可逐文件核查。"
      />

      <SummaryStrip columns={3}>
        <SummaryMetric label="注册游戏" value={games.length} detail="公开裁判入口" icon={<Scale className="size-4" />} />
        <SummaryMetric label="源码文件" value={sourceCount} detail="按游戏白名单公开" icon={<FileCode2 className="size-4" />} />
        <SummaryMetric label="加载策略" value="按需" detail="展开游戏后请求源码" mono={false} />
      </SummaryStrip>

      {games.length > 0 && (
        <StickyToolbar label="裁判快速索引">
          <span className="shrink-0 text-xs font-medium text-muted-foreground">快速定位</span>
          {games.map((game) => (
            <Button
              key={game.game_id}
              type="button"
              variant="ghost"
              size="sm"
              onClick={() => document.getElementById(`judge-${game.game_id}`)?.scrollIntoView({ block: 'start' })}
            >
              {game.label}
            </Button>
          ))}
        </StickyToolbar>
      )}

      {error ? (
        <DataRegion title="裁判目录" contentClassName="px-4 py-6">
          <ErrorMsg msg={error} />
        </DataRegion>
      ) : loading ? (
        <DataRegion title="裁判目录"><Loading text="正在加载裁判目录…" /></DataRegion>
      ) : games.length === 0 ? (
        <DataRegion title="裁判目录"><EmptyState text="暂无公开裁判" icon={<Scale className="size-5 opacity-50" />} className="py-8" /></DataRegion>
      ) : (
        <div className="flex min-w-0 flex-col gap-[var(--page-section-gap)]">
          {games.map((game) => <JudgeRegion key={game.game_id} game={game} />)}
        </div>
      )}
    </PageFrame>
  )
}

function JudgeRegion({ game }: { game: JudgeGameMeta }) {
  const [open, setOpen] = useState(false)
  const [source, setSource] = useState<SourceResp | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [activeFile, setActiveFile] = useState(0)

  useEffect(() => {
    if (!open || source || loading || error) return
    setLoading(true)
    setError('')
    apiGet<SourceResp>(`/api/judges/${game.game_id}/source`)
      .then((data) => {
        setSource(data)
        setActiveFile(0)
      })
      .catch((cause) => setError(errMsg(cause, '源码加载失败')))
      .finally(() => setLoading(false))
  }, [error, game.game_id, loading, open, source])

  const file = source?.files[activeFile]

  return (
    <DataRegion
      id={`judge-${game.game_id}`}
      title={game.label}
      description={game.summary}
      className="scroll-mt-[calc(var(--sticky-page-offset)+var(--sticky-toolbar-height)+var(--sticky-toolbar-gap))]"
      actions={
        <Button type="button" variant="outline" size="sm" onClick={() => setOpen((value) => !value)}>
          <ChevronDown aria-hidden="true" className={`size-4 transition-transform motion-reduce:transition-none ${open ? 'rotate-180' : ''}`} />
          {open ? '收起源码' : '查看源码'}
        </Button>
      }
    >
      <div className="min-w-0 space-y-2 px-4 py-3">
        <div className="flex min-w-0 flex-wrap items-center gap-x-4 gap-y-1">
          <CopyIdentifier value={game.game_id} label="游戏 ID" />
          <span className="inline-flex min-w-0 items-center gap-1 text-xs text-muted-foreground">
            <span className="shrink-0">入口</span>
            <Identifier lines={2}>{game.code_path}</Identifier>
          </span>
        </div>
        <p className="break-words text-xs text-muted-foreground">
          公开文件：{game.source_files.join(' · ')}
        </p>

        {open && (
          <div className="min-w-0 space-y-3 border-t pt-3">
            {loading ? (
              <Loading text="正在加载源码…" className="py-7" />
            ) : error ? (
              <div className="space-y-2 py-3">
                <ErrorMsg msg={error} />
                <Button type="button" variant="outline" size="sm" onClick={() => setError('')}>重试</Button>
              </div>
            ) : source && source.files.length > 0 && file ? (
              <>
                <div className="flex min-w-0 flex-wrap gap-1.5" role="tablist" aria-label={`${game.label}源码文件`}>
                  {source.files.map((sourceFile, index) => (
                    <Button
                      key={sourceFile.name}
                      type="button"
                      role="tab"
                      aria-selected={index === activeFile}
                      variant={index === activeFile ? 'default' : 'outline'}
                      size="sm"
                      onClick={() => setActiveFile(index)}
                      className="max-w-full"
                    >
                      <FileCode2 aria-hidden="true" className="size-3.5" />
                      <span className="max-w-56 truncate">{sourceFile.name}</span>
                    </Button>
                  ))}
                </div>
                <pre
                  data-scroll-region="judge-source"
                  data-overflow-allowed="both"
                  role="region"
                  aria-label={`${file.name} 源码`}
                  tabIndex={0}
                  className="max-h-[32rem] min-w-0 overflow-auto overscroll-contain rounded-lg border bg-muted/40 p-3 font-mono text-xs leading-relaxed outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50"
                >
                  <code>{file.source}</code>
                </pre>
                <div className="flex min-w-0 items-center gap-1 text-xs text-muted-foreground">
                  <span className="shrink-0">路径</span>
                  <Identifier lines={2}>{file.path}</Identifier>
                </div>
              </>
            ) : (
              <EmptyState text="暂无公开源码文件" icon={<FileCode2 className="size-5 opacity-50" />} className="py-7" />
            )}
          </div>
        )}
      </div>
    </DataRegion>
  )
}
