import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { Swords, User, Bot as BotIcon, Plus, Play, X as XIcon } from 'lucide-react'
import PageStub from '@/components/PageStub'
import OpponentPickerModal, { type PickBot } from '@/components/OpponentPickerModal'
import { useAuth } from '@/components/useAuth'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Badge } from '@/components/ui/badge'
import { ErrorMsg } from '@/components/ui/status'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { cn } from '@/lib/utils'
import { apiGet, apiJson, errMsg } from '@/api'
import { GAMES, gameLabel, type GameId } from '@/lib/games'
import { getGame, defaultMatchConfig } from '@/games'

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

export default function Challenge() {
  const { isLoggedIn, user } = useAuth()
  const nav = useNavigate()
  const [gameId, setGameId] = useState<GameId>('holdem')
  // 人类模式（替代双座）。true=人类亲自上场，对单 bot。
  const [humanMode, setHumanMode] = useState(false)
  // 双座：seat 0（先手/黑）与 seat 1（后手/白）。
  const [seats, setSeats] = useState<[SeatState, SeatState]>([
    { ...EMPTY_SEAT },
    { ...EMPTY_SEAT },
  ])
  // 人类模式：选中的 bot + 人类座位。
  const [humanBot, setHumanBot] = useState<PickBot | null>(null)
  const [humanSeat, setHumanSeat] = useState(1)
  // 弹窗：pickingSeat 标记当前为哪个座位挑 bot（'s0'|'s1'|'human'）。
  const [pickingSeat, setPickingSeat] = useState<'s0' | 's1' | 'human' | null>(null)
  // 动态对局参数（按所选游戏的 configFields 驱动，消除散落 hands 状态 + 漏 pencil n_dots）
  const [matchCfg, setMatchCfg] = useState<Record<string, number>>(defaultMatchConfig('holdem'))
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const gameSpec = getGame(gameId)

  // 切换游戏时重置参数 + 两座位 + 人类 bot（不同游戏的 bot 不互通）。
  useEffect(() => {
    setMatchCfg(defaultMatchConfig(gameId))
  }, [gameId])

  const resetSeatsOnGameChange = useCallback(() => {
    setSeats([{ ...EMPTY_SEAT }, { ...EMPTY_SEAT }])
    setHumanBot(null)
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
  const pickBotFor = (slot: 's0' | 's1' | 'human', bot: PickBot) => {
    if (slot === 'human') {
      setHumanBot(bot)
    } else {
      const idx = slot === 's0' ? 0 : 1
      setSeats((s) => {
        const next: [SeatState, SeatState] = [s[0], s[1]]
        next[idx] = { bot, versionId: undefined }
        return next
      })
    }
    setPickingSeat(null)
    void loadVersions(bot.id)
  }

  const clearSeat = (slot: 's0' | 's1' | 'human') => {
    if (slot === 'human') {
      setHumanBot(null)
    } else {
      const idx = slot === 's0' ? 0 : 1
      setSeats((s) => {
        const next: [SeatState, SeatState] = [s[0], s[1]]
        next[idx] = { ...EMPTY_SEAT }
        return next
      })
    }
  }

  const setSeatVersion = (slot: 's0' | 's1', vId: number | undefined) => {
    const idx = slot === 's0' ? 0 : 1
    setSeats((s) => {
      const next: [SeatState, SeatState] = [s[0], s[1]]
      next[idx] = { ...next[idx], versionId: vId }
      return next
    })
  }

  const selfPlay =
    !humanMode && seats[0].bot && seats[1].bot && seats[0].bot!.id === seats[1].bot!.id

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError('')
    try {
      if (humanMode) {
        if (!humanBot) throw new Error('请选择对手 Bot')
        const body: Record<string, unknown> = {
          bot_id: humanBot.id,
          human_seat: humanSeat,
          game_id: gameId,
          match_config: { ...matchCfg },
        }
        const d = await apiJson<{ match_id: string }>('/api/matches/human', 'POST', body)
        nav(`/play/${d.match_id}`)
        return
      }
      // 双座 bot vs bot
      if (!seats[0].bot || !seats[1].bot) throw new Error('请为两个座位各选择一个 Bot')
      const body: Record<string, unknown> = {
        my_bot_id: seats[0].bot.id,
        opponent_bot_id: seats[1].bot.id,
        game_id: gameId,
        match_config: { ...matchCfg },
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
      <PageStub title="发起挑战" subtitle="选择游戏与两个座位的 Bot（支持自博弈、指定历史版本）">
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

  // 座位渲染（双座模式共用）。slot='s0'|'s1'。
  const renderSeat = (slot: 's0' | 's1') => {
    const idx = slot === 's0' ? 0 : 1
    const seat = seats[idx]
    const seatLabel = slot === 's0' ? '座位 0（先手 / 黑）' : '座位 1（后手 / 白）'
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

  return (
    <PageStub title="发起挑战" subtitle="选择游戏，为两个座位各选 Bot 与版本（支持自博弈）">
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

            {/* 模式切换 */}
            <div className="rounded-lg border border-border p-3">
              <div className="flex flex-wrap gap-2 text-sm">
                <button
                  type="button"
                  onClick={() => setHumanMode(false)}
                  className={cn(
                    'inline-flex items-center gap-1.5 rounded-lg border px-3 py-1.5',
                    !humanMode
                      ? 'border-primary/40 bg-primary/10 text-primary'
                      : 'border-input text-muted-foreground hover:bg-accent',
                  )}
                >
                  <Swords className="size-3.5" />
                  双座对战
                </button>
                <button
                  type="button"
                  onClick={() => setHumanMode(true)}
                  className={cn(
                    'inline-flex items-center gap-1.5 rounded-lg border px-3 py-1.5',
                    humanMode
                      ? 'border-primary/40 bg-primary/10 text-primary'
                      : 'border-input text-muted-foreground hover:bg-accent',
                  )}
                >
                  <User className="size-3.5" />
                  人类亲自上场
                </button>
              </div>

              {!humanMode ? (
                <div className="mt-3 space-y-3">
                  {selfPlay && (
                    <Badge variant="secondary" className="gap-1">
                      <BotIcon className="size-3" />
                      自博弈
                    </Badge>
                  )}
                  <div className="grid gap-3 sm:grid-cols-2">
                    {renderSeat('s0')}
                    {renderSeat('s1')}
                  </div>
                  <p className="text-xs text-muted-foreground">
                    两个座位可选同一个 Bot（自博弈），亦可各自指定历史版本对比。版本缺省=当前激活版本。
                  </p>
                </div>
              ) : (
                <div className="mt-3 space-y-3 text-sm text-muted-foreground">
                  <p>
                    你（<strong className="text-foreground">{user?.username}</strong>）作为人类玩家，对战下面的 Bot。
                  </p>
                  <div className="space-y-2">
                    <Label>对手 Bot（{gameLabel(gameId)}）</Label>
                    <button
                      type="button"
                      onClick={() => setPickingSeat('human')}
                      className="flex w-full items-center gap-2 rounded-lg border border-dashed border-input px-3 py-2.5 text-sm text-muted-foreground hover:bg-accent"
                    >
                      {humanBot ? (
                        <span className="flex flex-wrap items-center gap-2 text-foreground">
                          <BotIcon className="size-4 text-primary" />
                          <strong>{humanBot.display_name || humanBot.name}</strong>
                          <span className="text-xs text-muted-foreground">
                            {humanBot.owner_display || humanBot.owner_name || `#${humanBot.owner_id}`}
                          </span>
                        </span>
                      ) : (
                        <>
                          <Plus className="size-4" />
                          选择 Bot（搜索 / 我的 / 按用户）
                        </>
                      )}
                    </button>
                  </div>
                  <div className="space-y-1.5">
                    <span className="text-sm text-muted-foreground">你坐哪一位？</span>
                    <Select value={String(humanSeat)} onValueChange={(v) => setHumanSeat(Number(v))}>
                      <SelectTrigger className="mt-1.5 h-9 w-full">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="0">座位 0（先手/黑）</SelectItem>
                        <SelectItem value="1">座位 1（后手/白）</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                  <p className="text-xs text-muted-foreground">人类对战不计天梯、走独立并发。</p>
                </div>
              )}
            </div>

            {!humanMode &&
              gameSpec.configFields.map((f) => (
                <div key={f.key} className="space-y-1.5">
                  <Label htmlFor={`challenge-${f.key}`}>
                    {f.label}（{f.min}–{f.max}）
                  </Label>
                  <Input
                    id={`challenge-${f.key}`}
                    type="number"
                    min={f.min}
                    max={f.max}
                    value={matchCfg[f.key] ?? f.default}
                    onChange={(e) => setMatchCfg({ ...matchCfg, [f.key]: Number(e.target.value) })}
                  />
                </div>
              ))}

            {error && <ErrorMsg msg={error} />}
            <Button
              type="submit"
              disabled={busy || (!humanMode ? !seats[0].bot || !seats[1].bot : !humanBot)}
              className="w-full gap-1.5"
            >
              <Play className="size-4" />
              {busy ? '发起中…' : humanMode ? '开始人类对战' : '开始对局'}
            </Button>
            {!busy && (() => {
              if (!humanMode) {
                if (!seats[0].bot || !seats[1].bot)
                  return <p className="text-center text-xs text-muted-foreground">请为两个座位各选择一个 Bot</p>
              } else if (!humanBot) {
                return <p className="text-center text-xs text-muted-foreground">请选择对手 Bot</p>
              }
              return null
            })()}
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
