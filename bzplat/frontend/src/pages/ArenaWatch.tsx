/**
 * /arena 入口：无 id 时展示引导；有 id（?id=）时重定向到统一对局页 /match/:id。
 * 旧 /watch/:id 已在 app-shell 重定向；本页保留列表式引导，避免双实现漂移。
 */
import { Link, Navigate, useSearchParams } from 'react-router-dom'
import { Radio, ArrowRight } from 'lucide-react'
import PageStub from '@/components/PageStub'
import { Card, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'

export default function ArenaWatch() {
  const [sp] = useSearchParams()
  const id = sp.get('id') || ''

  // 带 id → 统一观赛/回放页（seats/胜者/累计 全量）
  if (id) {
    return <Navigate to={`/match/${encodeURIComponent(id)}`} replace />
  }

  return (
    <PageStub title="观赛">
      <Card className="mt-4 overflow-hidden border-primary/20">
        <CardContent className="flex flex-col items-center gap-3 bg-gradient-to-br from-primary/5 via-card to-card px-4 py-16 text-center">
          <Radio className="size-10 text-primary/60" />
          <p className="text-lg font-medium tracking-wide text-foreground">对局观赛区</p>
          <p className="text-sm text-muted-foreground">
            从首页或对局历史打开对局，统一走 <code className="text-xs">/match/:id</code>（实时 + 回放）
          </p>
          <div className="mt-2 flex flex-wrap justify-center gap-2">
            <Button asChild variant="outline" size="sm" className="gap-1.5">
              <Link to="/">最新对局<ArrowRight className="size-3.5" /></Link>
            </Button>
            <Button asChild variant="outline" size="sm" className="gap-1.5">
              <Link to="/history">对局历史<ArrowRight className="size-3.5" /></Link>
            </Button>
          </div>
        </CardContent>
      </Card>
    </PageStub>
  )
}
