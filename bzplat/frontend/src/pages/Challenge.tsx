import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import PageStub from '../components/PageStub'
import { useAuth } from '../components/useAuth'
import { apiGet, apiJson, errMsg } from '../api'

interface Bot {
  id: number
  name: string
  display_name?: string
  owner_id?: number
  format?: string
  os?: string
  arch?: string
  is_active?: number
}

export default function Challenge() {
  const { isLoggedIn, user } = useAuth()
  const nav = useNavigate()
  const [mine, setMine] = useState<Bot[]>([])
  const [publicBots, setPublicBots] = useState<Bot[]>([])
  const [myBotId, setMyBotId] = useState('')
  const [oppBotId, setOppBotId] = useState('')
  const [hands, setHands] = useState(70)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    try {
      const pub = await apiGet<{ bots: Bot[] }>('/api/bots/public')
      setPublicBots((pub.bots || []).filter((b) => b.is_active !== 0))
      if (isLoggedIn) {
        const m = await apiGet<{ bots: Bot[] }>('/api/bots/mine')
        setMine((m.bots || []).filter((b) => b.is_active !== 0))
      }
    } catch (e) {
      setError(errMsg(e, '加载 Bot 失败'))
    }
  }, [isLoggedIn])

  useEffect(() => {
    void load()
  }, [load])

  const opponents = publicBots.filter((b) => {
    if (!myBotId) return true
    return String(b.id) !== myBotId
  })

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError('')
    try {
      const d = await apiJson<{ match_id: string }>('/api/matches/challenge', 'POST', {
        my_bot_id: Number(myBotId),
        opponent_bot_id: Number(oppBotId),
        hands,
      })
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
      <p className="mb-4">选择己方与公开对手 Bot，发起自发对局（默认 70 手）。</p>
      <form onSubmit={(e) => void onSubmit(e)} className="mx-auto max-w-lg space-y-4">
        <label className="block text-sm text-slate-600">
          我的 Bot
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
          对手 Bot（公开）
          <select
            value={oppBotId}
            onChange={(e) => setOppBotId(e.target.value)}
            required
            className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2.5 text-slate-800 focus:border-brand-400 focus:outline-none"
          >
            <option value="">选择…</option>
            {opponents.map((b) => (
              <option key={b.id} value={b.id}>
                {b.display_name || b.name}
                {b.owner_id === user?.id ? '（自己）' : ''} — {b.format}/{b.os}
              </option>
            ))}
          </select>
        </label>
        <label className="block text-sm text-slate-600">
          手数
          <input
            type="number"
            min={1}
            max={70}
            value={hands}
            onChange={(e) => setHands(Number(e.target.value) || 70)}
            className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2.5 text-slate-800 focus:border-brand-400 focus:outline-none"
          />
        </label>
        {error && <p className="text-sm text-error-500">{error}</p>}
        <button
          type="submit"
          disabled={busy || !myBotId || !oppBotId}
          className="w-full rounded-lg bg-brand-600 px-4 py-2.5 text-sm font-medium text-white hover:bg-brand-500 disabled:opacity-60"
        >
          {busy ? '发起中…' : '发起挑战并观战'}
        </button>
      </form>
      {mine.length === 0 && (
        <p className="mt-4 text-center text-sm text-slate-500">
          还没有 Bot？去{' '}
          <Link to="/my-bots" className="text-brand-600 hover:text-brand-700">
            我的 Bot
          </Link>{' '}
          上传。
        </p>
      )}
    </PageStub>
  )
}
