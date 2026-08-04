import { useEffect, useState } from 'react'
import { ChevronDown, FileCode2 } from 'lucide-react'
import PageStub from '@/components/PageStub'
import { Card } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Loading, ErrorMsg } from '@/components/ui/status'
import { apiGet } from '@/api'

/** GET /api/judges 返回的裁判元信息列表 */
interface JudgeGameMeta {
  game_id: string
  label: string
  code_path: string
  summary: string
  source_files: string[]
}

interface JudgesResp {
  games: JudgeGameMeta[]
}

/** GET /api/judges/{id}/source 返回的源码文件 */
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
  const [games, setGames] = useState<JudgeGameMeta[] | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let alive = true
    setLoading(true)
    apiGet<JudgesResp>('/api/judges')
      .then((d) => {
        if (alive) setGames(d.games)
      })
      .catch((e) => alive && setError(typeof e === 'string' ? e : '加载失败'))
      .finally(() => alive && setLoading(false))
    return () => {
      alive = false
    }
  }, [])

  return (
    <PageStub
      title="裁判"
      subtitle="平台每款游戏的裁判是公开可审计的明文代码——规则透明，公正可查。点开查看各游戏裁判引擎的完整源码。"
    >
      {loading ? (
        <Loading />
      ) : error ? (
        <ErrorMsg msg={error} />
      ) : (
        <div className="space-y-4">
          {games?.map((g) => (
            <JudgeCard key={g.game_id} game={g} />
          ))}
        </div>
      )}
    </PageStub>
  )
}

/** 单个游戏裁判卡片：展示 summary + 折叠的源码全文（懒加载源码）。 */
function JudgeCard({ game }: { game: JudgeGameMeta }) {
  const [open, setOpen] = useState(false)
  const [source, setSource] = useState<SourceResp | null>(null)
  const [srcError, setSrcError] = useState('')
  const [srcLoading, setSrcLoading] = useState(false)
  const [activeFile, setActiveFile] = useState(0)

  // 懒加载：首次展开时才请求源码
  useEffect(() => {
    if (!open || source || srcLoading) return
    setSrcLoading(true)
    apiGet<SourceResp>(`/api/judges/${game.game_id}/source`)
      .then((d) => {
        setSource(d)
        setActiveFile(0)
      })
      .catch((e) => setSrcError(typeof e === 'string' ? e : '源码加载失败'))
      .finally(() => setSrcLoading(false))
  }, [open, source, srcLoading, game.game_id])

  return (
    <Card className="p-4 sm:p-5">
      <div className="flex flex-col gap-1">
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="text-lg font-semibold text-primary">{game.label}</h2>
          <code className="rounded bg-muted px-1.5 py-0.5 text-xs text-muted-foreground">
            {game.game_id}
          </code>
        </div>
        <p className="text-sm text-muted-foreground">{game.summary}</p>
        <p className="text-xs text-muted-foreground">
          公开源码文件：{game.source_files.join(', ')}
        </p>
      </div>

      <Button
        type="button"
        variant="outline"
        size="sm"
        className="mt-3"
        onClick={() => setOpen((v) => !v)}
      >
        <ChevronDown className={`size-4 transition-transform ${open ? 'rotate-180' : ''}`} />
        {open ? '收起裁判源码' : '查看裁判源码'}
      </Button>

      {open && (
        <div className="mt-3">
          {srcLoading ? (
            <Loading />
          ) : srcError ? (
            <ErrorMsg msg={srcError} />
          ) : source && source.files.length > 0 ? (
            <div className="space-y-2">
              {/* 文件切换 */}
              <div className="flex flex-wrap gap-1.5">
                {source.files.map((f, i) => (
                  <Button
                    key={f.name}
                    type="button"
                    variant={i === activeFile ? 'default' : 'outline'}
                    size="sm"
                    className="gap-1.5"
                    onClick={() => setActiveFile(i)}
                  >
                    <FileCode2 className="size-3.5" />
                    {f.name}
                  </Button>
                ))}
              </div>
              {/* 源码全文（等宽、可横向滚动） */}
              <pre className="max-h-[32rem] overflow-auto rounded-lg border border-border bg-muted/50 p-3 text-xs leading-relaxed">
                <code>{source.files[activeFile].source}</code>
              </pre>
              <p className="text-xs text-muted-foreground">
                路径：{source.files[activeFile].path}
              </p>
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">暂无公开源码文件。</p>
          )}
        </div>
      )}
    </Card>
  )
}
