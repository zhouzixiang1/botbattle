import type { LucideIcon } from 'lucide-react'
import {
  Bot,
  Inbox,
  LayoutDashboard,
  Medal,
  ScrollText,
  ShieldCheck,
  Swords,
  Users,
} from 'lucide-react'
import { Link, useSearchParams } from 'react-router-dom'

import { PageFrame, PageHeader } from '@/components/layout'
import { useAuth } from '@/components/useAuth'
import { Button } from '@/components/ui/button'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { EmptyState } from '@/components/ui/status'
import { cn } from '@/lib/utils'
import BotsTab from '@/pages/admin/BotsTab'
import ContestsTab from '@/pages/admin/ContestsTab'
import Dashboard from '@/pages/admin/Dashboard'
import CommunicationsTab from '@/pages/admin/EmailTab'
import LogsTab from '@/pages/admin/LogsTab'
import MatchesTab from '@/pages/admin/MatchesTab'
import UsersTab from '@/pages/admin/UsersTab'

type TabKey = 'dashboard' | 'users' | 'bots' | 'matches' | 'contests' | 'communications' | 'logs'

interface AdminTab {
  key: TabKey
  label: string
  description: string
  icon: LucideIcon
}

const TABS: AdminTab[] = [
  { key: 'dashboard', label: '仪表盘', description: '平台概览与运行指标', icon: LayoutDashboard },
  { key: 'users', label: '用户', description: '账号、权限与实名状态', icon: Users },
  { key: 'bots', label: 'Bot', description: '版本、上架与运行能力', icon: Bot },
  { key: 'matches', label: '对局', description: '对局记录与异常处置', icon: Swords },
  { key: 'contests', label: '锦标赛', description: '赛事生命周期与参赛者', icon: Medal },
  { key: 'communications', label: '通信中心', description: '收发信、群发与问题反馈', icon: Inbox },
  { key: 'logs', label: '日志', description: '运行日志与故障排查', icon: ScrollText },
]

function resolveTab(value: string | null): TabKey {
  // 旧 ?tab=email 链接继续落到新通信中心，不让收藏失效。
  if (value === 'email') return 'communications'
  return TABS.some((item) => item.key === value) ? value as TabKey : 'dashboard'
}

export default function Admin() {
  const { user, isLoggedIn } = useAuth()
  const [searchParams, setSearchParams] = useSearchParams()
  const tab = resolveTab(searchParams.get('tab'))
  const active = TABS.find((item) => item.key === tab) ?? TABS[0]

  const selectTab = (next: TabKey) => {
    const params = new URLSearchParams(searchParams)
    if (next === 'dashboard') params.delete('tab')
    else params.set('tab', next)
    setSearchParams(params, { replace: false })
  }

  if (!isLoggedIn) {
    return (
      <PageFrame layout="admin-auth-required">
        <PageHeader title="管理控制台" description="请先使用管理员账号登录。" />
        <section className="mx-auto w-full max-w-5xl rounded-xl border bg-card">
          <EmptyState text="当前未登录" className="py-12" />
          <div className="border-t p-3 text-center">
            <Button asChild size="sm"><Link to="/login">前往登录</Link></Button>
          </div>
        </section>
      </PageFrame>
    )
  }

  if (user?.role !== 'admin') {
    return (
      <PageFrame layout="admin-forbidden">
        <PageHeader title="管理控制台" description="该区域只向平台管理员开放。" />
        <section className="mx-auto w-full max-w-5xl rounded-xl border bg-card">
          <EmptyState text="当前账号没有管理权限" icon={<ShieldCheck className="size-7 opacity-40" />} className="py-12" />
        </section>
      </PageFrame>
    )
  }

  return (
    <PageFrame width="full" layout="admin-console" className="gap-3">
      <PageHeader
        eyebrow="平台运维"
        title="管理控制台"
        description={`${active.label}：${active.description}`}
        actions={<Button asChild variant="outline" size="sm" className="max-lg:min-h-11"><Link to="/feedback">查看用户视角</Link></Button>}
      />

      <div className="grid min-w-0 gap-3 lg:grid-cols-[12.5rem_minmax(0,1fr)]">
        <div className="lg:hidden">
          <Select value={tab} onValueChange={(value) => selectTab(value as TabKey)}>
            <SelectTrigger aria-label="选择管理模块" className="min-h-11 w-full bg-card">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {TABS.map((item) => (
                <SelectItem key={item.key} value={item.key} className="min-h-11">{item.label}</SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        <aside className="sticky top-[var(--sticky-page-offset)] hidden self-start rounded-xl border bg-card p-2 lg:block">
          <nav aria-label="管理控制台模块" className="space-y-1">
            {TABS.map((item) => {
              const Icon = item.icon
              const selected = tab === item.key
              return (
                <button
                  key={item.key}
                  type="button"
                  aria-current={selected ? 'page' : undefined}
                  onClick={() => selectTab(item.key)}
                  className={cn(
                    'flex min-h-11 w-full cursor-pointer items-start gap-2 rounded-lg px-2.5 py-2 text-left transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
                    selected ? 'bg-primary/10 text-primary' : 'text-muted-foreground hover:bg-accent hover:text-foreground',
                  )}
                >
                  <Icon aria-hidden="true" className="mt-0.5 size-4 shrink-0" />
                  <span className="min-w-0">
                    <span className="block text-sm font-medium leading-5">{item.label}</span>
                    <span className="mt-0.5 block text-[0.6875rem] leading-4 opacity-80">{item.description}</span>
                  </span>
                </button>
              )
            })}
          </nav>
        </aside>

        <main
          id="admin-content"
          className="min-w-0 max-lg:[&_[data-slot=button]]:min-h-11 max-lg:[&_[data-slot=button]]:min-w-11 max-lg:[&_[data-slot=input]]:min-h-11 max-lg:[&_[data-slot=select-trigger]]:min-h-11 max-lg:[&_textarea]:min-h-11"
          aria-label={active.label}
        >
          {tab === 'dashboard' && <Dashboard />}
          {tab === 'users' && <UsersTab />}
          {tab === 'bots' && <BotsTab />}
          {tab === 'matches' && <MatchesTab />}
          {tab === 'contests' && <ContestsTab />}
          {tab === 'communications' && <CommunicationsTab />}
          {tab === 'logs' && <LogsTab />}
        </main>
      </div>
    </PageFrame>
  )
}
