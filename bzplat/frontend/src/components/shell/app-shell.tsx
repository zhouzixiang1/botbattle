import { useState, lazy, Suspense } from 'react'
import { useLocation, Routes, Route, NavLink, Link, useNavigate } from 'react-router-dom'
import { Menu, LogOut, User as UserIcon, Loader2 } from 'lucide-react'
import { useAuth } from '@/components/useAuth'
import NotificationBell from '@/components/NotificationBell'
import { ThemeToggle } from '@/components/theme-toggle'
import { GlobalSearch } from '@/components/shell/global-search'
import BrandMark from '@/components/BrandMark'
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
    'inline-flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm font-medium transition-colors',
    isActive
      ? 'bg-primary/10 text-primary'
      : 'text-muted-foreground hover:bg-accent hover:text-accent-foreground'
  )

/** 顶栏水平导航（访客桌面）：更紧凑，无图标以省宽度 */
const topNavCls = ({ isActive }: { isActive: boolean }) =>
  cn(
    'inline-flex items-center rounded-lg px-2.5 py-1.5 text-sm font-medium transition-colors',
    isActive
      ? 'bg-primary/10 text-primary'
      : 'text-muted-foreground hover:bg-accent hover:text-accent-foreground'
  )

/** 未登录态（auth 页）不显示侧边栏，内容占满宽度居中。 */
const AUTH_PATHS = ['/login', '/register', '/verify-email', '/reset-password']

/** 主导航列表（桌面侧栏与移动抽屉共用）。 */
function navItemsFor(user?: { role?: string } | null): NavItem[] {
  const items = [...NAV_ITEMS]
  if (user?.role === 'admin' || user?.role === 'organizer') items.push(ADMIN_NAV)
  return items
}

function NavLinks({
  onNavigate,
  user,
  compact,
}: {
  onNavigate?: () => void
  user?: { role?: string } | null
  /** 顶栏水平布局：不显示图标 */
  compact?: boolean
}) {
  const cls = compact ? topNavCls : navCls
  return (
    <nav className={cn(compact ? 'flex items-center gap-0.5' : 'flex flex-col gap-0.5')}>
      {navItemsFor(user).map((item) => (
        <NavLink key={item.to} to={item.to} end={item.end} className={cls} onClick={onNavigate}>
          {!compact && <item.icon className="size-4 shrink-0" />}
          {item.label}
        </NavLink>
      ))}
    </nav>
  )
}

/** 访客顶栏：登录 + 注册 CTA */
function GuestAuthActions() {
  return (
    <>
      <Button asChild variant="ghost" size="sm" className="text-muted-foreground">
        <Link to="/login">登录</Link>
      </Button>
      <Button asChild size="sm" className="shadow-soft">
        <Link to="/register">注册</Link>
      </Button>
    </>
  )
}

