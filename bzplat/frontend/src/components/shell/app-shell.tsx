import { useState, lazy, Suspense } from 'react'
import { useLocation, Routes, Route, NavLink, Link, useNavigate, Navigate, useParams } from 'react-router-dom'
import { Menu, LogOut, User as UserIcon, Loader2, PanelLeftClose, PanelLeft } from 'lucide-react'
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
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
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
const MatchViewer = lazy(() => import('@/pages/MatchViewer'))
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

/** /watch/:id → /match/:id 重定向（统一对局页）。 */
function WatchRedirect() {
  const { id } = useParams<{ id: string }>()
  return <Navigate to={`/match/${id ?? ''}`} replace />
}

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

/** auth 页不显示侧边栏（登录/注册/验证/重置——内容占满居中）。 */
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
}: {
  onNavigate?: () => void
  user?: { role?: string } | null
}) {
  return (
    <nav className="flex flex-col gap-0.5">
      {navItemsFor(user).map((item) => (
        <NavLink key={item.to} to={item.to} end={item.end} className={navCls} onClick={onNavigate}>
          <item.icon className="size-4 shrink-0" />
          {item.label}
        </NavLink>
      ))}
    </nav>
  )
}

export function AppShell() {
  const { user, isLoggedIn, logout } = useAuth()
  const nav = useNavigate()
  const location = useLocation()
  const [mobileOpen, setMobileOpen] = useState(false)
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false)

  const onLogout = async () => {
    await logout()
    nav('/')
  }

  // auth 页不显示侧边栏（干净居中）；其他页面统一显示侧边栏（含未登录）
  const isAuthPage = AUTH_PATHS.includes(location.pathname)
  const showSidebar = !isAuthPage

  return (
    <div className="flex min-h-screen bg-background font-sans text-foreground lg:flex-row">
      {/* 桌面侧边栏（lg+，统一显示——登录/未登录都用侧边栏；auth 页不显示） */}
      {showSidebar && (
        <aside
          className={cn(
            'sticky top-0 hidden h-screen shrink-0 flex-col border-r border-border bg-card transition-all duration-200 lg:flex',
            sidebarCollapsed ? 'w-14' : 'w-64',
          )}
        >
          {/* Logo + 搜索（折叠时只显示 Logo 图标） */}
          <div className="flex flex-col gap-3 border-b border-border p-3">
            <div className="flex items-center justify-between">
              <Link to="/" className="flex items-center">
                <BrandMark size={sidebarCollapsed ? 'sm' : 'md'} />
              </Link>
              <Button
                variant="ghost"
                size="icon"
                className="size-7 shrink-0 text-muted-foreground"
                onClick={() => setSidebarCollapsed((v) => !v)}
                aria-label={sidebarCollapsed ? '展开侧边栏' : '收起侧边栏'}
              >
                {sidebarCollapsed ? <PanelLeft className="size-4" /> : <PanelLeftClose className="size-4" />}
              </Button>
            </div>
            {!sidebarCollapsed && <GlobalSearch compact />}
          </div>

          {/* 导航（flex-1 占满中部，可滚动） */}
          <div className="no-scrollbar flex-1 overflow-y-auto p-2">
            {sidebarCollapsed ? (
              <nav className="flex flex-col items-center gap-1">
                {navItemsFor(user).map((item) => (
                  <Tooltip key={item.to}>
                    <TooltipTrigger asChild>
                      <NavLink
                        to={item.to}
                        end={item.end}
                        className="inline-flex size-9 items-center justify-center rounded-lg text-muted-foreground transition-colors hover:bg-accent [&.active]:bg-primary/10 [&.active]:text-primary"
                      >
                        <item.icon className="size-4" />
                      </NavLink>
                    </TooltipTrigger>
                    <TooltipContent side="right">{item.label}</TooltipContent>
                  </Tooltip>
                ))}
              </nav>
            ) : (
              <NavLinks user={user} />
            )}
          </div>

          {/* 底部用户区/工具（登录态=用户面板+登出；未登录=登录/注册按钮） */}
          <div className="flex flex-col gap-2 border-t border-border p-2">
            {!sidebarCollapsed && (
              <div className="flex items-center justify-end gap-1">
                <ThemeToggle />
                {isLoggedIn && <NotificationBell />}
              </div>
            )}
            {isLoggedIn ? (
              <div className={cn('flex items-center gap-2 rounded-lg px-1 py-1', sidebarCollapsed && 'justify-center')}>
                <UserIcon className="size-4 shrink-0 text-primary" />
                {!sidebarCollapsed && (
                  <>
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <Link
                          to={`/user/${encodeURIComponent(user?.username ?? '')}`}
                          className="min-w-0 flex-1 truncate text-sm font-medium text-foreground hover:text-primary"
                        >
                          {user?.display_name || user?.username}
                        </Link>
                      </TooltipTrigger>
                      <TooltipContent>{user?.username}</TooltipContent>
                    </Tooltip>
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
                  </>
                )}
              </div>
            ) : (
              !sidebarCollapsed && (
                <div className="flex flex-col gap-2">
                  <Button asChild variant="outline" size="sm" className="w-full">
                    <Link to="/login">登录</Link>
                  </Button>
                  <Button asChild size="sm" className="w-full shadow-soft">
                    <Link to="/register">注册</Link>
                  </Button>
                </div>
              )
            )}
          </div>
        </aside>
      )}

      {/* 右侧主体（侧栏旁，或全宽） */}
      <div className="flex min-w-0 flex-1 flex-col">
        {/*
          顶栏策略：
          - 非 auth 页：仅 <lg 显示（lg+ 用侧栏）
          - auth 页：全断点显示（仅品牌+主题）
        */}
        <header
          className={cn(
            'sticky top-0 z-50 border-b border-border bg-background/85 backdrop-blur-md',
            showSidebar && 'lg:hidden'
          )}
        >
          <div className="flex h-14 w-full items-center gap-2 px-4 lg:px-8">
            {/* 移动端汉堡（<lg）+ 品牌 */}
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
            ) : null}
            <Link to="/" className="flex items-center">
              <BrandMark />
            </Link>

            {/* 右侧操作 */}
            <div className={cn('flex items-center gap-1', isAuthPage ? 'ml-auto' : 'ml-auto')}>
              {!isAuthPage && (
                <div className="hidden sm:block">
                  <GlobalSearch />
                </div>
              )}
              <ThemeToggle />
              {isAuthPage && !isLoggedIn && (
                <Button asChild variant="ghost" size="sm" className="text-muted-foreground">
                  <Link to="/login">登录</Link>
                </Button>
              )}
            </div>
          </div>
        </header>

        {/* 主体内容 */}
        <main className="mx-auto w-full flex-1 px-4 py-6 lg:px-8">
          <Suspense fallback={<PageFallback />}>
            <Routes>
              <Route path="/" element={<Home />} />
              <Route path="/arena" element={<ArenaWatch />} />
              <Route path="/watch/:id" element={<WatchRedirect />} />
              <Route path="/challenge" element={<Challenge />} />
              <Route path="/leaderboard" element={<Leaderboard />} />
              <Route path="/history" element={<History />} />
              <Route path="/match/:id" element={<MatchViewer />} />
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
