import { useCallback, useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { User, Bot as BotIcon, Plus, Play, X as XIcon } from 'lucide-react'
import PageStub from '@/components/PageStub'
import OpponentPickerModal, { type PickBot } from '@/components/OpponentPickerModal'
import { useAuth } from '@/components/useAuth'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Label } from '@/components/ui/label'
import { Badge } from '@/components/ui/badge'
import { ErrorMsg } from '@/components/ui/status'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { cn } from '@/lib/utils'
import { apiGet, apiJson, errMsg } from '@/api'
import { GAMES, type GameId } from '@/lib/games'

/** 版本列表条目（公开视图：id+version+upload_note+created_at+size_bytes；owner 视图字段更多）。 */
interface VersionRow {
  id: number
  version: number
  upload_note?: string
  created_at?: string
  uploaded_at?: string
  size_bytes?: number
}

/** 一个座位的选中状态：bot + 选定版本 id（undefined=当前/激活版本）。 */
interface SeatState {
  bot: PickBot | null
  /** 选定版本的 bot_versions.id；undefined/null = 用当前激活版本。 */
  versionId: number | undefined
}

const EMPTY_SEAT: SeatState = { bot: null, versionId: undefined }

/**
 * 合并后的挑战页：单一人/机对局，无模式切换。
 *
 * 两座位（显示从 1 起计；后端仍 0 起计）：
 * - 座位 1（先手 / 黑）：固定 Bot。
 * - 座位 2（后手 / 白）：Bot 或「我亲自上场」（人类固定坐此位）。
 * 提交按座位 2 类型走 /api/matches/challenge（bot vs bot）或
 * /api/matches/human（human_seat=1 固定，对应 0 起计后端座 1=后手/白）。
 */
