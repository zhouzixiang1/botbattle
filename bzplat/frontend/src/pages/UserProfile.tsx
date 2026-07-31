import { Link, useParams } from 'react-router-dom'
import PageStub from '../components/PageStub'
import { useAuth } from '../components/useAuth'

export default function UserProfile() {
  const { name } = useParams()
  const { user } = useAuth()
  const isSelf = !!user && user.username === name

  return (
    <PageStub title="用户资料">
      <div className="rounded-xl border border-slate-200 bg-white p-4 text-sm">
        <p>
          用户：
          <span className="ml-1 font-medium text-brand-700">{name ?? '—'}</span>
        </p>
        {isSelf && user && (
          <div className="mt-3 space-y-1 text-slate-400">
            <p>显示名：{user.display_name || user.username}</p>
            <p>邮箱：{user.email}</p>
            <p>
              角色：<span className="text-slate-700">{user.role}</span>
            </p>
          </div>
        )}
        <p className="mt-4">
          <Link to="/leaderboard" className="text-brand-600 hover:text-brand-700">
            查看排行榜 →
          </Link>
        </p>
      </div>
    </PageStub>
  )
}
