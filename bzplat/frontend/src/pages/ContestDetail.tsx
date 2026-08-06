import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { Trophy, Users, Swords, ListOrdered, Play, DoorOpen, RefreshCw, Timer, ChevronDown, ChevronRight, Plus, Download } from 'lucide-react'
import PageStub from '@/components/PageStub'
import { Card, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs'
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
import ScheduleTable from '@/components/contest/ScheduleTable'
import Countdown from '@/components/Countdown'
import { useAuth } from '@/components/useAuth'
import Pagination from '@/components/Pagination'
import { apiGet, apiJson, errMsg } from '@/api'
import { gameLabel, isBoardGame } from '@/lib/games'
import { fmtTime } from '@/lib/format'
import { toast } from 'sonner'

const STAGE_TYPE_LABEL: Record<string, string> = {
  swiss: '瑞士轮',
  round_robin: '单循环',
  double_round_robin: '双循环',
  group_round_robin: '分组单循环',
  group_double_round_robin: '分组双循环',
  single_elimination: '单败淘汰',
}
const SCORING_LABEL: Record<string, string> = {
  poker_3_1_0: '计分 3/1/0',
  ccgc_2_1_0: '计分 2/1/0',
}

interface Contest {
  id: number
  title: string
  description?: string
  status: string
  organizer_id: number
  hands_per_match?: number
  template_id?: string
  /** 可选：后端/模板表解析出的可读名 */
  template_name?: string
  game_id?: string
  stages_json?: string
  current_stage_idx?: number
  rest_ends_at?: string | null
  require_real_name?: number
  // 时间编排
  registration_opens_at?: string | null
  registration_closes_at?: string | null
  starts_at?: string | null
  ends_at?: string | null
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
  // 实名信息（仅组织者可见——后端对非组织者脱敏剔除）
  real_name?: string
  phone?: string
  school?: string
  student_id?: string
}
interface Pairing {
  id: number
  round_num?: number
  bracket_slot?: number | null
  bot_a_id: number
  bot_b_id: number | null
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
  scheduled_at?: string | null
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

/** 状态相关的时间编排提示（报名窗口/开赛/rest 倒计时） */
function ContestScheduleInfo({ c }: { c: Contest }) {
  const now = Date.now()
  const items: { label: string; time?: string | null; countdown?: string }[] = []
  if (c.registration_opens_at) items.push({ label: '开放报名', time: c.registration_opens_at })
  if (c.registration_closes_at) items.push({ label: '报名截止', time: c.registration_closes_at })
  if (c.starts_at) items.push({ label: '比赛开始', time: c.starts_at })
  if (c.ends_at) items.push({ label: '比赛结束', time: c.ends_at })
  if (items.length === 0) return null
  return (
    <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
      {items.map((it) => {
        const future = it.time && new Date(it.time).getTime() > now
        return (
          <span key={it.label} className="flex items-center gap-1">
            <span className="font-medium text-foreground">{it.label}:</span>
            <span>{it.time ? fmtTime(it.time) : '—'}</span>
            {future && it.time && (
              <Countdown endsAt={it.time} className="text-primary" />
            )}
          </span>
        )
      })}
    </div>
  )
}

export default function ContestDetail() {
  const { id } = useParams()
  const { user, isLoggedIn } = useAuth()
  const [contest, setContest] = useState<Contest | null>(null)
  const [entries, setEntries] = useState<Entry[]>([])
  const [pairings, setPairings] = useState<Pairing[]>([])
  const [standings, setStandings] = useState<Standing[]>([])
  const [myEntry, setMyEntry] = useState<Entry | null>(null)
  const [estimate, setEstimate] = useState<{ estimated_matches?: number; eta_seconds?: number } | null>(null)
  const [bots, setBots] = useState<Array<{ id: number; name: string; display_name?: string }>>([])
  const [botId, setBotId] = useState('')
  const [stageTab, setStageTab] = useState(0)
  // 对阵视图：'tree'（对阵树）/ 'table'（一览表）。淘汰赛默认 tree，其余默认 table，可手动切换。
  const [pairingView, setPairingView] = useState<'tree' | 'table'>('tree')
  const [error, setError] = useState('')
  // 报名列表分页（115 人赛事场景：服务端分页，避免一次性渲染全部）
  const [entriesPage, setEntriesPage] = useState(1)
  const [entriesTotal, setEntriesTotal] = useState(0)
  const entriesPerPage = 20
  // 内容区 Tab：对阵 / 选手 / 排行（只渲染当前 tab，避免大量报名/积分全量铺开导致长空白）
  const [contentTab, setContentTab] = useState<'matchups' | 'entries' | 'standings'>('matchups')
  // 积分榜客户端分页（量级通常 < 200，客户端 slice 足够；每页 30 行）
  const [standingsPage, setStandingsPage] = useState(1)
  const standingsPerPage = 30

  const stages = useMemo(() => parseStages(contest), [contest])

  const load = useCallback(() => {
    if (!id) return
    apiGet<{
      contest: Contest
      entries: Entry[]
      pairings: Pairing[]
      standings: Standing[]
      estimate?: { estimated_matches?: number; eta_seconds?: number }
      entries_page?: number
      entries_per_page?: number
      entries_total?: number
      my_entry?: Entry | null
    }>(`/api/contests/${id}?entries_page=${entriesPage}&entries_per_page=${entriesPerPage}`)
      .then((d) => {
        setContest(d.contest)
        setEntries(d.entries || [])
        setPairings(d.pairings || [])
        setStandings(d.standings || [])
        setEstimate(d.estimate || null)
        setStageTab(d.contest.current_stage_idx ?? 0)
        setEntriesTotal(d.entries_total ?? d.entries.length)
        setMyEntry(d.my_entry ?? null)
      })
      .catch((e) => setError(errMsg(e)))
  }, [id, entriesPage, entriesPerPage])

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
  // myEntry 来自后端 my_entry 字段（不分页，休息换 Bot UI 依赖；entries 分页后前端 find 不可靠）
  // 实名校验：赛事要求实名且当前用户未填完整 → 提示去设置页补填
  const needsRealName = !!contest?.require_real_name && !!user && !(
    user.real_name && user.phone && user.school && user.student_id
  )

  const act = async (path: string, body?: unknown, okMsg?: string) => {
    setError('')
    try {
      await apiJson(path, 'POST', body)
      await load()
      if (okMsg) toast.success(okMsg)
    } catch (e) {
      setError(errMsg(e))
    }
  }

  const stagePairings = pairings.filter((p) => (p.stage_idx ?? 0) === stageTab)
  const curStageType = stages[stageTab]?.type as string | undefined
  const isElimStage = curStageType === 'single_elimination' || curStageType === 'double_elimination'
  // 阶段切换时，按赛制重置对阵视图默认值（淘汰→对阵树；swiss/循环→一览表），用户仍可手动切换
  useEffect(() => {
    setPairingView(isElimStage ? 'tree' : 'table')
  }, [stageTab, isElimStage])

  // 每阶段的对阵进度（已完成 / 总数）+ 已进行到的最大轮次，供 stage tab 进度条与「第 N 轮」展示
  const stageProgress = useMemo(() => {
    const map = new Map<number, { completed: number; total: number; maxRound: number }>()
    for (const p of pairings) {
      const idx = p.stage_idx ?? 0
      const cur = map.get(idx) ?? { completed: 0, total: 0, maxRound: 0 }
      cur.total += 1
      if (p.status === 'completed') cur.completed += 1
      const r = p.round_num ?? 1
      if (r > cur.maxRound) cur.maxRound = r
      map.set(idx, cur)
    }
    return map
  }, [pairings])

  // 积分榜客户端分页：当前页 slice + 越界回退（如 standings 缩短到当前页之外）
  const standingsTotal = standings.length
  const standingsTotalPages = Math.max(1, Math.ceil(standingsTotal / standingsPerPage))
  const safeStandingsPage = Math.min(standingsPage, standingsTotalPages)
  const standingsPageItems = standings.slice(
    (safeStandingsPage - 1) * standingsPerPage,
    safeStandingsPage * standingsPerPage,
  )
  // 行号需要按全量排序位置计算（而非当前页内序），保证翻页后名次连续
  const standingsPageBase = (safeStandingsPage - 1) * standingsPerPage

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
            <div className="min-w-0 flex-1 overflow-hidden">
              <h2 className="break-words text-lg font-semibold text-foreground [overflow-wrap:anywhere]">
                {contest.title}
              </h2>
              <p className="mt-1 max-h-28 overflow-y-auto whitespace-pre-wrap break-words text-sm text-muted-foreground [overflow-wrap:anywhere]">
                {contest.description || '无说明'}
              </p>
              <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                <StatusBadge status={contest.status} />
                <span className="max-w-full truncate" title={contest.template_id || undefined}>
                  模板 {contest.template_name || contest.template_id || '—'}
                </span>
                <span>· 游戏 {gameLabel(contest.game_id || 'holdem')}</span>
                {isBoardGame(contest.game_id) ? (
                  <span>· 单局决胜</span>
                ) : (
                  <span>· 每场 70 手</span>
                )}
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
              阶段休息中，倒计时 <Countdown endsAt={contest.rest_ends_at} className="text-warning" />（可更换派遣 Bot）
            </div>
          )}
          {/* 时间编排：报名窗口 / 开赛 / 比赛时间 */}
          <ContestScheduleInfo c={contest} />
        </CardContent>
      </Card>

      {error && <ErrorMsg msg={error} className="mt-3" />}

      {/* 操作区 */}
      <div className="mt-4 flex flex-wrap gap-2">
        {isOrg && contest.status === 'draft' && (
          <Button onClick={() => void act(`/api/contests/${id}/open`, undefined, '已开放报名')} className="gap-1.5">
            <DoorOpen className="size-4" />开放报名
          </Button>
        )}
        {isOrg && (contest.status === 'open' || contest.status === 'draft') && (
          <Button variant="outline" onClick={() => void act(`/api/contests/${id}/publish`, undefined, '排期已发布')} className="gap-1.5">
            <ListOrdered className="size-4" />截止报名·出排期
          </Button>
        )}
        {isOrg && (contest.status === 'open' || contest.status === 'draft' || contest.status === 'published') && (
          <Button variant="outline" onClick={() => void act(`/api/contests/${id}/start`, undefined, '比赛已开始')} className="gap-1.5">
            <Play className="size-4" />立即开赛
          </Button>
        )}
        {isOrg && contest.status === 'rest' && (
          <Button onClick={() => void act(`/api/contests/${id}/resume`, undefined, '已进入下一阶段')}>结束休息 / 下一阶段</Button>
        )}
        {isLoggedIn && contest.status === 'open' && (
          <div className="flex flex-wrap items-center gap-2">
            {needsRealName && (
              <span className="text-sm text-warning">
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
            <Button variant="outline" disabled={!botId || needsRealName} onClick={() => void act(`/api/contests/${id}/register`, { bot_id: Number(botId) }, '报名成功')}>
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
            <Button variant="outline" disabled={!botId} onClick={() => void act(`/api/contests/${id}/dispatch`, { bot_id: Number(botId) }, '派遣 Bot 已更换')}>
              确认更换
            </Button>
          </div>
        )}
      </div>

      {/* 内容区 Tabs：对阵 / 选手 / 排行（只渲染当前 tab，避免大量报名/积分全量铺开导致长空白） */}
      <Tabs value={contentTab} onValueChange={(v) => setContentTab(v as typeof contentTab)} className="mt-4">
        <TabsList>
          <TabsTrigger value="matchups" className="gap-1.5">
            <Swords className="size-4" />对阵
          </TabsTrigger>
          <TabsTrigger value="entries" className="gap-1.5">
            <Users className="size-4" />选手
            {entriesTotal > 0 && (
              <span className="text-xs text-muted-foreground">{entriesTotal}</span>
            )}
          </TabsTrigger>
          <TabsTrigger value="standings" className="gap-1.5">
            <ListOrdered className="size-4" />排行
            {standings.length > 0 && (
              <span className="text-xs text-muted-foreground">{standings.length}</span>
            )}
          </TabsTrigger>
        </TabsList>

        {/* Tab「对阵」：阶段切换(S4) + 阶段配置 + 对阵视图(S6a) + 正式名次(finished) */}
        <TabsContent value="matchups" className="mt-4 space-y-4">
          {/* 阶段 Tabs（中文赛制名 + 当前进度，隐藏 raw type 字符串） */}
          {stages.length > 0 && (
            <Tabs value={String(stageTab)} onValueChange={(v) => setStageTab(Number(v))}>
              <TabsList>
                {stages.map((s, i) => {
                  const prog = stageProgress.get(i)
                  const typeLabel = STAGE_TYPE_LABEL[s.type || ''] || s.type
                  // 「瑞士轮 · 第3轮」式：有对阵且未全部完成时显示当前轮次
                  const roundTag = prog && prog.maxRound > 0 && prog.completed < prog.total
                    ? `第${prog.maxRound}轮`
                    : null
                  return (
                    <TabsTrigger key={s.key || i} value={String(i)} className="gap-1.5">
                      <span>{typeLabel || s.key || `阶段${i + 1}`}</span>
                      {roundTag && <span className="text-xs text-muted-foreground">· {roundTag}</span>}
                      {contest.current_stage_idx === i && contest.status !== 'finished' && (
                        <Badge variant="outline" className="ml-1 text-[9px] text-primary">当前</Badge>
                      )}
                      {prog && prog.total > 0 && (
                        <span className="text-[10px] text-muted-foreground">
                          {prog.completed}/{prog.total}
                        </span>
                      )}
                    </TabsTrigger>
                  )
                })}
              </TabsList>
            </Tabs>
          )}

          {/* 阶段配置（中文可读化，避免 raw type/scoring 字符串） */}
          {stages[stageTab] && (
            <div className="break-words rounded-lg bg-muted/50 px-3 py-2 text-xs text-muted-foreground">
              <span className="font-medium text-foreground">本阶段配置：</span>
              {[
                STAGE_TYPE_LABEL[stages[stageTab].type || ''] || stages[stageTab].type,
                SCORING_LABEL[stages[stageTab].scoring || ''] || stages[stageTab].scoring,
                stages[stageTab].group_count ? `分组 ${stages[stageTab].group_count}` : null,
                stages[stageTab].rounds !== undefined ? `轮数 ${stages[stageTab].rounds}` : null,
                stages[stageTab].advance_count ? `晋级 ${stages[stageTab].advance_count}` : null,
                stages[stageTab].advance_per_group ? `每组晋级 ${stages[stageTab].advance_per_group}` : null,
                stages[stageTab].rest_after_minutes ? `休息 ${stages[stageTab].rest_after_minutes} 分` : null,
                stages[stageTab].allow_bot_swap_in_rest ? '休息可换 Bot' : null,
              ].filter(Boolean).join(' · ')}
            </div>
          )}

          {/* 对阵区（BracketTree/PairedFoldedList 能吃满宽） */}
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <h3 className="text-sm font-semibold text-foreground">
                对阵{stages.length ? ` · ${stages[stageTab]?.key || `阶段${stageTab + 1}`}` : ''}
              </h3>
              {/* 对阵视图切换：对阵树 / 一览表（淘汰赛默认树，其余默认表，可手动切换） */}
              {stagePairings.length > 0 && (
                <div className="ml-auto flex items-center gap-1 rounded-lg bg-muted p-0.5 text-xs">
                  <button
                    type="button"
                    onClick={() => setPairingView('tree')}
                    className={`rounded-md px-2 py-1 transition-colors ${
                      pairingView === 'tree' ? 'bg-background font-medium text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'
                    }`}
                  >
                    对阵树
                  </button>
                  <button
                    type="button"
                    onClick={() => setPairingView('table')}
                    className={`rounded-md px-2 py-1 transition-colors ${
                      pairingView === 'table' ? 'bg-background font-medium text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'
                    }`}
                  >
                    一览表
                  </button>
                </div>
              )}
            </div>
            {stagePairings.length === 0 ? (
              <Card className="mt-2"><EmptyState text="暂无对阵" icon={<Swords className="size-7 opacity-40" />} /></Card>
            ) : pairingView === 'table' ? (
              // 一览表：所有对阵的扁平表格（轮次/Bot/排期时间/状态/查看）
              <div className="mt-2">
                <ScheduleTable pairings={stagePairings} />
              </div>
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

          {/* P5：全员正式名次（赛事 finished 时显示在阵区下方 + 下载） */}
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
        </TabsContent>

        {/* Tab「选手」：报名列表（含组织者批量指派/导出/实名显示/移除） */}
        <TabsContent value="entries" className="mt-4">
          <div>
            <div className="flex items-center gap-2">
              <h3 className="text-sm font-semibold text-foreground">报名（{entriesTotal}）</h3>
              {isOrg && (contest.status === 'draft' || contest.status === 'open') && (
                <Button
                  size="sm"
                  variant="outline"
                  className="ml-auto h-7 gap-1 text-xs"
                  onClick={() => void act(`/api/contests/${id}/entries/bulk`, { assign_all: true, game_id: contest.game_id }, '批量指派完成')}
                >
                  <Plus className="size-3" />批量指派
                </Button>
              )}
              {/* 组织者导出：报名名单（含实名）+ 结果排名合并 CSV */}
              {isOrg && (
                <a
                  href={`/api/contests/${id}/export?format=csv`}
                  className="inline-flex items-center gap-1 text-xs text-primary hover:underline"
                  title="导出报名名单（含实名信息）与成绩排名 CSV"
                >
                  <Download className="size-3" />导出名单
                </a>
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
                        <Link to={`/bot/${e.bot_id}`} className="max-w-[12rem] truncate font-medium text-foreground hover:text-primary" title={e.bot_display || e.bot_name || `#${e.bot_id}`}>
                          {e.bot_display || e.bot_name || `#${e.bot_id}`}
                        </Link>
                        {e.owner_name && (
                          <Link to={`/user/${encodeURIComponent(e.owner_name)}`} className="text-xs text-muted-foreground hover:text-primary">
                            @{e.owner_display || e.owner_name}
                          </Link>
                        )}
                        {/* 组织者可见实名信息（姓名/手机/学校/学号）——非组织者后端已脱敏剔除 */}
                        {isOrg && e.real_name && (
                          <span className="text-xs text-muted-foreground">
                            · {e.real_name}{e.phone ? ` / ${e.phone}` : ''}{e.school ? ` / ${e.school}` : ''}{e.student_id ? ` / ${e.student_id}` : ''}
                          </span>
                        )}
                        {e.seed ? <span className="text-xs text-muted-foreground">种子 {e.seed}</span> : ''}
                        {e.group_id && <Badge variant="secondary" className="max-w-[8rem] truncate text-[10px]">{e.group_id}</Badge>}
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
                <Pagination
                  page={entriesPage}
                  perPage={entriesPerPage}
                  total={entriesTotal}
                  onPageChange={setEntriesPage}
                />
              </CardContent>
            </Card>
          </div>
        </TabsContent>

        {/* Tab「排行」：积分榜（客户端分页，per_page=30） */}
        <TabsContent value="standings" className="mt-4">
          <div>
            <div className="flex items-center gap-2">
              <h3 className="text-sm font-semibold text-foreground">积分榜</h3>
            </div>
            <Card className="mt-2 overflow-hidden">
              <div className="overflow-x-auto">
                <Table className="min-w-[28rem]">
                  <TableHeader>
                    <TableRow>
                      <TableHead className="w-10">#</TableHead>
                      <TableHead className="min-w-[6rem]">Bot</TableHead>
                      <TableHead>积分</TableHead>
                      <TableHead className="hidden sm:table-cell">W/D/L</TableHead>
                      <TableHead className="hidden md:table-cell">净筹码</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {standings.length === 0 ? (
                      <TableRow><TableCell colSpan={5}><EmptyState text="暂无积分数据" icon={<Trophy className="size-7 opacity-40" />} /></TableCell></TableRow>
                    ) : (
                      standingsPageItems.map((s, i) => (
                        <TableRow key={s.bot_id}>
                          <TableCell className="font-mono text-xs text-muted-foreground">{standingsPageBase + i + 1}</TableCell>
                          <TableCell className="max-w-[10rem]">
                            <Link to={`/bot/${s.bot_id}`} className="block truncate font-medium text-foreground hover:text-primary" title={s.bot_name || `#${s.bot_id}`}>
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
              </div>
            </Card>
            {/* 积分榜客户端分页：standings 量级通常 < 200，slice 分页够用 */}
            <Pagination
              page={safeStandingsPage}
              perPage={standingsPerPage}
              total={standingsTotal}
              onPageChange={setStandingsPage}
            />
          </div>
        </TabsContent>
      </Tabs>
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
                    {p.bot_b_id == null ? (
                      // 轮空（bye placeholder）：无对手，渲染为 muted 非链接文本而非 /bot/null
                      <span className="italic text-muted-foreground">轮空 (bye)</span>
                    ) : (
                      <Link to={`/bot/${p.bot_b_id}`} className={`hover:text-primary ${w === 1 ? 'font-semibold text-success' : w === 0 ? 'text-muted-foreground' : 'text-foreground'}`}>
                        {p.bot_b_display || p.bot_b_name || `#${p.bot_b_id}`}
                      </Link>
                    )}
                    <StatusBadge status={p.status || 'pending'} />
                    {p.scheduled_at && (
                      <span className="text-xs text-muted-foreground">{fmtTime(p.scheduled_at)}</span>
                    )}
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
