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
import RuntimeTab from './RuntimeTab'
import TemplatesTab from './TemplatesTab'
import JudgeTab from './JudgeTab'
import LogsTab from './LogsTab'

type TabKey = 'dashboard' | 'users' | 'bots' | 'matches' | 'contests' | 'templates' | 'runtime' | 'judges' | 'logs' | 'email'

const TABS: { key: TabKey; label: string }[] = [
  { key: 'dashboard', label: '仪表盘' },
  { key: 'users', label: '用户' },
  { key: 'bots', label: 'Bot' },
  { key: 'matches', label: '对局记录' },
  { key: 'contests', label: '锦标赛' },
  { key: 'templates', label: '赛制模板' },
  { key: 'runtime', label: '运行时' },
  { key: 'judges', label: '裁判' },
  { key: 'logs', label: '日志' },
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
          <Link to="/login" className="text-primary hover:opacity-80">
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
        <p className="text-muted-foreground">
          仅管理员可访问管理端。组织者请到「比赛」页创建与管理赛事。
        </p>
      </PageStub>
    )
  }

  return (
    <PageStub title="管理端">
      <div className="mb-5 flex gap-1 overflow-x-auto border-b border-border">
        {TABS.map((t) => (
          <button
            key={t.key}
            type="button"
            onClick={() => setTab(t.key)}
            className={`-mb-px shrink-0 whitespace-nowrap border-b-2 px-4 py-2 text-sm font-medium transition ${
              tab === t.key
                ? 'border-primary text-primary'
                : 'border-transparent text-muted-foreground hover:border-input hover:text-foreground'
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
        {tab === 'templates' && <TemplatesTab />}
        {tab === 'runtime' && <RuntimeTab />}
        {tab === 'judges' && <JudgeTab />}
        {tab === 'logs' && <LogsTab />}
        {tab === 'email' && <EmailTab />}
      </div>
    </PageStub>
  )
}
