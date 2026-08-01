import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import PageStub from '../components/PageStub'
import { useAuth } from '../components/useAuth'
import { apiGet, errMsg } from '../api'
import { gameLabel } from '../lib/games'

interface UserProfileData {
  id: number
  username: string
  display_name: string
  role: string
  bio: string
  avatar: string
  created_at: string
  last_login_at?: string
  stats: {
    wins: number
    losses: number
    draws: number
    matches_played: number
    net_chips: number
    rated_bots: number
  }
  bot_count: number
}

interface BotRow {
  id: number
  name: string
  display_name: string
  game_id: string
  description?: string
  is_active: number | boolean
  is_public: number | boolean
}

export default function UserProfile() {
  const { name } = useParams<{ name: string }>()
  const { user } = useAuth()
  const isSelf = !!user && user.username === name

  const [profile, setProfile] = useState<UserProfileData | null>(null)
  const [bots, setBots] = useState<BotRow[]>([])
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    if (!name) return
    setLoading(true)
    Promise.all([
      apiGet<{ profile: UserProfileData }>(`/api/users/${encodeURIComponent(name)}/profile`),
      apiGet<{ bots: BotRow[] }>(`/api/users/${encodeURIComponent(name)}/bots`),
    ])
      .then(([p, b]) => {
        setProfile(p.profile)
        setBots(b.bots || [])
      })
      .catch((e) => setError(errMsg(e)))
      .finally(() => setLoading(false))
  }, [name])

  if (loading) {
    return (
      <PageStub title="用户资料">
        <p className="py-8 text-center text-sm text-slate-400">加载中…</p>
      </PageStub>
    )
  }
  if (error || !profile) {
    return (
      <PageStub title="用户资料">
        <p className="mb-3 text-sm text-error-500">{error || '用户不存在或已停用'}</p>
        <Link to="/leaderboard" className="text-sm text-brand-600 hover:text-brand-700">
          ← 返回排行榜
        </Link>
      </PageStub>
    )
  }

  const displayName = profile.display_name || profile.username
  const avatarUrl = profile.avatar ? `/avatars/${profile.avatar}` : ''
  const totalGames = profile.stats.matches_played || 0
  const wins = profile.stats.wins || 0
  const wr = totalGames > 0 ? ((wins + (profile.stats.draws || 0) * 0.5) / totalGames) * 100 : 0

  return (
    <PageStub title={displayName}>
      {/* 顶部信息卡 */}
      <div className="card mb-5 p-5">
        <div className="flex flex-wrap items-start gap-4">
          <div className="h-20 w-20 shrink-0 overflow-hidden rounded-full border border-slate-200 bg-slate-100">
            {avatarUrl ? (
              <img src={avatarUrl} alt={displayName} className="h-full w-full object-cover" />
            ) : (
              <div className="flex h-full w-full items-center justify-center text-2xl font-bold text-slate-400">
                {displayName.charAt(0).toUpperCase()}
              </div>
            )}
          </div>
          <div className="min-w-0 flex-1">
            <h2 className="text-xl font-bold text-slate-900">{displayName}</h2>
            <p className="text-sm text-slate-500">@{profile.username}</p>
            {profile.role !== 'user' && (
              <span className="mt-1 inline-block rounded-full bg-brand-50 px-2 py-0.5 text-xs font-medium text-brand-700">
                {profile.role === 'admin' ? '管理员' : '组织者'}
              </span>
            )}
            {profile.bio && <p className="mt-2 text-sm text-slate-600">{profile.bio}</p>}
            <div className="mt-2 text-xs text-slate-400">
              注册于 {profile.created_at?.slice(0, 10)}
              {totalGames > 0 && <> · 参与 {totalGames} 场对局</>}
            </div>
          </div>

          {isSelf && (
            <Link
              to="/settings"
              className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-xs text-slate-600 hover:bg-slate-50"
            >
              编辑资料
            </Link>
          )}
        </div>

        {/* 总战绩 */}
        <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
          <div className="rounded-xl border border-slate-200 bg-white px-4 py-3 text-center">
            <div className="text-xs uppercase tracking-wide text-slate-400">总胜率</div>
            <div className="mt-1 font-mono text-xl font-bold text-slate-800">{wr.toFixed(1)}%</div>
          </div>
          <div className="rounded-xl border border-slate-200 bg-white px-4 py-3 text-center">
            <div className="text-xs uppercase tracking-wide text-slate-400">胜</div>
            <div className="mt-1 font-mono text-xl font-bold text-success-600">{wins}</div>
          </div>
          <div className="rounded-xl border border-slate-200 bg-white px-4 py-3 text-center">
            <div className="text-xs uppercase tracking-wide text-slate-400">负/平</div>
            <div className="mt-1 font-mono text-xl font-bold text-error-600">
              {profile.stats.losses || 0}
              <span className="text-sm text-slate-400"> / {profile.stats.draws || 0}</span>
            </div>
          </div>
          <div className="rounded-xl border border-slate-200 bg-white px-4 py-3 text-center">
            <div className="text-xs uppercase tracking-wide text-slate-400">Bot 数</div>
            <div className="mt-1 font-mono text-xl font-bold text-brand-700">{profile.bot_count}</div>
          </div>
        </div>
      </div>

      {/* Bot 列表 */}
      <h3 className="mb-3 text-sm font-semibold text-slate-700">
        Bot 列表（{bots.length}）
      </h3>
      {bots.length === 0 ? (
        <div className="rounded-xl border border-slate-200 bg-white py-8 text-center text-sm text-slate-400">
          暂无公开 Bot
        </div>
      ) : (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {bots.map((b) => (
            <Link
              key={b.id}
              to={`/bot/${b.id}`}
              className="card block p-4 transition hover:border-brand-300 hover:shadow-sm"
            >
              <div className="flex items-center justify-between">
                <span className="font-medium text-slate-800">
                  {b.display_name || b.name}
                </span>
                <span className="rounded-full bg-brand-50 px-2 py-0.5 text-[10px] font-medium text-brand-700">
                  {gameLabel(b.game_id)}
                </span>
              </div>
              <p className="mt-1 truncate text-xs text-slate-400">@{b.name}</p>
              {b.description && (
                <p className="mt-1 line-clamp-2 text-xs text-slate-500">{b.description}</p>
              )}
            </Link>
          ))}
        </div>
      )}
    </PageStub>
  )
}
