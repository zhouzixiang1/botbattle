import { useState, lazy, Suspense } from 'react'
import { Routes, Route, NavLink, Link, useNavigate } from 'react-router-dom'
import { Menu, LogOut, User as UserIcon, Loader2 } from 'lucide-react'
import { useAuth } from '@/components/useAuth'
import NotificationBell from '@/components/NotificationBell'
import { ThemeToggle } from '@/components/theme-toggle'
import { GlobalSearch } from '@/components/shell/global-search'
import { NAV_ITEMS, ADMIN_NAV, type NavItem } from '@/components/shell/nav-config'
import { Button } from '@/components/ui/button'
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from '@/components/ui/sheet'
import { cn } from '@/lib/utils'

// 页面懒加载（代码分割：每个页面独立 chunk，recharts 等大依赖只在访问时加载）
const Home = lazy(() => import('@/pages/Home'))
const Challenge = lazy(() => import('@/pages/Challenge'))
const Leaderboard = lazy(() => import('@/pages/Leaderboard'))
const Contests = lazy(() => import('@/pages/Contests'))
const ContestDetail = lazy(() => import('@/pages/ContestDetail'))
const MyBots = lazy(() => import('@/pages/MyBots'))
const Wiki = lazy(() => import('@/pages/Wiki'))
const DataDownload = lazy(() => import('@/pages/DataDownload'))
const ArenaWatch = lazy(() => import('@/pages/ArenaWatch'))
const History = lazy(() => import('@/pages/History'))
const MatchDetail = lazy(() => import('@/pages/MatchDetail'))
const BotDetail = lazy(() => import('@/pages/BotDetail'))
const HumanPlay = lazy(() => import('@/pages/HumanPlay'))
const UserProfile = lazy(() => import('@/pages/UserProfile'))
const SearchPage = lazy(() => import('@/pages/Search'))
const Notifications = lazy(() => import('@/pages/Notifications'))
const Settings = lazy(() => import('@/pages/Settings'))
const Login = lazy(() => import('@/pages/Login'))
const Register = lazy(() => import('@/pages/Register'))
const VerifyEmail = lazy(() => import('@/pages/VerifyEmail'))
const ResetPassword = lazy(() => import('@/pages/ResetPassword'))
const Admin = lazy(() => import('@/pages/admin/Admin'))

/** 懒加载 fallback（旋转加载图标，居中） */
function PageFallback() {
  return (
    <div className="flex min-h-[50vh] items-center justify-center">
      <Loader2 className="size-6 animate-spin text-muted-foreground" />
    </div>
  )
}

const navCls = ({ isActive }: { isActive: boolean }) =>
  cn(
    'inline-flex items-center gap-1.5 rounded-lg px-3 py-2 text-sm font-medium transition-colors',
    isActive
      ? 'bg-primary/10 text-primary'
      : 'text-muted-foreground hover:bg-accent hover:text-accent-foreground'
  )

function NavLinks({ onNavigate }: { onNavigate?: () => void }) {
  const { user } = useAuth()
  const items = [...NAV_ITEMS]
  if (user?.role === 'admin' || user?.role === 'organizer') items.push(ADMIN_NAV)
  return (
    <nav className="flex flex-col gap-1">
      {items.map((item) => (
        <NavLink key={item.to} to={item.to} end={item.end} className={navCls} onClick={onNavigate}>
          <item.icon className="size-4" />
          {item.label}
        </NavLink>
      ))}
    </nav>
  )
}

