import { useCallback, useState } from 'react'
import { Link } from 'react-router-dom'
import { Laptop, ShieldX, Wifi, WifiOff } from 'lucide-react'
import { toast } from 'sonner'

import { apiFetch, apiJson, errMsg } from '@/api'
import Pagination from '@/components/Pagination'
import {
  localAgentBotName,
  localAgentStatus,
  type LocalAIAgent,
} from '@/components/runtime-environment'
import { DataRegion, StickyToolbar } from '@/components/layout'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { EmptyState, ErrorMsg, Loading, RefreshBtn } from '@/components/ui/status'
import { EntityName, Identifier } from '@/components/ui/overflow-text'
import { useConfirm } from '@/hooks/use-confirm'
import { useSingleFlightPolling } from '@/hooks/use-single-flight-polling'
import { fmtTime } from '@/lib/format'
import { gameLabel } from '@/lib/games'

interface AdminLocalAIAgent extends LocalAIAgent {
  owner_id: number
  owner_name: string
  owner_display_name: string
  created_at?: string | null
}

interface AgentPage {
  items: AdminLocalAIAgent[]
  page: number
  per_page: number
  total: number
}

const PER_PAGE = 20

export default function LocalAIConnectionsTab() {
  const [confirm, confirmDialog] = useConfirm()
  const [items, setItems] = useState<AdminLocalAIAgent[]>([])
  const [page, setPage] = useState(1)
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [busyId, setBusyId] = useState('')

  const load = useCallback(async (signal: AbortSignal) => {
    const result = await apiFetch<AgentPage>(
      `/api/admin/local-ai/agents?page=${page}&per_page=${PER_PAGE}`,
      { method: 'GET', signal },
    )
    if (signal.aborted) return
    setItems(result.items || [])
    setTotal(result.total || 0)
  }, [page])

  const { refresh, polling, offline } = useSingleFlightPolling({
    task: load,
    intervalMs: 5_000,
    maxIntervalMs: 40_000,
    onSuccess: () => {
      setLoading(false)
      setError('')
    },
    onError: (reason) => {
      setLoading(false)
      setError(errMsg(reason, '本地连接加载失败'))
    },
  })

  const revoke = async (agent: AdminLocalAIAgent) => {
    if (!await confirm({
      title: '撤销本地连接',
      desc: `撤销后，${agent.owner_display_name || agent.owner_name} 的“${agent.label}”会立即断开，进行中的本地 Bot 决策将按技术故障处理。`,
      confirmText: '撤销连接',
      danger: true,
    })) return
    setBusyId(agent.public_id)
    try {
      await apiJson(
        `/api/admin/local-ai/agents/${encodeURIComponent(agent.public_id)}`,
        'DELETE',
      )
      toast.success('本地连接已撤销')
      refresh()
    } catch (reason) {
      setError(errMsg(reason, '撤销本地连接失败'))
    } finally {
      setBusyId('')
    }
  }

  const onlineCount = items.filter((agent) => agent.is_online).length
  const availableCount = items.filter((agent) => agent.is_available).length

  return (
    <div className="min-w-0 space-y-3">
      <StickyToolbar label="本地连接工具栏" className="justify-between">
        <p className="min-w-0 text-xs text-muted-foreground">
          共 {total} 个连接 · 本页在线 {onlineCount} · 可接任务 {availableCount}
        </p>
        <RefreshBtn onClick={refresh} />
      </StickyToolbar>

      {(error || offline) && (
        <ErrorMsg
          msg={offline ? '当前离线；以下保留上次成功获取的连接状态。' : error}
        />
      )}

      <DataRegion
        title="用户本地连接"
        description="查看连接归属与占用状态；令牌不会显示在管理端。"
        contentClassName="min-w-0"
        data-testid="admin-local-ai-connections"
      >
        {loading && items.length === 0 ? (
          <Loading text="正在读取本地连接…" className="py-6" />
        ) : items.length === 0 ? (
          <EmptyState
            text="当前没有本地 Bot 连接"
            icon={<Laptop className="size-6 opacity-40" />}
            className="py-6"
          />
        ) : (
          <ul className="divide-y divide-border" aria-busy={polling}>
            {items.map((agent) => {
              const state = localAgentStatus(agent)
              return (
                <li
                  key={agent.public_id}
                  className="grid min-w-0 gap-2 px-3 py-2.5 text-sm md:grid-cols-[minmax(0,1.2fr)_minmax(0,1fr)_minmax(7rem,.65fr)_auto] md:items-center"
                >
                  <div className="min-w-0">
                    <div className="flex min-w-0 items-center gap-2">
                      {agent.is_online ? (
                        <Wifi className="size-4 shrink-0 text-primary" aria-hidden="true" />
                      ) : (
                        <WifiOff className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
                      )}
                      <EntityName>{localAgentBotName(agent)}</EntityName>
                      <Badge variant={state.available ? 'default' : agent.status === 'revoked' ? 'destructive' : 'secondary'}>
                        {state.label}
                      </Badge>
                    </div>
                    <p className="mt-0.5 min-w-0 text-xs text-muted-foreground">
                      <span className="font-medium text-foreground">{agent.label}</span>
                      <span> · {gameLabel(agent.game_id)}</span>
                    </p>
                  </div>

                  <div className="min-w-0 text-xs">
                    <Link
                      to={`/user/${encodeURIComponent(agent.owner_name)}`}
                      className="block min-w-0 text-primary hover:underline"
                    >
                      <EntityName tooltipFocusable={false}>
                        {agent.owner_display_name || agent.owner_name}
                      </EntityName>
                    </Link>
                    <Identifier tooltipFocusable={false}>@{agent.owner_name}</Identifier>
                  </div>

                  <div className="min-w-0 text-xs text-muted-foreground">
                    <div>{agent.is_online ? (agent.is_busy ? '正在执行对局' : '保持连接') : '当前离线'}</div>
                    <div className="mt-0.5 break-words [overflow-wrap:anywhere]">
                      {agent.last_seen_at ? `最近在线 ${fmtTime(agent.last_seen_at)}` : '尚未成功连接'}
                    </div>
                  </div>

                  <Button
                    type="button"
                    size="sm"
                    variant="destructive"
                    disabled={agent.status === 'revoked' || busyId === agent.public_id}
                    onClick={() => void revoke(agent)}
                    className="justify-self-start md:justify-self-end"
                  >
                    <ShieldX className="size-3.5" aria-hidden="true" />
                    {agent.status === 'revoked' ? '已撤销' : busyId === agent.public_id ? '撤销中…' : '撤销'}
                  </Button>
                </li>
              )
            })}
          </ul>
        )}
      </DataRegion>

      <Pagination
        page={page}
        perPage={PER_PAGE}
        total={total}
        onPageChange={setPage}
      />
      {confirmDialog}
    </div>
  )
}