export default function Challenge() {
  const { isLoggedIn, user } = useAuth()
  const nav = useNavigate()
  const [gameId, setGameId] = useState<GameId>('holdem')
  // 两座位（内部仍 0 起计以对齐后端；显示 +1）。
  const [seats, setSeats] = useState<[SeatState, SeatState]>([
    { ...EMPTY_SEAT },
    { ...EMPTY_SEAT },
  ])
  // 座位 2 的类型：'bot' 或 'human'（人类固定座位 2 = 后手/白）。
  const [seat2Kind, setSeat2Kind] = useState<'bot' | 'human'>('bot')
  // 弹窗：pickingSeat 标记当前为哪个座位挑 bot（'s1'|'s2'）。
  const [pickingSeat, setPickingSeat] = useState<'s1' | 's2' | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  // 对局级配置（游戏无关，当前规则参数已钉死固定值，恒空对象）。
  const matchCfg: Record<string, number> = {}

  const resetSeatsOnGameChange = useCallback(() => {
    setSeats([{ ...EMPTY_SEAT }, { ...EMPTY_SEAT }])
  }, [])

  // 版本选择器所需：缓存每个 bot id 的版本列表（弹窗选定 bot 后按需拉取）。
  // key = bot id；value = { rows, current, loading }
  const [versionCache, setVersionCache] = useState<
    Record<number, { rows: VersionRow[]; current: number | undefined; loading: boolean }>
  >({})

  const loadVersions = useCallback(async (botId: number) => {
    setVersionCache((c) =>
      c[botId] ? { ...c, [botId]: { ...c[botId], loading: true } } : { ...c, [botId]: { rows: [], current: undefined, loading: true } },
    )
    try {
      const d = await apiGet<{ versions: VersionRow[]; current_version: number }>(
        `/api/bots/${botId}/versions`,
      )
      setVersionCache((c) => ({
        ...c,
        [botId]: { rows: d.versions || [], current: d.current_version, loading: false },
      }))
    } catch {
      setVersionCache((c) => ({
        ...c,
        [botId]: { rows: [], current: undefined, loading: false },
      }))
    }
  }, [])

  // 选定某座位的 bot（来自弹窗）：写入 bot + 重置版本为「当前」+ 拉版本列表。
  const pickBotFor = (slot: 's1' | 's2', bot: PickBot) => {
    const idx = slot === 's1' ? 0 : 1
    setSeats((s) => {
      const next: [SeatState, SeatState] = [s[0], s[1]]
      next[idx] = { bot, versionId: undefined }
      return next
    })
    setPickingSeat(null)
    void loadVersions(bot.id)
  }

  const clearSeat = (slot: 's1' | 's2') => {
    const idx = slot === 's1' ? 0 : 1
    setSeats((s) => {
      const next: [SeatState, SeatState] = [s[0], s[1]]
      next[idx] = { ...EMPTY_SEAT }
      return next
    })
  }

  const setSeatVersion = (slot: 's1' | 's2', vId: number | undefined) => {
    const idx = slot === 's1' ? 0 : 1
    setSeats((s) => {
      const next: [SeatState, SeatState] = [s[0], s[1]]
      next[idx] = { ...next[idx], versionId: vId }
      return next
    })
  }

  // 自博弈：座位 2 = Bot 且两座同 bot id。
  const selfPlay =
    seat2Kind === 'bot' && seats[0].bot && seats[1].bot && seats[0].bot!.id === seats[1].bot!.id

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError('')
    try {
      if (seat2Kind === 'human') {
        // 人类对战：人类固定座位 2（后端 0 起计 = 座 1）。座位 1 = Bot。
        if (!seats[0].bot) throw new Error('请选择座位 1 的 Bot')
        const body: Record<string, unknown> = {
          bot_id: seats[0].bot.id,
          human_seat: 1, // 固定：人类 = 后端座 1 = 后手/白
          match_config: { ...matchCfg },
          game_id: gameId,
        }
        // 注：HumanChallengeBody 不接受 bot_version_id，故座位 1 选版本时人类对战忽略版本。
        const d = await apiJson<{ match_id: string }>('/api/matches/human', 'POST', body)
        nav(`/play/${d.match_id}`)
        return
      }
      // bot vs bot
      if (!seats[0].bot || !seats[1].bot) throw new Error('请为两个座位各选择一个 Bot')
      const body: Record<string, unknown> = {
        my_bot_id: seats[0].bot.id,
        opponent_bot_id: seats[1].bot.id,
        match_config: { ...matchCfg },
        game_id: gameId,
      }
      if (seats[0].versionId !== undefined) body.my_bot_version_id = seats[0].versionId
      if (seats[1].versionId !== undefined) body.opponent_bot_version_id = seats[1].versionId
      const d = await apiJson<{ match_id: string }>('/api/matches/challenge', 'POST', body)
      nav(`/match/${d.match_id}`)
    } catch (err) {
      setError(errMsg(err, '发起挑战失败'))
    } finally {
      setBusy(false)
    }
  }

  if (!isLoggedIn) {
    return (
      <PageStub title="发起挑战" subtitle="选择游戏与座位 Bot（支持自博弈、人类对战、指定历史版本）">
        <Card className="mx-auto max-w-lg">
          <CardContent>
            <p className="text-sm text-muted-foreground">
              请先{' '}
              <Link to="/login" className="font-medium text-primary hover:underline">
                登录
              </Link>{' '}
              后选择双方 Bot 发起挑战。
            </p>
          </CardContent>
        </Card>
      </PageStub>
    )
  }

  // bot 座位渲染（座位 1 与座位 2-bot 共用）。slot='s1'|'s2'；座位号显示 +1。
  const renderBotSeat = (slot: 's1' | 's2') => {
    const idx = slot === 's1' ? 0 : 1
    const seat = seats[idx]
    const seatLabel = slot === 's1' ? '座位 1（先手 / 黑）' : '座位 2（后手 / 白）'
    const vc = seat.bot ? versionCache[seat.bot.id] : undefined
    return (
      <div className="space-y-2">
        <div className="flex items-center justify-between">
          <Label>{seatLabel}</Label>
          {seat.bot && (
            <button
              type="button"
              onClick={() => clearSeat(slot)}
              className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-destructive"
            >
              <XIcon className="size-3" /> 清除
            </button>
          )}
        </div>
        <button
          type="button"
          onClick={() => setPickingSeat(slot)}
          className="flex w-full items-center gap-2 rounded-lg border border-dashed border-input px-3 py-2.5 text-sm text-muted-foreground hover:bg-accent"
        >
          {seat.bot ? (
            <span className="flex flex-wrap items-center gap-2 text-foreground">
              <BotIcon className="size-4 text-primary" />
              <strong>{seat.bot.display_name || seat.bot.name}</strong>
              <span className="text-xs text-muted-foreground">
                {seat.bot.owner_display || seat.bot.owner_name || `#${seat.bot.owner_id}`}
                {seat.bot.owner_id === user?.id ? '（我的）' : ''}
              </span>
            </span>
          ) : (
            <>
              <Plus className="size-4" />
              选择 Bot（搜索 / 我的 / 按用户）
            </>
          )}
        </button>

        {/* 版本选择：bot 选定后展示。空串哨兵 = 当前/激活版本。 */}
        {seat.bot && (
          <Select
            value={seat.versionId === undefined ? '' : String(seat.versionId)}
            onValueChange={(v) => setSeatVersion(slot, v === '' ? undefined : Number(v))}
          >
            <SelectTrigger className="h-9 w-full">
              <SelectValue placeholder="选择版本" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="">
                {vc?.current !== undefined ? `当前版本 (v${vc.current})` : '当前版本'}
              </SelectItem>
              {(vc?.rows || []).map((vr) => {
                const isCurrent = vc?.current !== undefined && vr.version === vc.current
                return (
                  <SelectItem key={vr.id} value={String(vr.id)}>
                    v{vr.version}
                    {vr.upload_note ? ` ${vr.upload_note}` : ''}
                    {isCurrent ? ' · 当前' : ''}
                  </SelectItem>
                )
              })}
            </SelectContent>
          </Select>
        )}
      </div>
    )
  }

  const ready = seat2Kind === 'human' ? !!seats[0].bot : !!seats[0].bot && !!seats[1].bot

  return (
    <PageStub title="发起挑战" subtitle="座位 1 固定 Bot；座位 2 可选 Bot 或亲自上场（人类不计天梯）">
      <form onSubmit={(e) => void onSubmit(e)} className="mx-auto max-w-2xl">
        <Card>
          <CardContent className="space-y-4">
            {/* 游戏筛选：切换时重置两座位（不同游戏的 bot 不互通） */}
            <div className="space-y-1.5">
              <Label>游戏</Label>
              <Select
                value={gameId}
                onValueChange={(v) => {
                  setGameId(v as GameId)
                  resetSeatsOnGameChange()
                }}
              >
                <SelectTrigger className="mt-1.5 h-9 w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {GAMES.map((g) => (
                    <SelectItem key={g.id} value={g.id}>{g.label}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <div className="rounded-lg border border-border p-3">
              {selfPlay && (
                <Badge variant="secondary" className="mb-3 gap-1">
                  <BotIcon className="size-3" />
                  自博弈
                </Badge>
              )}
              <div className="grid gap-3 sm:grid-cols-2">
                {/* 座位 1：固定 Bot */}
                {renderBotSeat('s1')}

                {/* 座位 2：Bot 或 人类（小开关，仅此座位有） */}
                <div className="space-y-2">
                  <div className="flex items-center justify-between">
                    <Label>座位 2（后手 / 白）</Label>
                    <div className="inline-flex rounded-lg border border-input p-0.5 text-xs">
                      <button
                        type="button"
                        onClick={() => setSeat2Kind('bot')}
                        className={cn(
                          'inline-flex items-center gap-1 rounded-md px-2 py-1',
                          seat2Kind === 'bot'
                            ? 'bg-primary/10 text-primary'
                            : 'text-muted-foreground hover:bg-accent',
                        )}
                      >
                        <BotIcon className="size-3" />
                        选 Bot
                      </button>
                      <button
                        type="button"
                        onClick={() => setSeat2Kind('human')}
                        className={cn(
                          'inline-flex items-center gap-1 rounded-md px-2 py-1',
                          seat2Kind === 'human'
                            ? 'bg-primary/10 text-primary'
                            : 'text-muted-foreground hover:bg-accent',
                        )}
                      >
                        <User className="size-3" />
                        我亲自上场
                      </button>
                    </div>
                  </div>

                  {seat2Kind === 'bot' ? (
                    <div className="space-y-2">
                      {seats[1].bot && (
                        <button
                          type="button"
                          onClick={() => clearSeat('s2')}
                          className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-destructive"
                        >
                          <XIcon className="size-3" /> 清除
                        </button>
                      )}
                      <button
                        type="button"
                        onClick={() => setPickingSeat('s2')}
                        className="flex w-full items-center gap-2 rounded-lg border border-dashed border-input px-3 py-2.5 text-sm text-muted-foreground hover:bg-accent"
                      >
                        {seats[1].bot ? (
                          <span className="flex flex-wrap items-center gap-2 text-foreground">
                            <BotIcon className="size-4 text-primary" />
                            <strong>{seats[1].bot.display_name || seats[1].bot.name}</strong>
                            <span className="text-xs text-muted-foreground">
                              {seats[1].bot.owner_display || seats[1].bot.owner_name || `#${seats[1].bot.owner_id}`}
                              {seats[1].bot.owner_id === user?.id ? '（我的）' : ''}
                            </span>
                          </span>
                        ) : (
                          <>
                            <Plus className="size-4" />
                            选择 Bot（搜索 / 我的 / 按用户）
                          </>
                        )}
                      </button>
                      {seats[1].bot && (
                        (() => {
                          const vc = versionCache[seats[1].bot!.id]
                          return (
                            <Select
                              value={seats[1].versionId === undefined ? '' : String(seats[1].versionId)}
                              onValueChange={(v) => setSeatVersion('s2', v === '' ? undefined : Number(v))}
                            >
                              <SelectTrigger className="h-9 w-full">
                                <SelectValue placeholder="选择版本" />
                              </SelectTrigger>
                              <SelectContent>
                                <SelectItem value="">
                                  {vc?.current !== undefined ? `当前版本 (v${vc.current})` : '当前版本'}
                                </SelectItem>
                                {(vc?.rows || []).map((vr) => {
                                  const isCurrent = vc?.current !== undefined && vr.version === vc.current
                                  return (
                                    <SelectItem key={vr.id} value={String(vr.id)}>
                                      v{vr.version}
                                      {vr.upload_note ? ` ${vr.upload_note}` : ''}
                                      {isCurrent ? ' · 当前' : ''}
                                    </SelectItem>
                                  )
                                })}
                              </SelectContent>
                            </Select>
                          )
                        })()
                      )}
                    </div>
                  ) : (
                    <div className="rounded-lg border border-dashed border-input px-3 py-3 text-sm text-muted-foreground">
                      你（<strong className="text-foreground">@{user?.username}</strong>）作为人类玩家，不计天梯。
                    </div>
                  )}
                </div>
              </div>

              <p className="mt-3 text-xs text-muted-foreground">
                {seat2Kind === 'human'
                  ? '座位 1 选 Bot，座位 2 由你亲自上场。人类对战走独立并发、不计天梯。'
                  : '两个座位可选同一个 Bot（自博弈），亦可各自指定历史版本对比。版本缺省=当前激活版本。'}
              </p>
            </div>

            {error && <ErrorMsg msg={error} />}
            <Button
              type="submit"
              disabled={busy || !ready}
              className="w-full gap-1.5"
            >
              <Play className="size-4" />
              {busy ? '发起中…' : seat2Kind === 'human' ? '开始人类对战' : '开始对局'}
            </Button>
            {!busy && !ready && (
              <p className="text-center text-xs text-muted-foreground">
                {seat2Kind === 'human'
                  ? '请选择座位 1 的 Bot'
                  : '请为两个座位各选择一个 Bot'}
              </p>
            )}
          </CardContent>
        </Card>
      </form>

      {pickingSeat !== null && (
        <OpponentPickerModal
          gameId={gameId}
          myUserId={user?.id}
          onClose={() => setPickingSeat(null)}
          onPick={(b) => pickBotFor(pickingSeat, b)}
        />
      )}
    </PageStub>
  )
}
