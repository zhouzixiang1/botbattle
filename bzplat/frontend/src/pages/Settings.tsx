import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import PageStub from '../components/PageStub'
import { useAuth } from '../components/useAuth'
import { apiGet, apiJson, errMsg } from '../api'
import { gameLabel } from '../lib/games'

interface Prefs {
  email_match_done: number
  email_followed: number
  email_contest: number
  email_comment: number
}
interface FavBot {
  id: number
  name: string
  display_name: string
  game_id: string
  owner_name?: string
  rating?: number
}

export default function Settings() {
  const { user, refresh } = useAuth()
  const [tab, setTab] = useState<'profile' | 'password' | 'notifications' | 'favorites'>('profile')
  const [error, setError] = useState('')
  const [msg, setMsg] = useState('')

  // profile 表单
  const [displayName, setDisplayName] = useState(user?.display_name || '')
  const [bio, setBio] = useState(user?.bio || '')
  // password 表单
  const [oldPw, setOldPw] = useState('')
  const [newPw, setNewPw] = useState('')
  // prefs
  const [prefs, setPrefs] = useState<Prefs>({
    email_match_done: 0, email_followed: 0, email_contest: 0, email_comment: 0,
  })
  // favorites
  const [favs, setFavs] = useState<FavBot[]>([])

  useEffect(() => {
    apiGet<{ prefs: Prefs }>('/api/notification-prefs')
      .then((d) => setPrefs(d.prefs))
      .catch(() => {})
    apiGet<{ favorites: FavBot[] }>('/api/auth/me/favorites')
      .then((d) => setFavs(d.favorites || []))
      .catch(() => {})
  }, [])

  if (!user) {
    return (
      <PageStub title="设置">
        <p className="py-8 text-center text-sm text-slate-400">请先登录</p>
      </PageStub>
    )
  }

  function saveProfile(e: React.FormEvent) {
    e.preventDefault()
    setError(''); setMsg('')
    apiJson('/api/auth/profile', 'PUT', { display_name: displayName, bio })
      .then(() => {
        if (refresh) refresh()
        setMsg('资料已保存')
      })
      .catch((e) => setError(errMsg(e)))
  }

  function uploadAvatar(e: React.ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0]
    if (!f) return
    setError(''); setMsg('')
    const fd = new FormData()
    fd.append('file', f)
    apiJson('/api/auth/avatar', 'POST', fd)
      .then(() => {
        if (refresh) refresh()
        setMsg('头像已更新')
      })
      .catch((e) => setError(errMsg(e)))
  }

  function changePassword(e: React.FormEvent) {
    e.preventDefault()
    setError(''); setMsg('')
    apiJson('/api/auth/change-password', 'POST', { old_password: oldPw, new_password: newPw })
      .then(() => {
        setMsg('密码已修改，请重新登录')
        setOldPw(''); setNewPw('')
      })
      .catch((e) => setError(errMsg(e)))
  }

  function togglePref(key: keyof Prefs) {
    const next = { ...prefs, [key]: prefs[key] ? 0 : 1 }
    setPrefs(next)
    apiJson('/api/notification-prefs', 'PUT', next).catch((e) => setError(errMsg(e)))
  }

  const avatarUrl = user.avatar ? `/avatars/${user.avatar}` : ''

  return (
    <PageStub title="个人设置">
      {/* Tabs */}
      <div className="mb-4 flex gap-1 border-b border-slate-200">
        {(
          [
            ['profile', '资料'],
            ['password', '密码'],
            ['notifications', '通知偏好'],
            ['favorites', '我的收藏'],
          ] as const
        ).map(([k, label]) => (
          <button
            key={k}
            type="button"
            onClick={() => setTab(k)}
            className={`-mb-px border-b-2 px-4 py-2 text-sm font-medium ${
              tab === k ? 'border-brand-500 text-brand-700' : 'border-transparent text-slate-500 hover:text-slate-700'
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {error && <p className="mb-3 text-sm text-error-500">{error}</p>}
      {msg && <p className="mb-3 text-sm text-success-600">{msg}</p>}

      {/* 资料 */}
      {tab === 'profile' && (
        <form onSubmit={saveProfile} className="max-w-md space-y-4">
          <div>
            <label className="mb-1 block text-sm text-slate-600">头像</label>
            <div className="flex items-center gap-3">
              <div className="h-16 w-16 overflow-hidden rounded-full border border-slate-200 bg-slate-100">
                {avatarUrl ? (
                  <img src={avatarUrl} alt="avatar" className="h-full w-full object-cover" />
                ) : (
                  <div className="flex h-full w-full items-center justify-center text-xl font-bold text-slate-400">
                    {(user.display_name || user.username).charAt(0).toUpperCase()}
                  </div>
                )}
              </div>
              <input type="file" accept="image/png,image/jpeg,image/webp,image/gif" onChange={uploadAvatar}
                className="text-xs text-slate-500" />
            </div>
            <p className="mt-1 text-xs text-slate-400">png/jpeg/webp/gif，≤2MB</p>
          </div>
          <div>
            <label className="mb-1 block text-sm text-slate-600">显示名</label>
            <input value={displayName} onChange={(e) => setDisplayName(e.target.value)} maxLength={64}
              className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-700" />
          </div>
          <div>
            <label className="mb-1 block text-sm text-slate-600">简介</label>
            <textarea value={bio} onChange={(e) => setBio(e.target.value)} maxLength={500} rows={3}
              className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-700" />
          </div>
          <button type="submit" className="rounded-lg bg-brand-600 px-4 py-2 text-sm font-medium text-white hover:bg-brand-500">
            保存
          </button>
        </form>
      )}

      {/* 密码 */}
      {tab === 'password' && (
        <form onSubmit={changePassword} className="max-w-md space-y-4">
          <div>
            <label className="mb-1 block text-sm text-slate-600">当前密码</label>
            <input type="password" value={oldPw} onChange={(e) => setOldPw(e.target.value)} required
              className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-700" />
          </div>
          <div>
            <label className="mb-1 block text-sm text-slate-600">新密码（≥8 位）</label>
            <input type="password" value={newPw} onChange={(e) => setNewPw(e.target.value)} required minLength={8}
              className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-700" />
          </div>
          <button type="submit" className="rounded-lg bg-brand-600 px-4 py-2 text-sm font-medium text-white hover:bg-brand-500">
            修改密码
          </button>
          <p className="text-xs text-slate-400">修改密码后会清除所有登录会话，需重新登录。</p>
        </form>
      )}

      {/* 通知偏好 */}
      {tab === 'notifications' && (
        <div className="max-w-md space-y-3">
          <p className="text-sm text-slate-500">站内通知始终开启；以下控制是否同时发送邮件提醒（需管理员配置 SMTP）。</p>
          {([
            ['email_match_done', '对局完成'],
            ['email_followed', '被关注'],
            ['email_contest', '赛事阶段变化'],
            ['email_comment', '被评论'],
          ] as const).map(([key, label]) => (
            <label key={key} className="flex items-center gap-2 text-sm text-slate-700">
              <input
                type="checkbox"
                checked={!!prefs[key]}
                onChange={() => togglePref(key)}
                className="h-4 w-4 rounded border-slate-300"
              />
              {label} 邮件提醒
            </label>
          ))}
        </div>
      )}

      {/* 我的收藏 */}
      {tab === 'favorites' && (
        <div>
          {favs.length === 0 ? (
            <p className="py-8 text-center text-sm text-slate-400">暂无收藏的 Bot</p>
          ) : (
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {favs.map((b) => (
                <Link key={b.id} to={`/bot/${b.id}`} className="card block p-4 hover:border-brand-300">
                  <div className="flex items-center justify-between">
                    <span className="font-medium text-slate-800">{b.display_name || b.name}</span>
                    <span className="rounded-full bg-brand-50 px-2 py-0.5 text-[10px] text-brand-700">{gameLabel(b.game_id)}</span>
                  </div>
                  <p className="mt-1 text-xs text-slate-400">
                    @{b.name}{b.owner_name ? ` · ${b.owner_name}` : ''}{b.rating != null ? ` · ${Number(b.rating).toFixed(0)}` : ''}
                  </p>
                </Link>
              ))}
            </div>
          )}
        </div>
      )}

      <p className="mt-6">
        <Link to={`/user/${encodeURIComponent(user.username)}`} className="text-sm text-brand-600 hover:text-brand-700">
          ← 返回我的主页
        </Link>
      </p>
    </PageStub>
  )
}