export function AppShell() {
  const { user, isLoggedIn, loading, logout } = useAuth()
  const nav = useNavigate()
  const [mobileOpen, setMobileOpen] = useState(false)

  const onLogout = async () => {
    await logout()
    nav('/')
  }

  return (
    <div className="flex min-h-screen flex-col bg-background font-sans text-foreground">
      {/* 顶栏 */}
      <header className="sticky top-0 z-50 border-b border-border bg-background/85 backdrop-blur-md">
        <div className="mx-auto flex h-14 w-full max-w-screen-2xl items-center gap-2 px-4 lg:px-6">
          {/* 移动端汉堡 */}
          <Sheet open={mobileOpen} onOpenChange={setMobileOpen}>
            <SheetTrigger asChild>
              <Button variant="ghost" size="icon" className="md:hidden" aria-label="菜单">
                <Menu className="size-5" />
              </Button>
            </SheetTrigger>
            <SheetContent side="left" className="w-72 p-4">
              <SheetHeader>
                <SheetTitle className="flex items-center gap-2 text-left">
                  <span className="flex size-8 items-center justify-center rounded-lg bg-primary text-sm font-bold text-primary-foreground">
                    B
                  </span>
                  <span className="font-display">Botbattle</span>
                </SheetTitle>
              </SheetHeader>
              <div className="mt-4 px-1">
                <NavLinks onNavigate={() => setMobileOpen(false)} />
              </div>
            </SheetContent>
          </Sheet>

          {/* Logo */}
          <Link
            to="/"
            className="flex shrink-0 items-center gap-2 font-semibold tracking-tight text-foreground"
          >
            <span className="flex size-8 items-center justify-center rounded-lg bg-primary text-sm font-bold text-primary-foreground shadow-soft">
              B
            </span>
            <span className="hidden font-display text-lg sm:inline">Botbattle</span>
          </Link>

          {/* 桌面端主导航 */}
          <nav className="ml-2 hidden items-center gap-0.5 md:flex">
            <DesktopNav />
          </nav>

          {/* 搜索（中间填充） */}
          <div className="ml-auto flex items-center gap-1.5">
            <GlobalSearch />
            <ThemeToggle />

            {loading ? (
              <span className="text-xs text-muted-foreground">…</span>
            ) : isLoggedIn && user ? (
              <div className="flex items-center gap-1.5">
                <NotificationBell />
                <Link
                  to={`/user/${encodeURIComponent(user.username)}`}
                  className="inline-flex items-center gap-1.5 rounded-lg px-2.5 py-2 text-sm text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground"
                  title={user.username}
                >
                  <UserIcon className="size-4 text-primary" />
                  <span className="hidden max-w-[8rem] truncate font-medium text-primary sm:inline">
                    {user.display_name || user.username}
                  </span>
                  {user.role === 'admin' && (
                    <span className="rounded bg-destructive/10 px-1.5 py-0.5 text-[10px] font-medium text-destructive">
                      admin
                    </span>
                  )}
                </Link>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  onClick={() => void onLogout()}
                  className="gap-1.5 text-muted-foreground"
                >
                  <LogOut className="size-3.5" />
                  <span className="hidden sm:inline">登出</span>
                </Button>
              </div>
            ) : (
              <div className="flex items-center gap-1">
                <NavLink to="/login" className={navCls}>
                  登录
                </NavLink>
                <Button asChild size="sm" className="shadow-soft">
                  <Link to="/register">注册</Link>
                </Button>
              </div>
            )}
          </div>
        </div>
      </header>

      {/* 主体 */}
      <main className="mx-auto w-full max-w-screen-2xl flex-1 px-4 py-6 lg:px-6">
        <Suspense fallback={<PageFallback />}>
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/arena" element={<ArenaWatch />} />
          <Route path="/watch/:id" element={<ArenaWatch />} />
          <Route path="/challenge" element={<Challenge />} />
          <Route path="/leaderboard" element={<Leaderboard />} />
          <Route path="/history" element={<History />} />
          <Route path="/match/:id" element={<MatchDetail />} />
          <Route path="/bot/:id" element={<BotDetail />} />
          <Route path="/play/:id" element={<HumanPlay />} />
          <Route path="/my-bots" element={<MyBots />} />
          <Route path="/wiki" element={<Wiki />} />
          <Route path="/data" element={<DataDownload />} />
          <Route path="/contests" element={<Contests />} />
          <Route path="/contests/:id" element={<ContestDetail />} />
          <Route path="/user/:name" element={<UserProfile />} />
          <Route path="/search" element={<SearchPage />} />
          <Route path="/notifications" element={<Notifications />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="/login" element={<Login />} />
          <Route path="/register" element={<Register />} />
          <Route path="/verify-email" element={<VerifyEmail />} />
          <Route path="/reset-password" element={<ResetPassword />} />
          <Route path="/admin" element={<Admin />} />
        </Routes>
        </Suspense>
      </main>

      {/* 页脚 */}
      <footer className="border-t border-border">
        <div className="mx-auto flex w-full max-w-screen-2xl flex-col items-center justify-between gap-2 px-4 py-5 text-xs text-muted-foreground sm:flex-row lg:px-6">
          <span>Botbattle · 多游戏 Bot 竞赛平台（德州 / 五子棋 / 点格棋）</span>
          <span className="opacity-70">React 19 · Tailwind v4 · shadcn/ui</span>
        </div>
      </footer>
    </div>
  )
}

/** 桌面端导航（带图标，主项） */
function DesktopNav() {
  const { user } = useAuth()
  const items = [...NAV_ITEMS]
  if (user?.role === 'admin' || user?.role === 'organizer') items.push(ADMIN_NAV)
  return (
    <>
      {items.map((item) => (
        <NavLink key={item.to} to={item.to} end={item.end} className={navCls}>
          <item.icon className="size-4" />
          <span className="hidden lg:inline">{item.label}</span>
        </NavLink>
      ))}
    </>
  )
}

export type { NavItem }
