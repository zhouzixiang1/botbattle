import { useEffect, useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import PageStub from '../components/PageStub'
import { useAuth } from '../components/useAuth'
import { apiGet, apiJson, errMsg } from '../api'

interface Contest {
  id: number
  title: string
  status: string
  description?: string
  hands_per_match?: number
  created_at?: string
}

export default function Contests() {
  const { user, isLoggedIn } = useAuth()
  const [list, setList] = useState<Contest[]>([])
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [hands, setHands] = useState(70)
  const [error, setError] = useState('')
  const canCreate = user?.role === 'organizer' || user?.role === 'admin'

  const load = () =>
    apiGet<{ contests: Contest[] }>('/api/contests')
      .then((d) => setList(d.contests || []))
      .catch((e) => setError(errMsg(e)))

  useEffect(() => {
    void load()
  }, [])

  const onCreate = async (e: FormEvent) => {
    e.preventDefault()
    setError('')
    try {
      await apiJson('/api/contests', 'POST', {
        title,
        description,
        hands_per_match: hands,
      })
      setTitle('')
      setDescription('')
      await load()
    } catch (err) {
      setError(errMsg(err))
    }
  }

  return (
    <PageStub title="比赛">
      <p className="mb-4 text-sm text-slate-400">
        组织者发布比赛，选手派遣 Bot 参加循环赛。
      </p>
      {error && <p className="mb-3 text-sm text-error-500">{error}</p>}

      {canCreate && isLoggedIn && (
        <form
          onSubmit={(e) => void onCreate(e)}
          className="mb-6 flex flex-wrap items-end gap-3 rounded-xl border border-slate-200 bg-white p-4"
        >
          <label className="text-sm text-slate-600">
            标题
            <input
              className="mt-1 block rounded-lg border border-slate-300 bg-white px-3 py-2 text-slate-800"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              required
            />
          </label>
          <label className="text-sm text-slate-600">
            说明
            <input
              className="mt-1 block rounded-lg border border-slate-300 bg-white px-3 py-2 text-slate-800"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
          </label>
          <label className="text-sm text-slate-600">
            手数
            <input
              type="number"
              min={1}
              max={70}
              className="mt-1 block w-24 rounded-lg border border-slate-300 bg-white px-3 py-2 text-slate-800"
              value={hands}
              onChange={(e) => setHands(Number(e.target.value))}
            />
          </label>
          <button
            type="submit"
            className="rounded-lg bg-brand-600 px-4 py-2 text-sm text-white hover:bg-brand-500"
          >
            创建比赛
          </button>
        </form>
      )}

      <ul className="divide-y divide-slate-700/80 overflow-hidden rounded-xl border border-slate-200 bg-white">
        {list.map((c) => (
          <li key={c.id} className="px-4 py-3">
            <Link
              to={`/contests/${c.id}`}
              className="text-lg text-brand-700 hover:underline"
            >
              {c.title}
            </Link>
            <div className="text-xs text-slate-500">
              {c.status} · {c.hands_per_match} 手 · {c.created_at}
            </div>
          </li>
        ))}
        {!list.length && (
          <li className="px-4 py-8 text-center text-slate-500">暂无比赛</li>
        )}
      </ul>
    </PageStub>
  )
}
