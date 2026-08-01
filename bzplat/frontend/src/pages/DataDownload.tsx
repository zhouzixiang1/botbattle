import { useEffect, useState } from 'react'
import { AlertTriangle, Lock, Download } from 'lucide-react'
import PageStub from '@/components/PageStub'
import { Card } from '@/components/ui/card'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { EmptyState, ErrorMsg } from '@/components/ui/status'
import { useAuth } from '@/components/useAuth'
import { apiGet, errMsg } from '@/api'
import { GAMES, gameLabel } from '@/lib/games'

interface Pack {
  game_id: string
  month: string
  cnt: number
}

export default function DataDownload() {
  const { user } = useAuth()
  const [packs, setPacks] = useState<Pack[]>([])
  const [error, setError] = useState('')
  const [filterGame, setFilterGame] = useState('')

  useEffect(() => {
    const q = filterGame ? `?game_id=${encodeURIComponent(filterGame)}` : ''
    apiGet<{ packs: Pack[] }>(`/api/matchpacks${q}`)
      .then((d) => setPacks(d.packs || []))
      .catch((e) => setError(errMsg(e)))
  }, [filterGame])

  const level = user?.level ?? 0
  const gated = level < 1

  return (
    <PageStub title="对局数据集">
      <p className="mb-4 text-sm text-muted-foreground">
        下载已完成对局的数据集（按游戏 × 月份打包，gzip 压缩，每行一条 JSON 对局）。
        可用于 AI 训练、对局复盘与数据分析。
      </p>
      {gated && (
        <div className="mb-4 flex items-start gap-2 rounded-lg border border-warning/30 bg-warning/10 px-4 py-3 text-sm text-warning-foreground">
          <AlertTriangle className="mt-0.5 size-4 shrink-0" />
          <span>
            下载数据集需 <strong>等级 ≥ 1</strong>（当前 Lv.{level}）。多参与对局/赛事/评论即可升级。
          </span>
        </div>
      )}
      {error && <ErrorMsg msg={error} className="mb-3" />}
      <div className="mb-3">
        <label className="flex items-center gap-2 text-sm text-muted-foreground">
          筛选游戏
          <select
            value={filterGame}
            onChange={(e) => setFilterGame(e.target.value)}
            className="h-9 rounded-md border border-input bg-transparent px-3 text-sm text-foreground shadow-xs focus:outline-none focus:ring-2 focus:ring-ring"
          >
            <option value="">全部</option>
            {GAMES.map((g) => (
              <option key={g.id} value={g.id}>{g.label}</option>
            ))}
          </select>
        </label>
      </div>
      {packs.length === 0 ? (
        <EmptyState text="暂无可下载的数据集" />
      ) : (
        <Card>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>游戏</TableHead>
                <TableHead>月份</TableHead>
                <TableHead className="hidden sm:table-cell">对局数</TableHead>
                <TableHead>下载</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {packs.map((p) => (
                <TableRow key={`${p.game_id}-${p.month}`}>
                  <TableCell>{gameLabel(p.game_id)}</TableCell>
                  <TableCell className="font-mono text-muted-foreground">{p.month}</TableCell>
                  <TableCell className="hidden text-muted-foreground sm:table-cell">{p.cnt}</TableCell>
                  <TableCell>
                    {gated ? (
                      <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
                        <Lock className="size-3.5" />
                        需 Lv.1
                      </span>
                    ) : (
                      <a
                        href={`/api/matchpacks/download?game_id=${p.game_id}&month=${p.month}`}
                        className="inline-flex items-center gap-1 font-medium text-primary hover:underline"
                      >
                        <Download className="size-3.5" />
                        下载 .gz
                      </a>
                    )}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Card>
      )}
    </PageStub>
  )
}
