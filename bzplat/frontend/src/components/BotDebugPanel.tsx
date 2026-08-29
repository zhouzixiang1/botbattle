import { useState } from 'react'
import { Bug, ChevronDown, Copy } from 'lucide-react'
import { toast } from 'sonner'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from '@/components/ui/collapsible'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from '@/components/ui/tooltip'

export interface BotDebugEntry {
  seat: 0 | 1
  turn: number
  leg: number | null
  debug: unknown
}

export interface BotDebugPayload {
  match_id: string
  entries: BotDebugEntry[]
  entry_count: number
  total_bytes: number
  dropped_count: number
  updated_at: string | null
}

function debugText(value: unknown): string {
  if (typeof value === 'string') return value
  const encoded = JSON.stringify(value, null, 2)
  return encoded === undefined ? String(value) : encoded
}

export default function BotDebugPanel({
  payload,
  seatNames,
}: {
  payload: BotDebugPayload
  seatNames: [string, string]
}) {
  const [open, setOpen] = useState(false)
  const bySeat = ([0, 1] as const).map((seat) =>
    payload.entries.filter((entry) => entry.seat === seat),
  )

  // 有权限但没有任何实际输出时不占据页面层级；权限本身不是可展示内容。
  if (payload.entry_count <= 0) return null

  const copyText = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text)
      toast.success('调试信息已复制')
    } catch {
      toast.error('复制失败，请手动选择文本')
    }
  }

  return (
    <Collapsible open={open} onOpenChange={setOpen}>
      <Card data-testid="bot-debug-panel" className="mb-3 min-w-0 gap-0 overflow-hidden py-0">
        <CollapsibleTrigger asChild>
          <Button
            variant="ghost"
            className="h-auto w-full min-w-0 justify-start rounded-none px-4 py-3 text-left"
            aria-label={open ? '收起 Bot 调试信息' : '展开 Bot 调试信息'}
          >
            <Bug className="size-4 shrink-0 text-primary" />
            <span className="min-w-0 flex-1">
              <span className="block font-medium">Bot 调试信息</span>
              <span className="block truncate text-xs font-normal text-muted-foreground">
                仅参赛 Bot 作者、赛事组织者与管理员可见
              </span>
            </span>
            <Badge variant="secondary" className="shrink-0">
              {payload.entry_count} 条
            </Badge>
            <ChevronDown
              className={`size-4 shrink-0 transition-transform ${open ? 'rotate-180' : ''}`}
            />
          </Button>
        </CollapsibleTrigger>
        <CollapsibleContent>
          <CardContent className="min-w-0 border-t border-border px-3 py-3 sm:px-4">
            {payload.dropped_count > 0 && (
              <p className="mb-2 break-words text-xs text-muted-foreground">
                有 {payload.dropped_count} 条内容因安全或容量上限未保存。
              </p>
            )}
            <Tabs defaultValue="0" className="min-w-0">
              <TabsList className="grid w-full grid-cols-2">
                {([0, 1] as const).map((seat) => (
                  <TabsTrigger key={seat} value={String(seat)} className="min-w-0">
                    <span className="truncate">{seatNames[seat]} · 座位 {seat + 1}</span>
                    <span className="ml-1 text-[10px] opacity-70">({bySeat[seat].length})</span>
                  </TabsTrigger>
                ))}
              </TabsList>
              {([0, 1] as const).map((seat) => {
                const entries = bySeat[seat]
                const allText = entries.map((entry) => {
                  const game = entry.leg == null ? '' : ` · 第 ${entry.leg + 1} 场`
                  return `第 ${entry.turn} 次决策${game}\n${debugText(entry.debug)}`
                }).join('\n\n')
                return (
                  <TabsContent key={seat} value={String(seat)} className="min-w-0">
                    <div className="mb-2 flex min-w-0 items-center justify-between gap-2">
                      <span className="min-w-0 truncate text-xs text-muted-foreground">
                        {entries.length ? `${entries.length} 次有调试输出的决策` : '该 Bot 未输出 debug'}
                      </span>
                      {entries.length > 0 && (
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <Button
                              variant="outline"
                              size="sm"
                              className="shrink-0 gap-1"
                              onClick={() => void copyText(allText)}
                            >
                              <Copy className="size-3.5" />复制本座位
                            </Button>
                          </TooltipTrigger>
                          <TooltipContent>复制当前座位全部安全调试文本</TooltipContent>
                        </Tooltip>
                      )}
                    </div>
                    {entries.length > 0 && (
                      <div className="max-h-[28rem] min-w-0 space-y-2 overflow-y-auto overflow-x-hidden pr-1">
                        {entries.map((entry, index) => {
                          const text = debugText(entry.debug)
                          return (
                            <div
                              key={`${entry.seat}-${entry.leg ?? 'single'}-${entry.turn}-${index}`}
                              className="min-w-0 max-w-full rounded-md border border-border bg-muted/25 p-2.5"
                            >
                              <div className="mb-1.5 flex min-w-0 items-center gap-2">
                                <span className="min-w-0 flex-1 truncate text-xs font-medium text-foreground">
                                  第 {entry.turn} 次决策
                                  {entry.leg == null ? '' : ` · 第 ${entry.leg + 1} 场`}
                                </span>
                                <Tooltip>
                                  <TooltipTrigger asChild>
                                    <Button
                                      variant="ghost"
                                      size="icon-sm"
                                      className="shrink-0"
                                      aria-label={`复制 ${seatNames[seat]} 第 ${entry.turn} 次调试信息`}
                                      onClick={() => void copyText(text)}
                                    >
                                      <Copy className="size-3.5" />
                                    </Button>
                                  </TooltipTrigger>
                                  <TooltipContent>复制这一条</TooltipContent>
                                </Tooltip>
                              </div>
                              <pre className="max-w-full whitespace-pre-wrap break-words font-mono text-[11px] leading-relaxed text-muted-foreground [overflow-wrap:anywhere]">
                                {text}
                              </pre>
                            </div>
                          )
                        })}
                      </div>
                    )}
                  </TabsContent>
                )
              })}
            </Tabs>
          </CardContent>
        </CollapsibleContent>
      </Card>
    </Collapsible>
  )
}
