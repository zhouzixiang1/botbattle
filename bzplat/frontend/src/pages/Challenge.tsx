import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { Swords, User, Bot as BotIcon, Plus, Play } from 'lucide-react'
import PageStub from '@/components/PageStub'
import OpponentPickerModal, { type PickBot } from '@/components/OpponentPickerModal'
import { useAuth } from '@/components/useAuth'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { ErrorMsg } from '@/components/ui/status'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { cn } from '@/lib/utils'
import { apiGet, apiJson, errMsg } from '@/api'
import { GAMES, gameLabel, type GameId } from '@/lib/games'
import { getGame, defaultMatchConfig } from '@/games'

interface Bot {
  id: number
  name: string
  display_name?: string
  owner_id?: number
  format?: string
  os?: string
  arch?: string
  is_active?: number
  game_id?: string
}

export default function Challenge() {
  const { isLoggedIn, user } = useAuth()
  const nav = useNavigate()
  const [gameId, setGameId] = useState<GameId>('holdem')
  const [mine, setMine] = useState<Bot[]>([])
  const [myBotId, setMyBotId] = useState('')
  // 对手：bot 模式选定的对手 bot；human 模式
  const [opp, setOpp] = useState<PickBot | null>(null)
  const [humanMode, setHumanMode] = useState(false) // true=人类亲自上场
  const [humanSeat, setHumanSeat] = useState(1)
  const [pickerOpen, setPickerOpen] = useState(false)
  // 动态对局参数（按所选游戏的 configFields 驱动，消除散落 hands 状态 + 漏 pencil n_dots）
  const [matchCfg, setMatchCfg] = useState<Record<string, number>>(defaultMatchConfig('holdem'))
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const gameSpec = getGame(gameId)
  // 切换游戏时重置参数为该游戏默认
  useEffect(() => {
    setMatchCfg(defaultMatchConfig(gameId))
  }, [gameId])

  const load = useCallback(async () => {
    try {
      const m = await apiGet<{ bots: Bot[] }>(`/api/bots/mine?game_id=${encodeURIComponent(gameId)}`)
      setMine((m.bots || []).filter((b) => b.is_active !== 0))
      setMyBotId('')
      setOpp(null)
    } catch (e) {
      setError(errMsg(e, '加载 Bot 失败'))
    }
  }, [gameId])

  useEffect(() => {
    void load()
  }, [load])

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError('')
    try {
      if (humanMode) {
        if (!myBotId) throw new Error('请选择你的 Bot 作为对手')
        const body: Record<string, unknown> = {
          bot_id: Number(myBotId),
          human_seat: humanSeat,
          game_id: gameId,
          ...matchCfg,
        }
        const d = await apiJson<{ match_id: string }>('/api/matches/human', 'POST', body)
        nav(`/play/${d.match_id}`)
        return
      }
      // bot vs bot
      if (!myBotId) throw new Error('请选择你的 Bot')
      if (!opp) throw new Error('请选择对手 Bot')
      const body: Record<string, unknown> = {
        my_bot_id: Number(myBotId),
        opponent_bot_id: opp.id,
        game_id: gameId,
        ...matchCfg,
      }
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
      <PageStub title="发起挑战" subtitle="选择游戏与你的 Bot，再选对手（搜索/自博弈/人类亲自上场）">
        <Card className="mx-auto max-w-lg">
          <CardContent>
            <p className="text-sm text-muted-foreground">
              请先{' '}
              <Link to="/login" className="font-medium text-primary hover:underline">
                登录
              </Link>{' '}
              后选择己方 Bot 发起挑战。
            </p>
          </CardContent>
        </Card>
      </PageStub>
    )
  }

  return (
    <PageStub title="发起挑战" subtitle="选择游戏与你的 Bot，再选对手（搜索/自博弈/人类亲自上场）">
      <form onSubmit={(e) => void onSubmit(e)} className="mx-auto max-w-2xl">
        <Card>
          <CardContent className="space-y-4">
            {/* 游戏 + 己方 Bot：桌面端双栏，移动端单列 */}
            <div className="grid gap-4 sm:grid-cols-2">
              <div className="space-y-1.5">
                <Label>游戏</Label>
                <Select value={gameId} onValueChange={(v) => setGameId(v as GameId)}>
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

              <div className="space-y-1.5">
                <Label>你的 Bot（{gameLabel(gameId)}）</Label>
                <Select value={myBotId} onValueChange={setMyBotId}>
                  <SelectTrigger className="mt-1.5 h-9 w-full">
                    <SelectValue placeholder="选择…" />
                  </SelectTrigger>
                  <SelectContent>
                    {mine.map((b) => (
                      <SelectItem key={b.id} value={String(b.id)}>
                        {b.display_name || b.name} ({b.format}/{b.os}-{b.arch})
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            </div>

        {/* 对手模式切换 */}
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
              与 Bot 对战
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
            <div className="mt-3">
              <button
                type="button"
                onClick={() => setPickerOpen(true)}
                className="flex w-full items-center gap-2 rounded-lg border border-dashed border-input px-3 py-2.5 text-sm text-muted-foreground hover:bg-accent"
              >
                {opp ? (
                  <span className="flex flex-wrap items-center gap-2 text-foreground">
                    <BotIcon className="size-4 text-primary" />
                    <strong>{opp.display_name || opp.name}</strong>
                    <span className="text-xs text-muted-foreground">
                      {opp.owner_display || opp.owner_name || `#${opp.owner_id}`}
                      {opp.owner_id === user?.id ? '（自博弈）' : ''}
                    </span>
                  </span>
                ) : (
                  <>
                    <Plus className="size-4" />
                    选择对手 Bot（搜索 / 我的 / 按用户）
                  </>
                )}
              </button>
            </div>
          ) : (
            <div className="mt-3 text-sm text-muted-foreground">
              <p className="mb-2">
                你（<strong className="text-foreground">{user?.username}</strong>）作为人类玩家，对战上面的 Bot。
              </p>
              <div className="space-y-1.5">
                <span className="text-sm text-muted-foreground">你坐哪一位？</span>
                <Select value={String(humanSeat)} onValueChange={(v) => setHumanSeat(Number(v))}>
                  <SelectTrigger className="mt-1.5 h-9 w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="1">座位 1（后手/白）</SelectItem>
                    <SelectItem value="0">座位 0（先手/黑）</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <p className="mt-2 text-xs text-muted-foreground">人类对战不计天梯、走独立并发。</p>
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
          disabled={busy || (!humanMode && !opp)}
          className="w-full gap-1.5"
        >
          <Play className="size-4" />
          {busy ? '发起中…' : humanMode ? '开始人类对战' : '开始对局'}
        </Button>
          </CardContent>
        </Card>
      </form>

      {pickerOpen && (
        <OpponentPickerModal
          gameId={gameId}
          myUserId={user?.id}
          onClose={() => setPickerOpen(false)}
          onPick={(b) => {
            setOpp(b)
            setPickerOpen(false)
          }}
        />
      )}
    </PageStub>
  )
}
