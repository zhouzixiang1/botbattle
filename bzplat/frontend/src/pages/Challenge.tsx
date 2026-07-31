import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import PageStub from '../components/PageStub'
import OpponentPickerModal, { type PickBot } from '../components/OpponentPickerModal'
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
  const [hands, setHands] = useState(70)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

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
        }
        if (gameId === 'holdem') body.hands = hands
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
        选择游戏与你的 Bot，再选对手（搜索/自博弈/人类亲自上场）。
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

        {/* 对手模式切换 */}
        <div className="rounded-lg border border-slate-200 p-3">
          <div className="flex flex-wrap gap-2 text-sm">
            <button
              type="button"
              onClick={() => setHumanMode(false)}
              className={`rounded-lg border px-3 py-1.5 ${!humanMode ? 'border-brand-400 bg-brand-50 text-brand-700' : 'border-slate-300 text-slate-600 hover:bg-slate-50'}`}
            >
              与 Bot 对战
            </button>
            <button
              type="button"
              onClick={() => setHumanMode(true)}
              className={`rounded-lg border px-3 py-1.5 ${humanMode ? 'border-brand-400 bg-brand-50 text-brand-700' : 'border-slate-300 text-slate-600 hover:bg-slate-50'}`}
            >
              人类亲自上场
            </button>
          </div>

          {!humanMode ? (
            <div className="mt-3">
              <button
                type="button"
                onClick={() => setPickerOpen(true)}
                className="w-full rounded-lg border border-dashed border-slate-300 px-3 py-2.5 text-sm text-slate-600 hover:bg-slate-50"
              >
                {opp ? (
                  <span>
                    <strong>{opp.display_name || opp.name}</strong>
                    <span className="ml-2 text-xs text-slate-400">
                      {opp.owner_display || opp.owner_name || `#${opp.owner_id}`}
                      {opp.owner_id === user?.id ? '（自博弈）' : ''}
                    </span>
                  </span>
                ) : (
                  '＋ 选择对手 Bot（搜索 / 我的 / 按用户）'
                )}
              </button>
            </div>
          ) : (
            <div className="mt-3 text-sm text-slate-600">
              <p className="mb-2">
                你（<strong>{user?.username}</strong>）作为人类玩家，对战上面的 Bot。
              </p>
              <label className="block">
                你坐哪一位？
                <select value={humanSeat} onChange={(e) => setHumanSeat(Number(e.target.value))} className={inp}>
                  <option value={1}>座位 1（后手/白）</option>
                  <option value={0}>座位 0（先手/黑）</option>
                </select>
              </label>
              <p className="mt-2 text-xs text-slate-400">人类对战不计天梯、走独立并发。</p>
            </div>
          )}
        </div>

        {gameId === 'holdem' && !humanMode && (
          <label className="block text-sm text-slate-600">
            手数（1–300）
            <input
              type="number" min={1} max={300}
              value={hands}
              onChange={(e) => setHands(Number(e.target.value) || 70)}
              className={inp}
            />
          </label>
        )}

        {error && <p className="text-sm text-error-500">{error}</p>}
        <button
          type="submit"
          disabled={busy || (!humanMode && !opp)}
          className="w-full rounded-lg bg-brand-600 px-4 py-2.5 text-sm font-medium text-white hover:bg-brand-500 disabled:opacity-50"
        >
          {busy ? '发起中…' : humanMode ? '开始人类对战' : '开始对局'}
        </button>
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
