import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import PageStub from '../components/PageStub'
import { useAuth } from '../components/useAuth'
import { apiGet, apiJson, errMsg } from '../api'
import { GAMES, gameLabel, type GameId } from '../lib/games'

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
  const { isLoggedIn } = useAuth()
  const nav = useNavigate()
  const [gameId, setGameId] = useState<GameId>('holdem')
  const [mine, setMine] = useState<Bot[]>([])
  const [publicBots, setPublicBots] = useState<Bot[]>([])
  const [myBotId, setMyBotId] = useState('')
  const [oppBotId, setOppBotId] = useState('')
  const [hands, setHands] = useState(70)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    try {
      const q = `?game_id=${encodeURIComponent(gameId)}`
      const pub = await apiGet<{ bots: Bot[] }>(`/api/bots/public${q}`)
      setPublicBots((pub.bots || []).filter((b) => b.is_active !== 0))
      if (isLoggedIn) {
        const m = await apiGet<{ bots: Bot[] }>(`/api/bots/mine${q}`)
        setMine((m.bots || []).filter((b) => b.is_active !== 0))
      }
      setMyBotId('')
      setOppBotId('')
    } catch (e) {
      setError(errMsg(e, '加载 Bot 失败'))
    }
  }, [isLoggedIn, gameId])

  useEffect(() => {
    void load()
  }, [load])

  const opponents = useMemo(
    () => publicBots.filter((b) => !myBotId || String(b.id) !== myBotId),
    [publicBots, myBotId],
  )

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError('')
    try {
      const body: Record<string, unknown> = {
        my_bot_id: Number(myBotId),
        opponent_bot_id: Number(oppBotId),
        game_id: gameId,
      }
      if (gameId === 'holdem') body.hands = hands
      const d = await apiJson<{ match_id: string }>('/api/matches/challenge', 'POST', body)
      nav(`/watch/${d.match_id}`)
    } catch (err) {
      setError(errMsg(err, '发起挑战失败'))
    } finally {
      setBusy(false)
    }
  }

  if (!isLoggedIn) {
    return (
      <PageStub title="发起挑战">
        <p>
          请先{' '}
          <Link to="/login" className="text-brand-600 hover:text-brand-700">
            登录
          </Link>{' '}
          后选择己方 Bot 发起挑战。
        </p>
      </PageStub>
    )
  }

  return (
    <PageStub title="发起挑战">
      <p className="mb-4">选择游戏与 Bot，发起自发对局。</p>
      <form onSubmit={(e) => void onSubmit(e)} className="mx-auto max-w-lg space-y-4">
        <label className="block text-sm text-slate-600">
          游戏
          <select
            value={gameId}
            onChange={(e) => setGameId(e.target.value as GameId)}
            className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2.5 text-slate-800 focus:border-brand-400 focus:outline-none"
          >
            {GAMES.map((g) => (
              <option key={g.id} value={g.id}>
                {g.label}
              </option>
            ))}
          </select>
        </label>
        <label className="block text-sm text-slate-600">
          我的 Bot（{gameLabel(gameId)}）
          <select
            value={myBotId}
            onChange={(e) => setMyBotId(e.target.value)}
            required
            className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2.5 text-slate-800 focus:border-brand-400 focus:outline-none"
          >
            <option value="">选择…</option>
            {mine.map((b) => (
              <option key={b.id} value={b.id}>
                {b.display_name || b.name} ({b.format}/{b.os}-{b.arch})
              </option>
            ))}
          </select>
        </label>
        <label className="block text-sm text-slate-600">
          对手 Bot
          <select
            value={oppBotId}
            onChange={(e) => setOppBotId(e.target.value)}
            required
            className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2.5 text-slate-800 focus:border-brand-400 focus:outline-none"
          >
            <option value="">选择…</option>
            {opponents.map((b) => (
              <option key={b.id} value={b.id}>
                {b.display_name || b.name} ({b.format}/{b.os}-{b.arch})
              </option>
            ))}
          </select>
        </label>
        {gameId === 'holdem' && (
          <label className="block text-sm text-slate-600">
            手数（1–70）
            <input
              type="number"
              min={1}
              max={70}
              value={hands}
              onChange={(e) => setHands(Number(e.target.value) || 70)}
              className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2.5 text-slate-800 focus:border-brand-400 focus:outline-none"
            />
          </label>
        )}
        {error && <p className="text-sm text-error-500">{error}</p>}
        <button
          type="submit"
          disabled={busy || !myBotId || !oppBotId}
          className="w-full rounded-lg bg-brand-600 px-4 py-2.5 text-sm font-medium text-white hover:bg-brand-500 disabled:opacity-50"
        >
          {busy ? '发起中…' : '开始对局'}
        </button>
      </form>
    </PageStub>
  )
}
