import { useState } from 'react'
import { Link } from 'react-router-dom'
import PageStub from '../../components/PageStub'
import { useAuth } from '../../components/useAuth'
import Dashboard from './Dashboard'
import UsersTab from './UsersTab'
import BotsTab from './BotsTab'
import MatchesTab from './MatchesTab'
import ContestsTab from './ContestsTab'
import EmailTab from './EmailTab'

type TabKey = 'dashboard' | 'users' | 'bots' | 'matches' | 'contests' | 'email'

const TABS: { key: TabKey; label: string }[] = [
  { key: 'dashboard', label: '仪表盘' },
  { key: 'users', label: '用户' },
  { key: 'bots', label: 'Bot' },
  { key: 'matches', label: '对局' },
  { key: 'contests', label: '比赛' },
  { key: 'email', label: '邮件' },
]

export default function Admin() {
  const { user, isLoggedIn } = useAuth()
  const [tab, setTab] = useState<TabKey>('dashboard')

  if (!isLoggedIn) {
    return (
      <PageStub title="管理端">
        <p>
          请先{' '}
          <Link to="/login" className="text-brand-600 hover:text-brand-700">
            登录
          </Link>
          。
        </p>
      </PageStub>
    )
  }

  if (user?.role !== 'admin') {
    return (
      <PageStub title="管理端">
        <p className="text-slate-500">
          仅管理员可访问管理端。组织者请到「比赛」页创建与管理赛事。
        </p>
      </PageStub>
    )
  }

  return (
    <PageStub title="管理端">
      {/* 标签栏 */}
      <div className="mb-5 flex flex-wrap gap-1 border-b border-slate-200">
        {TABS.map((t) => (
          <button
            key={t.key}
            type="button"
            onClick={() => setTab(t.key)}
            className={`-mb-px border-b-2 px-4 py-2 text-sm font-medium transition ${
              tab === t.key
                ? 'border-brand-500 text-brand-700'
                : 'border-transparent text-slate-500 hover:border-slate-300 hover:text-slate-700'
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      <div className="mt-2">
        {tab === 'dashboard' && <Dashboard />}
        {tab === 'users' && <UsersTab />}
        {tab === 'bots' && <BotsTab />}
        {tab === 'matches' && <MatchesTab />}
        {tab === 'contests' && <ContestsTab />}
        {tab === 'email' && <EmailTab />}
      </div>
    </PageStub>
  )
}