export function AppShell() {
  const { user, isLoggedIn, logout } = useAuth()
  const nav = useNavigate()
  const location = useLocation()
  const [mobileOpen, setMobileOpen] = useState(false)

  const onLogout = async () => {
    await logout()
    nav('/')
  }

  // auth 页不显示桌面侧边栏（未登录态更干净，内容占满居中）
  const isAuthPage = AUTH_PATHS.includes(location.pathname)
  // 已登录且非 auth：桌面侧栏；访客 / auth 页：全断点顶栏（含登录注册入口）
  const showSidebar = isLoggedIn && !isAuthPage

  return (
    <div className="flex min-h-screen flex-col bg-background font-sans text-foreground lg:flex-row">
      {/* 桌面侧边栏（lg 及以上，已登录非 auth 页） */}
      {showSidebar && (
        <aside className="sticky top-0 hidden h-screen w-64 shrink-0 flex-col border-r border-border bg-card lg:flex">
          {/* Logo + 搜索 */}
          <div className="flex flex-col gap-3 border-b border-border p-4">
            <Link to="/" className="flex items-center">
              <BrandMark size="md" />
            </Link>
            <GlobalSearch compact />
          </div>

          {/* 导航（flex-1 占满中部，可滚动） */}
          <div className="no-scrollbar flex-1 overflow-y-auto p-3">
            <NavLinks user={user} />
          </div>

          {/* 底部用户区 + 工具 */}
          <div className="flex flex-col gap-2 border-t border-border p-3">
            <div className="flex items-center justify-end gap-1">
              <ThemeToggle />
              <NotificationBell />
            </div>
            <div className="flex items-center gap-2 rounded-lg px-1 py-1">
              <UserIcon className="size-4 shrink-0 text-primary" />
              <Link
                to={`/user/${encodeURIComponent(user?.username ?? '')}`}
                className="min-w-0 flex-1 truncate text-sm font-medium text-foreground hover:text-primary"
                title={user?.username}
              >
                {user?.display_name || user?.username}
              </Link>
              {user?.role === 'admin' && (
                <span className="rounded bg-destructive/10 px-1.5 py-0.5 text-[10px] font-medium text-destructive">
                  admin
                </span>
              )}
              <Button
                type="button"
                variant="ghost"
                size="icon"
                onClick={() => void onLogout()}
                className="size-8 shrink-0 text-muted-foreground"
                aria-label="登出"
              >
                <LogOut className="size-4" />
              </Button>
            </div>
          </div>
        </aside>
      )}

      {/* 右侧主体（侧栏旁，或全宽） */}
      <div className="flex min-w-0 flex-1 flex-col">
        {/*
          顶栏策略：
          - 已登录 + 非 auth：仅 <lg 显示（lg+ 用侧栏）
          - 访客 / auth 页：全断点显示，保证桌面也能登录/注册与公开导航
        */}
        <header
          className={cn(
            'sticky top-0 z-50 border-b border-border bg-background/85 backdrop-blur-md',
            showSidebar && 'lg:hidden'
          )}
        >
          <div className="flex h-14 w-full items-center gap-2 px-4 lg:px-8">
            {/* 左侧：汉堡（需导航时）+ 品牌 */}
            {showSidebar ? (
              <Sheet open={mobileOpen} onOpenChange={setMobileOpen}>
                <SheetTrigger asChild>
                  <Button variant="ghost" size="icon" aria-label="菜单">
                    <Menu className="size-5" />
                  </Button>
                </SheetTrigger>
                <SheetContent side="left" className="w-72 p-4">
                  <SheetHeader>
                    <SheetTitle className="text-left">
                      <BrandMark />
                    </SheetTitle>
                  </SheetHeader>
                  <div className="mt-4 px-1">
                    <NavLinks onNavigate={() => setMobileOpen(false)} user={user} />
                  </div>
                </SheetContent>
              </Sheet>
            ) : (
              <>
                {/* 访客窄屏：公开导航抽屉 */}
                {!isAuthPage && (
                  <Sheet open={mobileOpen} onOpenChange={setMobileOpen}>
                    <SheetTrigger asChild>
                      <Button variant="ghost" size="icon" className="md:hidden" aria-label="菜单">
                        <Menu className="size-5" />
                      </Button>
                    </SheetTrigger>
                    <SheetContent side="left" className="w-72 p-4">
                      <SheetHeader>
                        <SheetTitle className="text-left">
                          <BrandMark />
                        </SheetTitle>
                      </SheetHeader>
                      <div className="mt-4 px-1">
                        <NavLinks onNavigate={() => setMobileOpen(false)} />
                      </div>
                      {!isLoggedIn && (
                        <div className="mt-6 flex flex-col gap-2 px-1">
                          <Button asChild variant="outline" className="w-full">
                            <Link to="/login" onClick={() => setMobileOpen(false)}>
                              登录
                            </Link>
                          </Button>
                          <Button asChild className="w-full shadow-soft">
                            <Link to="/register" onClick={() => setMobileOpen(false)}>
                              注册
                            </Link>
                          </Button>
                        </div>
                      )}
                    </SheetContent>
                  </Sheet>
                )}
                <Link to="/" className="flex items-center">
                  <BrandMark />
                </Link>
                {/* 访客桌面：水平公开导航 */}
                {!isAuthPage && (
                  <div className="ml-2 hidden min-w-0 flex-1 md:block">
                    <NavLinks compact />
                  </div>
                )}
              </>
            )}

            {/* 右侧操作 */}
            {showSidebar ? (
              <div className="ml-auto flex items-center gap-1">
                <GlobalSearch />
                <ThemeToggle />
                <NotificationBell />
                <Link
                  to={`/user/${encodeURIComponent(user?.username ?? '')}`}
                  className="inline-flex size-9 items-center justify-center rounded-lg text-primary transition-colors hover:bg-accent"
                  title={user?.username}
                >
                  <UserIcon className="size-4" />
                </Link>
              </div>
            ) : (
              <div className={cn('flex items-center gap-1', isAuthPage ? 'ml-auto' : 'ml-auto md:ml-2')}>
                {!isAuthPage && (
                  <div className="hidden sm:block">
                    <GlobalSearch />
                  </div>
                )}
                <ThemeToggle />
                {!isLoggedIn && <GuestAuthActions />}
              </div>
            )}
          </div>
        </header>

        {/* 主体内容 */}
        <main className="mx-auto w-full flex-1 px-4 py-6 lg:px-8">
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

        {/* 页脚（跟随主体宽度，不跨侧栏） */}
        <footer className="border-t border-border">
          <div className="flex w-full flex-col items-center justify-between gap-2 px-4 py-5 text-xs text-muted-foreground sm:flex-row lg:px-8">
            <span>Botbattle · 多游戏 Bot 竞赛平台（德州 / 五子棋 / 点格棋）</span>
            <span className="opacity-70">React 19 · Tailwind v4 · shadcn/ui</span>
          </div>
        </footer>
      </div>
    </div>
  )
}

export type { NavItem }
