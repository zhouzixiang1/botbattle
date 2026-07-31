import { useCallback, useEffect, useState, type FormEvent } from 'react'
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
interface User {
  id: number
  username: string
  display_name?: string
}

type OppMode = 'self' | 'search' | 'human'

export default function Challenge() {
  const { isLoggedIn, user } = useAuth()
  const nav = useNavigate()
  const [gameId, setGameId] = useState<GameId>('holdem')
  const [mine, setMine] = useState<Bot[]>([])
  const [myBotId, setMyBotId] = useState('')
  const [oppMode, setOppMode] = useState<OppMode>('search')
  // search 模式
  const [q, setQ] = useState('')
  const [users, setUsers] = useState<User[]>([])
  const [selUser, setSelUser] = useState<User | null>(null)
  const [userBots, setUserBots] = useState<Bot[]>([])
  const [oppBotId, setOppBotId] = useState('')
  // self 模式：自己的另一个 bot
  const [selfOppBotId, setSelfOppBotId] = useState('')
  // human 模式：哪方是人类
  const [humanSeat, setHumanSeat] = useState(1)
  const [hands, setHands] = useState(70)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    try {
      const qq = `?game_id=${encodeURIComponent(gameId)}`
      if (isLoggedIn) {
        const m = await apiGet<{ bots: Bot[] }>(`/api/bots/mine${qq}`)
        setMine((m.bots || []).filter((b) => b.is_active !== 0))
      }
      setMyBotId('')
      setSelfOppBotId('')
      setSelUser(null)
      setUserBots([])
      setOppBotId('')
    } catch (e) {
      setError(errMsg(e, '加载 Bot 失败'))
    }
  }, [isLoggedIn, gameId])

  useEffect(() => {
    void load()
  }, [load])

  // 用户搜索（防抖简化：输入即查）
  useEffect(() => {
    if (oppMode !== 'search') return
    const t = setTimeout(() => {
      if (!q.trim()) {
        setUsers([])
        return
      }
      apiGet<{ users: User[] }>(`/api/users?q=${encodeURIComponent(q.trim())}`)
        .then((d) => setUsers(d.users || []))
        .catch(() => setUsers([]))
    }, 250)
    return () => clearTimeout(t)
  }, [q, oppMode])

  // 选定用户后取其该游戏 public bot
  useEffect(() => {
    if (oppMode !== 'search' || !selUser) {
      setUserBots([])
      setOppBotId('')
      return
    }
    apiGet<{ bots: Bot[] }>(
      `/api/bots/public?game_id=${encodeURIComponent(gameId)}&owner_id=${selUser.id}`,
    )
      .then((d) => {
        const rows = (d.bots || []).filter((b) => b.is_active !== 0)
        setUserBots(rows)
        setOppBotId('')
      })
      .catch(() => setUserBots([]))
  }, [selUser, gameId, oppMode])

  // 自博弈候选：自己的其他 bot（排除当前 myBotId）
  const selfOpps = mine.filter((b) => !myBotId || String(b.id) !== myBotId)

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError('')
    try {
      if (oppMode === 'human') {
        // 人类亲自上场：myBotId 是 bot，当前用户是人类
        if (!myBotId) throw new Error('请选择你的 Bot 作为对手')
        const body: Record<string, unknown> = {
          bot_id: Number(myBotId),
          human_seat: humanSeat,
          game_id: gameId,
        }
        if (gameId === 'holdem') body.hands = hands
        const d = await apiJson<{ match_id: string }>('/api/matches/human', 'POST', body)
        nav(`/play/${d.match_id}`)
        return
      }
      // bot vs bot（自博弈 / 搜索）
      const oppId = oppMode === 'self' ? selfOppBotId : oppBotId
      if (!myBotId || !oppId) throw new Error('请选择双方 Bot')
      const body: Record<string, unknown> = {
        my_bot_id: Number(myBotId),
        opponent_bot_id: Number(oppId),
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

  const inp = 'mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2.5 text-slate-800 focus:border-brand-400 focus:outline-none'

  return (
    <PageStub title="发起挑战">
      <p className="mb-4 text-sm text-slate-500">
        选择游戏与你的 Bot，再选对手方式（自博弈 / 搜索用户 / 人类亲自上场）。
      </p>
      <form onSubmit={(e) => void onSubmit(e)} className="mx-auto max-w-lg space-y-4">
        <label className="block text-sm text-slate-600">
          游戏
          <select value={gameId} onChange={(e) => setGameId(e.target.value as GameId)} className={inp}>
            {GAMES.map((g) => (
              <option key={g.id} value={g.id}>{g.label}</option>
            ))}
          </select>
        </label>

        <label className="block text-sm text-slate-600">
          你的 Bot（{gameLabel(gameId)}）
          <select value={myBotId} onChange={(e) => setMyBotId(e.target.value)} required className={inp}>
            <option value="">选择…</option>
            {mine.map((b) => (
              <option key={b.id} value={b.id}>
                {b.display_name || b.name} ({b.format}/{b.os}-{b.arch})
              </option>
            ))}
          </select>
        </label>

        <div className="rounded-lg border border-slate-200 p-3">
          <p className="mb-2 text-sm font-medium text-slate-700">对手方式</p>
          <div className="flex flex-wrap gap-2 text-sm">
            {([['search', '搜索用户'], ['self', '我的另一 Bot（自博弈）'], ['human', '人类亲自上场']] as [OppMode, string][]).map(
              ([mode, label]) => (
                <button
                  key={mode}
                  type="button"
                  onClick={() => setOppMode(mode)}
                  className={`rounded-lg border px-3 py-1.5 ${
                    oppMode === mode
                      ? 'border-brand-400 bg-brand-50 text-brand-700'
                      : 'border-slate-300 text-slate-600 hover:bg-slate-50'
                  }`}
                >
                  {label}
                </button>
              ),
            )}
          </div>

          {oppMode === 'self' && (
            <label className="mt-3 block text-sm text-slate-600">
              对手 Bot（你的另一只同游戏 Bot）
              <select value={selfOppBotId} onChange={(e) => setSelfOppBotId(e.target.value)} className={inp}>
                <option value="">选择…</option>
                {selfOpps.map((b) => (
                  <option key={b.id} value={b.id}>
                    {b.display_name || b.name}
                  </option>
                ))}
              </select>
              {selfOpps.length === 0 && (
                <span className="text-xs text-slate-400"> 你需上传至少 2 只该游戏的 Bot。</span>
              )}
            </label>
          )}

          {oppMode === 'search' && (
            <div className="mt-3 space-y-2">
              <label className="block text-sm text-slate-600">
                搜索用户名
                <input
                  value={q}
                  onChange={(e) => setQ(e.target.value)}
                  placeholder="输入用户名前缀…"
                  className={inp}
                />
              </label>
              {users.length > 0 && !selUser && (
                <ul className="max-h-40 divide-y divide-slate-100 overflow-auto rounded-lg border border-slate-200 bg-white">
                  {users.map((u) => (
                    <li key={u.id}>
                      <button
                        type="button"
                        onClick={() => setSelUser(u)}
                        className="block w-full px-3 py-2 text-left text-sm hover:bg-slate-50"
                      >
                        {u.display_name || u.username} <span className="text-xs text-slate-400">@{u.username}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
              {selUser && (
                <div className="rounded-lg bg-slate-50 p-2 text-sm">
                  已选用户：<strong>{selUser.display_name || selUser.username}</strong>{' '}
                  <button type="button" onClick={() => setSelUser(null)} className="text-xs text-brand-600 hover:underline">
                    重选
                  </button>
                  <label className="mt-2 block text-slate-600">
                    该用户的 {gameLabel(gameId)} Bot
                    <select value={oppBotId} onChange={(e) => setOppBotId(e.target.value)} className={inp}>
                      <option value="">选择…</option>
                      {userBots.map((b) => (
                        <option key={b.id} value={b.id}>
                          {b.display_name || b.name}
                        </option>
                      ))}
                    </select>
                  </label>
                </div>
              )}
            </div>
          )}

          {oppMode === 'human' && (
            <div className="mt-3 text-sm text-slate-600">
              <p className="mb-2">
                你（<strong>{user?.username}</strong>）作为人类玩家，对战上面的 Bot。
              </p>
              <label className="block">
                你坐哪一位？
                <select value={humanSeat} onChange={(e) => setHumanSeat(Number(e.target.value))} className={inp}>
                  <option value={1}>座位 1（对手先手时为白/后手）</option>
                  <option value={0}>座位 0（先手/黑）</option>
                </select>
              </label>
              <p className="mt-2 text-xs text-slate-400">人类对战不计入天梯评分，走独立并发。</p>
            </div>
          )}
        </div>

        {gameId === 'holdem' && oppMode !== 'human' && (
          <label className="block text-sm text-slate-600">
            手数（1–300）
            <input
              type="number"
              min={1}
              max={300}
              value={hands}
              onChange={(e) => setHands(Number(e.target.value) || 70)}
              className={inp}
            />
          </label>
        )}

        {error && <p className="text-sm text-error-500">{error}</p>}
        <button
          type="submit"
          disabled={busy}
          className="w-full rounded-lg bg-brand-600 px-4 py-2.5 text-sm font-medium text-white hover:bg-brand-500 disabled:opacity-50"
        >
          {busy ? '发起中…' : oppMode === 'human' ? '开始人类对战' : '开始对局'}
        </button>
      </form>
    </PageStub>
  )
}
