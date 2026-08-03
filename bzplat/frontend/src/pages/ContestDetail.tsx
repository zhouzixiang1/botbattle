import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { Trophy, Users, Swords, ListOrdered, Play, DoorOpen, RefreshCw, Timer, ChevronDown, ChevronRight, Plus, Download } from 'lucide-react'
import PageStub from '@/components/PageStub'
import { Card, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { ErrorMsg, EmptyState, Loading, StatusBadge } from '@/components/ui/status'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import BracketTree from '@/components/contest/BracketTree'
import { useAuth } from '@/components/useAuth'
import { apiGet, apiJson, errMsg } from '@/api'

interface Contest {
  id: number
  title: string
  description?: string
  status: string
  organizer_id: number
  hands_per_match?: number
  template_id?: string
  game_id?: string
  stages_json?: string
  current_stage_idx?: number
  rest_ends_at?: string | null
  require_real_name?: number
}
interface Stage {
  key?: string
  type?: string
  scoring?: string
  rounds?: number
  group_count?: number
  advance_count?: number
  advance_per_group?: number
  rest_after_minutes?: number
  allow_bot_swap_in_rest?: boolean
}
interface Entry {
  id: number
  user_id: number
  bot_id: number
  registered_at?: string
  group_id?: string
  seed?: number
  eliminated?: number
  bot_name?: string
  bot_display?: string
  owner_name?: string
  owner_display?: string
}
interface Pairing {
  id: number
  round_num?: number
  bracket_slot?: number | null
  bot_a_id: number
  bot_b_id: number
  match_id?: string | null
  status?: string
  stage_idx?: number
  stage_key?: string
  group_id?: string
  bot_a_name?: string
  bot_a_display?: string
  bot_b_name?: string
  bot_b_display?: string
  owner_a_name?: string
  owner_b_name?: string
  match_winner?: number | null
}
interface Standing {
  bot_id: number
  points: number
  wins: number
  draws: number
  losses: number
  net_chips: number
  group_id?: string
  bot_name?: string
}

function parseStages(c: Contest | null): Stage[] {
  if (!c?.stages_json) return []
  try {
    return JSON.parse(c.stages_json)
  } catch {
    return []
  }
}

function RestCountdown({ endsAt }: { endsAt: string }) {
  const [left, setLeft] = useState('')
  useEffect(() => {
    const tick = () => {
      const ms = new Date(endsAt).getTime() - Date.now()
      if (ms <= 0) {
        setLeft('已到时')
        return
      }
      const s = Math.floor(ms / 1000)
      const m = Math.floor(s / 60)
      const r = s % 60
      setLeft(`${m}:${r.toString().padStart(2, '0')}`)
    }
    tick()
    const id = setInterval(tick, 1000)
    return () => clearInterval(id)
  }, [endsAt])
  return <span className="font-mono font-semibold text-primary">{left}</span>
}

export default function ContestDetail() {
  const { id } = useParams()
  const { user, isLoggedIn } = useAuth()
  const [contest, setContest] = useState<Contest | null>(null)
  const [entries, setEntries] = useState<Entry[]>([])
  const [pairings, setPairings] = useState<Pairing[]>([])
  const [standings, setStandings] = useState<Standing[]>([])
  const [estimate, setEstimate] = useState<{ estimated_matches?: number; eta_seconds?: number } | null>(null)
  const [bots, setBots] = useState<Array<{ id: number; name: string; display_name?: string }>>([])
  const [botId, setBotId] = useState('')
  const [stageTab, setStageTab] = useState(0)
  const [error, setError] = useState('')

  const stages = useMemo(() => parseStages(contest), [contest])

  const load = useCallback(() => {
    if (!id) return
    apiGet<{
      contest: Contest
      entries: Entry[]
      pairings: Pairing[]
      standings: Standing[]
      estimate?: { estimated_matches?: number; eta_seconds?: number }
    }>(`/api/contests/${id}`)
      .then((d) => {
        setContest(d.contest)
        setEntries(d.entries || [])
        setPairings(d.pairings || [])
        setStandings(d.standings || [])
        setEstimate(d.estimate || null)
        setStageTab(d.contest.current_stage_idx ?? 0)
      })
      .catch((e) => setError(errMsg(e)))
  }, [id])

  useEffect(() => {
    void load()
  }, [load])

  useEffect(() => {
    if (isLoggedIn && contest?.game_id) {
      apiGet<{ bots: Array<{ id: number; name: string; display_name?: string }> }>(
        `/api/bots/mine?game_id=${contest.game_id}`,
      )
        .then((d) => setBots(d.bots || []))
        .catch(() => undefined)
    }
  }, [isLoggedIn, contest?.game_id])

  const isOrg = !!user && !!contest && (user.role === 'admin' || user.id === contest.organizer_id)
  const myEntry = entries.find((e) => e.user_id === user?.id)
  // 实名校验：赛事要求实名且当前用户未填完整 → 提示去设置页补填
  const needsRealName = !!contest?.require_real_name && !!user && !(
    user.real_name && user.phone && user.school && user.student_id
  )

  const act = async (path: string, body?: unknown) => {
    setError('')
    try {
      await apiJson(path, 'POST', body)
      await load()
    } catch (e) {
      setError(errMsg(e))
    }
  }

  const stagePairings = pairings.filter((p) => (p.stage_idx ?? 0) === stageTab)
  const curStageType = stages[stageTab]?.type as string | undefined
  const isElimStage = curStageType === 'single_elimination' || curStageType === 'double_elimination'

  if (!contest) {
    return (
      <PageStub title="比赛详情">
        {error ? <ErrorMsg msg={error} /> : <Loading />}
      </PageStub>
    )
  }


  return (
    <PageStub title="比赛详情">
      {/* 头部信息卡 */}
      <Card>
        <CardContent className="py-4">
          <div className="flex flex-wrap items-start justify-between gap-2">
            <div className="min-w-0 flex-1">
              <h2 className="text-lg font-semibold text-foreground">{contest.title}</h2>
              <p className="mt-1 text-sm text-muted-foreground">{contest.description || '无说明'}</p>
              <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                <StatusBadge status={contest.status} />
                <span>模板 {contest.template_id}</span>
                <span>· 游戏 {contest.game_id || 'holdem'}</span>
                <span>· 每场 {contest.hands_per_match ?? 70} 手</span>
                {estimate?.estimated_matches != null && (
                  <Badge variant="outline" className="text-[10px]">预估 {estimate.estimated_matches} 场</Badge>
                )}
              </div>
            </div>
            <Tooltip>
              <TooltipTrigger asChild>
                <Button variant="ghost" size="icon" onClick={() => void load()}>
                  <RefreshCw className="size-4" />
                </Button>
              </TooltipTrigger>
              <TooltipContent>刷新</TooltipContent>
            </Tooltip>
          </div>
          {contest.status === 'rest' && contest.rest_ends_at && (
            <div className="mt-2 flex items-center gap-1.5 rounded-lg bg-warning/10 px-3 py-2 text-sm text-warning-foreground">
              <Timer className="size-4 text-warning" />
              阶段休息中，倒计时 <RestCountdown endsAt={contest.rest_ends_at} />（可更换派遣 Bot）
            </div>
          )}
        </CardContent>
      </Card>

      {error && <ErrorMsg msg={error} className="mt-3" />}

      {/* 操作区 */}
      <div className="mt-4 flex flex-wrap gap-2">
        {isOrg && contest.status === 'draft' && (
          <Button onClick={() => void act(`/api/contests/${id}/open`)} className="gap-1.5">
            <DoorOpen className="size-4" />开放报名
          </Button>
        )}
        {isOrg && (contest.status === 'open' || contest.status === 'draft') && (
          <Button variant="outline" onClick={() => void act(`/api/contests/${id}/start`)} className="gap-1.5">
            <Play className="size-4" />开始比赛
          </Button>
        )}
        {isOrg && contest.status === 'rest' && (
          <Button onClick={() => void act(`/api/contests/${id}/resume`)}>结束休息 / 下一阶段</Button>
        )}
        {isLoggedIn && contest.status === 'open' && (
          <div className="flex flex-wrap items-center gap-2">
            {needsRealName && (
              <span className="text-sm text-amber-600 dark:text-amber-500">
                本赛事要求实名报名，请先{' '}
                <Link to="/settings" className="font-medium underline">填写实名信息</Link>
              </span>
            )}
            <Select value={botId} onValueChange={setBotId}>
              <SelectTrigger className="h-9 w-[12rem]">
                <SelectValue placeholder="选择我的 Bot" />
              </SelectTrigger>
              <SelectContent>
                {bots.map((b) => (<SelectItem key={b.id} value={String(b.id)}>{b.display_name || b.name}</SelectItem>))}
              </SelectContent>
            </Select>
            <Button variant="outline" disabled={!botId || needsRealName} onClick={() => void act(`/api/contests/${id}/register`, { bot_id: Number(botId) })}>
              报名派遣
            </Button>
          </div>
        )}
        {isLoggedIn && myEntry && contest.status === 'rest' && (
          <div className="flex flex-wrap items-center gap-2">
            <Select value={botId} onValueChange={setBotId}>
              <SelectTrigger className="h-9 w-[12rem]">
                <SelectValue placeholder={`更换 Bot（当前 #${myEntry.bot_id}）`} />
              </SelectTrigger>
              <SelectContent>
                {bots.map((b) => (<SelectItem key={b.id} value={String(b.id)}>{b.display_name || b.name}</SelectItem>))}
              </SelectContent>
            </Select>
            <Button variant="outline" disabled={!botId} onClick={() => void act(`/api/contests/${id}/dispatch`, { bot_id: Number(botId) })}>
              确认更换
            </Button>
          </div>
        )}
      </div>

      {/* 阶段 Tabs */}
      {stages.length > 0 && (
        <Tabs value={String(stageTab)} onValueChange={(v) => setStageTab(Number(v))} className="mt-6">
          <TabsList>
            {stages.map((s, i) => (
              <TabsTrigger key={s.key || i} value={String(i)} className="gap-1.5">
                {s.key || `阶段${i + 1}`}
                <span className="text-xs text-muted-foreground">({s.type})</span>
                {contest.current_stage_idx === i && contest.status !== 'finished' && (
                  <Badge variant="outline" className="ml-1 text-[9px] text-primary">当前</Badge>
                )}
              </TabsTrigger>
            ))}
          </TabsList>
        </Tabs>
      )}

      {/* 阶段配置 */}
      {stages[stageTab] && (
        <div className="mt-2 rounded-lg bg-muted/50 px-3 py-2 text-xs text-muted-foreground">
          <span className="font-medium text-foreground">本阶段配置：</span>
          {[
            stages[stageTab].type,
            stages[stageTab].scoring,
            stages[stageTab].group_count ? `分组=${stages[stageTab].group_count}` : null,
            stages[stageTab].rounds !== undefined ? `轮数=${stages[stageTab].rounds}` : null,
            stages[stageTab].advance_count ? `晋级=${stages[stageTab].advance_count}` : null,
            stages[stageTab].advance_per_group ? `每组晋级=${stages[stageTab].advance_per_group}` : null,
            stages[stageTab].rest_after_minutes ? `休息=${stages[stageTab].rest_after_minutes}分` : null,
            stages[stageTab].allow_bot_swap_in_rest ? '休息可换Bot' : null,
          ].filter(Boolean).join(' · ')}
        </div>
      )}

      {/* 区2：双栏 = 对阵主区(左, 核心视觉) + 报名/积分边栏(右, sticky)。 <lg 单列堆叠 */}
      <div className="mt-6 lg:grid lg:grid-cols-[minmax(0,1fr)_22rem] lg:gap-6">
        {/* 左主区：对阵（BracketTree/PairedFoldedList 能吃满宽） */}
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <Swords className="size-4 text-muted-foreground" />
            <h3 className="text-sm font-semibold text-foreground">
              对阵{stages.length ? ` · ${stages[stageTab]?.key || `阶段${stageTab + 1}`}` : ''}
            </h3>
          </div>
          {stagePairings.length === 0 ? (
            <Card className="mt-2"><EmptyState text="暂无对阵" icon={<Swords className="size-7 opacity-40" />} /></Card>
          ) : isElimStage ? (
            // 淘汰赛：树状对阵图（按 bracket_slot 排列，胜者高亮，横向滚动+轮次折叠）
            <div className="mt-2">
              <BracketTree pairings={stagePairings} />
            </div>
          ) : (
            // swiss/循环/分组：按轮次/分组折叠的列表（大规模自动收起）
            <PairingFoldedList pairings={stagePairings} />
          )}
        </div>

        {/* 右边栏：报名列表 + 积分榜（桌面 sticky 常驻） */}
        <div className="mt-6 space-y-6 lg:mt-0 lg:self-start lg:sticky lg:top-20">
          {/* 报名列表 */}
          <div>
            <div className="flex items-center gap-2">
              <Users className="size-4 text-muted-foreground" />
              <h3 className="text-sm font-semibold text-foreground">报名（{entries.length}）</h3>
              {isOrg && (contest.status === 'draft' || contest.status === 'open') && (
                <Button
                  size="sm"
                  variant="outline"
                  className="ml-auto h-7 gap-1 text-xs"
                  onClick={() => void act(`/api/contests/${id}/entries/bulk`, { assign_all: true, game_id: contest.game_id })}
                >
                  <Plus className="size-3" />批量指派
                </Button>
              )}
            </div>
            <Card className="mt-2">
              <CardContent className="py-3">
                {entries.length === 0 ? (
                  <EmptyState text="暂无报名" className="py-6" />
                ) : (
                  <ul className="space-y-1.5 text-sm">
                    {entries.map((e) => (
                      <li key={e.id} className="flex flex-wrap items-center gap-2">
                        <Link to={`/bot/${e.bot_id}`} className="font-medium text-foreground hover:text-primary">
                          {e.bot_display || e.bot_name || `#${e.bot_id}`}
                        </Link>
                        {e.owner_name && (
                          <Link to={`/user/${encodeURIComponent(e.owner_name)}`} className="text-xs text-muted-foreground hover:text-primary">
                            @{e.owner_display || e.owner_name}
                          </Link>
                        )}
                        {e.seed ? <span className="text-xs text-muted-foreground">种子 {e.seed}</span> : ''}
                        {e.group_id && <Badge variant="secondary" className="text-[10px]">{e.group_id}</Badge>}
                        {e.eliminated ? <Badge variant="destructive" className="text-[10px]">淘汰</Badge> : ''}
                        {isOrg && (contest.status === 'draft' || contest.status === 'open') && (
                          <Button
                            size="sm"
                            variant="ghost"
                            className="ml-auto h-6 px-2 text-xs text-destructive"
                            onClick={() => void apiJson(`/api/contests/${id}/entries/${e.user_id}`, 'DELETE').then(load).catch((x) => setError(errMsg(x)))}
                          >
                            移除
                          </Button>
                        )}
                      </li>
                    ))}
                  </ul>
                )}
              </CardContent>
            </Card>
          </div>

          {/* P5：全员正式名次（赛事 finished 时显示 + 下载） */}
          {contest.status === 'finished' && (
            <div>
              <div className="flex items-center gap-2">
                <Trophy className="size-4 text-muted-foreground" />
                <h3 className="text-sm font-semibold text-foreground">正式名次</h3>
                <a
                  href={`/api/contests/${id}/official-results?format=csv`}
                  className="ml-auto inline-flex items-center gap-1 text-xs text-primary hover:underline"
                >
                  <Download className="size-3" />导出 CSV
                </a>
              </div>
            </div>
          )}

          {/* 积分榜 */}
          <div>
            <div className="flex items-center gap-2">
              <ListOrdered className="size-4 text-muted-foreground" />
              <h3 className="text-sm font-semibold text-foreground">积分榜</h3>
            </div>
            <Card className="mt-2">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="w-10">#</TableHead>
                    <TableHead>Bot</TableHead>
                    <TableHead>积分</TableHead>
                    <TableHead className="hidden sm:table-cell">W/D/L</TableHead>
                    <TableHead className="hidden md:table-cell">净筹码</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {standings.length === 0 ? (
                    <TableRow><TableCell colSpan={5}><EmptyState text="暂无积分数据" icon={<Trophy className="size-7 opacity-40" />} /></TableCell></TableRow>
                  ) : (
                    standings.map((s, i) => (
                      <TableRow key={s.bot_id}>
                        <TableCell className="font-mono text-xs text-muted-foreground">{i + 1}</TableCell>
                        <TableCell>
                          <Link to={`/bot/${s.bot_id}`} className="font-medium text-foreground hover:text-primary">
                            {s.bot_name || `#${s.bot_id}`}
                          </Link>
                        </TableCell>
                        <TableCell className="font-mono font-semibold text-primary">{s.points}</TableCell>
                        <TableCell className="hidden font-mono text-xs text-muted-foreground sm:table-cell">
                          <span className="text-success">{s.wins}</span>/{s.draws}/<span className="text-destructive">{s.losses}</span>
                        </TableCell>
                        <TableCell className="hidden font-mono text-xs text-muted-foreground md:table-cell">{s.net_chips}</TableCell>
                      </TableRow>
                    ))
                  )}
                </TableBody>
              </Table>
            </Card>
          </div>
        </div>{/* /右边栏 */}
      </div>{/* /双栏 */}
    </PageStub>
  )
}

/** swiss/循环/分组的对阵展示：按 round_num（或 group_id）折叠分组，大规模默认收起。 */
function PairingFoldedList({ pairings }: { pairings: Pairing[] }) {
  // 按 group_id 优先分组（分组赛），否则按 round_num（swiss/循环）
  const groups = useMemo(() => {
    const hasGroup = pairings.some((p) => p.group_id)
    const keyFn = hasGroup
      ? (p: Pairing) => p.group_id || '—'
      : (p: Pairing) => `第 ${(p.round_num ?? 1)} 轮`
    const map = new Map<string, Pairing[]>()
    for (const p of pairings) {
      const k = keyFn(p)
      if (!map.has(k)) map.set(k, [])
      map.get(k)!.push(p)
    }
    return Array.from(map.entries())
  }, [pairings])

  // 大规模（>6 组或任一组 >12 场）默认全部收起
  const big = groups.length > 6 || pairings.length > 60
  const [open, setOpen] = useState<Record<string, boolean>>(() =>
    Object.fromEntries(groups.map(([k]) => [k, !big])),
  )

  return (
    <div className="mt-2 space-y-2">
      {groups.map(([k, ps]) => (
        <Card key={k}>
          <button
            type="button"
            onClick={() => setOpen((o) => ({ ...o, [k]: !o[k] }))}
            className="flex w-full items-center gap-2 px-4 py-2 text-left text-sm font-medium text-foreground hover:bg-accent"
          >
            {open[k] ? <ChevronDown className="size-4" /> : <ChevronRight className="size-4" />}
            <span>{k}</span>
            <Badge variant="secondary" className="text-[10px]">{ps.length} 场</Badge>
            <span className="ml-auto text-xs text-muted-foreground">
              {ps.filter((p) => p.status === 'completed').length} 已完成
            </span>
          </button>
          {open[k] && (
            <div className="space-y-1.5 border-t border-border px-2 py-2">
              {ps.map((p) => {
                const w = p.match_winner
                return (
                  <div key={p.id} className="flex flex-wrap items-center gap-2 rounded px-2 py-1.5 text-sm">
                    <Link to={`/bot/${p.bot_a_id}`} className={`hover:text-primary ${w === 0 ? 'font-semibold text-success' : w === 1 ? 'text-muted-foreground' : 'text-foreground'}`}>
                      {p.bot_a_display || p.bot_a_name || `#${p.bot_a_id}`}
                    </Link>
                    <span className="text-muted-foreground">vs</span>
                    <Link to={`/bot/${p.bot_b_id}`} className={`hover:text-primary ${w === 1 ? 'font-semibold text-success' : w === 0 ? 'text-muted-foreground' : 'text-foreground'}`}>
                      {p.bot_b_display || p.bot_b_name || `#${p.bot_b_id}`}
                    </Link>
                    <StatusBadge status={p.status || 'pending'} />
                    {p.match_id && (
                      <Button asChild variant="ghost" size="sm" className="ml-auto gap-1 text-primary">
                        <Link to={`/match/${p.match_id}`}>查看</Link>
                      </Button>
                    )}
                  </div>
                )
              })}
            </div>
          )}
        </Card>
      ))}
    </div>
  )
}
