import { useState, lazy, Suspense } from 'react'
import { useLocation, Routes, Route, NavLink, Link, useNavigate, Navigate } from 'react-router-dom'
import { CircleUserRound, Menu, LogOut, User as UserIcon, Loader2, Mail, PanelLeftClose, PanelLeft, Settings2 } from 'lucide-react'
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
import { EntityName } from '@/components/ui/overflow-text'
import { useScrollRestoration } from '@/hooks/use-scroll-restoration'
import { cn } from '@/lib/utils'

// 页面懒加载（代码分割：每个页面独立 chunk，recharts 等大依赖只在访问时加载）
const Home = lazy(() => import('@/pages/Home'))
const Challenge = lazy(() => import('@/pages/Challenge'))
const Leaderboard = lazy(() => import('@/pages/Leaderboard'))
const Contests = lazy(() => import('@/pages/Contests'))
const ContestDetail = lazy(() => import('@/pages/ContestDetail'))
const MyBots = lazy(() => import('@/pages/MyBots'))
const Wiki = lazy(() => import('@/pages/Wiki'))
const Judges = lazy(() => import('@/pages/Judges'))
const History = lazy(() => import('@/pages/History'))
const MatchViewer = lazy(() => import('@/pages/MatchViewer'))
const BotDetail = lazy(() => import('@/pages/BotDetail'))
const HumanPlay = lazy(() => import('@/pages/HumanPlay'))
const UserProfile = lazy(() => import('@/pages/UserProfile'))
const SearchPage = lazy(() => import('@/pages/Search'))
const Notifications = lazy(() => import('@/pages/Notifications'))
const Messages = lazy(() => import('@/pages/Messages'))
const Feedback = lazy(() => import('@/pages/Feedback'))
const Settings = lazy(() => import('@/pages/Settings'))
const Login = lazy(() => import('@/pages/Login'))
const Register = lazy(() => import('@/pages/Register'))
const VerifyEmail = lazy(() => import('@/pages/VerifyEmail'))
const ResetPassword = lazy(() => import('@/pages/ResetPassword'))
const Admin = lazy(() => import('@/pages/admin/Admin'))

/** 懒加载 fallback（旋转加载图标，居中） */
function PageFallback() {
  return (
    <div className="flex min-h-[50vh] items-center justify-center" role="status" aria-label="页面加载中">
      <Loader2 aria-hidden="true" className="size-6 animate-spin text-muted-foreground" />
    </div>
  )
}

const navCls = ({ isActive }: { isActive: boolean }) =>
  cn(
    'inline-flex min-h-[var(--control-height)] min-w-0 items-center gap-2.5 rounded-lg px-3 py-1.5 text-sm font-medium transition-colors',
    isActive
      ? 'bg-primary/10 text-primary'
      : 'text-muted-foreground hover:bg-accent hover:text-accent-foreground'
  )

/** auth 页不显示侧边栏（登录/注册/验证/重置——内容占满居中）。 */
const AUTH_PATHS = ['/login', '/register', '/verify-email', '/reset-password']

/** 主导航列表（桌面侧栏与移动抽屉共用）。 */
function navItemsFor(user?: { role?: string } | null): NavItem[] {
  const items = [...NAV_ITEMS]
  if (user?.role === 'admin') items.push(ADMIN_NAV)
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
    <nav aria-label="主导航" className="flex min-w-0 flex-col gap-0.5">
      {navItemsFor(user).map((item) => (
        <NavLink key={item.to} to={item.to} end={item.end} className={navCls} onClick={onNavigate}>
          <item.icon aria-hidden="true" className="size-4 shrink-0" />
          <span className="truncate">{item.label}</span>
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
  useScrollRestoration()

  const onLogout = async () => {
    setMobileOpen(false)
    await logout()
    nav('/')
  }

  // auth 页不显示侧边栏（干净居中）；其他页面统一显示侧边栏（含未登录）
  const isAuthPage = AUTH_PATHS.includes(location.pathname)
  const showSidebar = !isAuthPage

  return (
    <div
      data-app-shell
      data-sidebar-visible={showSidebar ? 'true' : 'false'}
      className="flex min-h-dvh min-w-0 bg-background font-sans text-foreground"
    >
      <button
        type="button"
        onClick={() => {
          const main = document.getElementById('main-content')
          main?.focus()
          main?.scrollIntoView({ block: 'start' })
        }}
        className="fixed top-2 left-2 z-[var(--z-modal)] -translate-y-16 rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground shadow-lg transition-transform focus:translate-y-0"
      >
        跳至主内容
      </button>
      {/* 桌面侧边栏（xl+，统一显示——登录/未登录都用侧边栏；auth 页不显示） */}
      {showSidebar && (
        <aside
          aria-label="站点导航"
          data-sidebar-collapsed={sidebarCollapsed ? 'true' : 'false'}
          className={cn(
            'sticky top-0 hidden h-dvh shrink-0 flex-col overflow-x-clip border-r border-border bg-card xl:flex',
            sidebarCollapsed
              ? 'w-[var(--shell-sidebar-collapsed-width)]'
              : 'w-[var(--shell-sidebar-width)]',
          )}
        >
          {/* Logo + 搜索（折叠时只显示 Logo 图标） */}
          <div
            className={cn(
              'flex flex-col border-b border-border',
              sidebarCollapsed ? 'gap-2 p-2' : 'gap-3 p-3',
            )}
          >
            <div className={cn('flex items-center justify-between', sidebarCollapsed && 'flex-col gap-2')}>
              <Link
                to="/"
                aria-label={sidebarCollapsed ? 'Botbattle 首页' : undefined}
                className="flex min-w-0 max-w-full items-center overflow-hidden"
              >
                <BrandMark
                  className="min-w-0 max-w-full overflow-hidden"
                  size={sidebarCollapsed ? 'sm' : 'md'}
                  withText={!sidebarCollapsed}
                />
              </Link>
              <Button
                variant="ghost"
                size="icon"
                className="shrink-0 text-muted-foreground"
                onClick={() => setSidebarCollapsed((v) => !v)}
                aria-label={sidebarCollapsed ? '展开侧边栏' : '收起侧边栏'}
              >
                {sidebarCollapsed ? <PanelLeft aria-hidden="true" className="size-4" /> : <PanelLeftClose aria-hidden="true" className="size-4" />}
              </Button>
            </div>
            {!sidebarCollapsed && <GlobalSearch compact />}
          </div>

          {/* 导航（flex-1 占满中部，可滚动） */}
          <div
            data-scroll-region="sidebar-navigation"
            data-overflow-allowed="y"
            className="no-scrollbar min-h-0 min-w-0 flex-1 overflow-y-auto p-2"
          >
            {sidebarCollapsed ? (
              <nav aria-label="主导航" className="flex min-w-0 flex-col items-center gap-1">
                {navItemsFor(user).map((item) => (
                  <Tooltip key={item.to}>
                    <TooltipTrigger asChild>
                      <NavLink
                        to={item.to}
                        end={item.end}
                        aria-label={item.label}
                        className={({ isActive }) =>
                          cn(
                            'inline-flex size-[var(--control-height)] items-center justify-center rounded-lg transition-colors',
                            isActive
                              ? 'bg-primary/10 text-primary'
                              : 'text-muted-foreground hover:bg-accent hover:text-accent-foreground',
                          )
                        }
                      >
                        <item.icon aria-hidden="true" className="size-4" />
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
                {isLoggedIn && (
                  <Button asChild variant="ghost" size="icon" aria-label="站内信">
                    <Link to="/messages"><Mail aria-hidden="true" className="size-4" /></Link>
                  </Button>
                )}
                {isLoggedIn && <NotificationBell />}
              </div>
            )}
            {isLoggedIn ? (
              <div
                className={cn(
                  'flex min-w-0 items-center gap-2 rounded-lg px-1 py-1',
                  sidebarCollapsed && 'justify-center',
                )}
              >
                <UserIcon aria-hidden="true" className="size-4 shrink-0 text-primary" />
                {!sidebarCollapsed && (
                  <>
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <Link
                          to={`/user/${encodeURIComponent(user?.username ?? '')}`}
                          className="min-w-0 max-w-[7rem] flex-1 text-sm hover:text-primary"
                        >
                          <EntityName
                            className="text-sm hover:text-primary"
                            tooltip={false}
                            tooltipFocusable={false}
                          >
                            {user?.display_name || user?.username}
                          </EntityName>
                        </Link>
                      </TooltipTrigger>
                      <TooltipContent className="max-w-xs break-all">
                        {user?.display_name || user?.username}
                        {user?.username ? ` (@${user.username})` : ''}
                      </TooltipContent>
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
                      className="shrink-0 text-muted-foreground"
                      aria-label="登出"
                    >
                      <LogOut aria-hidden="true" className="size-4" />
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
          - 非 auth 页：仅 <xl 显示（xl+ 用侧栏）
          - auth 页：全断点显示（仅品牌+主题）
        */}
        <header
          data-shell-header
          className={cn(
            'sticky top-0 z-[var(--z-navigation)] border-b border-border bg-background/85 backdrop-blur-md',
            showSidebar && 'xl:hidden'
          )}
        >
          <div className="flex h-[var(--shell-header-height)] w-full min-w-0 items-center gap-2 px-[var(--page-gutter)]">
            {/* 移动端汉堡（<xl）+ 品牌 */}
            {showSidebar ? (
              <Sheet open={mobileOpen} onOpenChange={setMobileOpen}>
                <SheetTrigger asChild>
                  <Button variant="ghost" size="icon" aria-label="菜单">
                    <Menu aria-hidden="true" className="size-5" />
                  </Button>
                </SheetTrigger>
                <SheetContent
                  side="left"
                  data-scroll-region="mobile-navigation"
                  data-overflow-allowed="y"
                  className="w-[min(20rem,90vw)] overflow-y-auto p-4"
                >
                  <SheetHeader className="p-0">
                    <SheetTitle className="text-left">
                      <BrandMark />
                    </SheetTitle>
                  </SheetHeader>
                  <div className="mt-4 px-1">
                    <NavLinks onNavigate={() => setMobileOpen(false)} user={user} />
                  </div>
                  {/* 移动端补齐桌面侧栏的功能入口：搜索 + 通知 + 主题（<xl 时桌面侧栏隐藏，抽屉需对等） */}
                  <div className="mt-4 px-1">
                    <GlobalSearch compact />
                    <div className="mt-2 flex items-center justify-between gap-2">
                      {isLoggedIn && <div className="flex items-center gap-1"><Button asChild variant="ghost" size="icon" aria-label="站内信"><Link to="/messages" onClick={() => setMobileOpen(false)}><Mail className="size-4" /></Link></Button><NotificationBell /></div>}
                      <ThemeToggle />
                    </div>
                  </div>
                  {isLoggedIn && (
                    <div className="mt-5 min-w-0 border-t px-1 pt-4">
                      <div className="flex min-w-0 items-center gap-2 rounded-lg bg-muted/40 p-3">
                        <CircleUserRound aria-hidden="true" className="size-5 shrink-0 text-primary" />
                        <div className="min-w-0 flex-1">
                          <EntityName className="text-sm">
                            {user?.display_name || user?.username}
                          </EntityName>
                          {user?.username && (
                            <div className="truncate text-xs text-muted-foreground">@{user.username}</div>
                          )}
                        </div>
                      </div>
                      <div className="mt-2 grid min-w-0 grid-cols-2 gap-2">
                        <Button asChild variant="outline" size="sm" className="w-full">
                          <Link
                            to={`/user/${encodeURIComponent(user?.username ?? '')}`}
                            onClick={() => setMobileOpen(false)}
                          >
                            <UserIcon aria-hidden="true" className="size-4" />
                            个人主页
                          </Link>
                        </Button>
                        <Button asChild variant="outline" size="sm" className="w-full">
                          <Link to="/settings" onClick={() => setMobileOpen(false)}>
                            <Settings2 aria-hidden="true" className="size-4" />
                            账号设置
                          </Link>
                        </Button>
                      </div>
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        className="mt-2 w-full justify-start text-muted-foreground"
                        onClick={() => void onLogout()}
                      >
                        <LogOut aria-hidden="true" className="size-4" />
                        退出登录
                      </Button>
                    </div>
                  )}
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
            <div className="ml-auto flex min-w-0 items-center gap-1">
              {!isAuthPage && <GlobalSearch hotkey />}
              {!isAuthPage && isLoggedIn && (
                <Button asChild variant="ghost" size="icon" aria-label="账户">
                  <Link to="/settings">
                    <CircleUserRound aria-hidden="true" className="size-[1.15rem]" />
                  </Link>
                </Button>
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
        <main
          id="main-content"
          data-scroll-owner="page"
          tabIndex={-1}
          className="mx-auto w-full min-w-0 flex-1 px-[var(--page-gutter)] py-4 outline-none sm:py-5"
        >
          <Suspense fallback={<PageFallback />}>
            <Routes>
              <Route path="/" element={<Home />} />
              <Route path="/challenge" element={<Challenge />} />
              <Route path="/leaderboard" element={<Leaderboard />} />
              <Route path="/history" element={<History />} />
              <Route path="/match/:id" element={<MatchViewer />} />
              <Route path="/bot/:id" element={<BotDetail />} />
              <Route path="/play/:id" element={<HumanPlay />} />
              <Route path="/my-bots" element={<MyBots />} />
              <Route path="/wiki" element={<Wiki />} />
              <Route path="/judges" element={<Judges />} />
              <Route path="/contests" element={<Contests />} />
              <Route path="/contests/:id" element={<ContestDetail />} />
              <Route path="/user/:name" element={<UserProfile />} />
              <Route path="/search" element={<SearchPage />} />
              <Route path="/notifications" element={<Notifications />} />
              <Route path="/messages" element={<Messages />} />
              <Route path="/messages/:conversationId" element={<Messages />} />
              <Route path="/feedback" element={<Feedback />} />
              <Route path="/settings" element={<Settings />} />
              <Route path="/login" element={<Login />} />
              <Route path="/register" element={<Register />} />
              <Route path="/verify-email" element={<VerifyEmail />} />
              <Route path="/reset-password" element={<ResetPassword />} />
              <Route path="/admin" element={<Admin />} />
              {/* catch-all：未知路由（旧 /arena、/watch、拼写错）重定向首页，不渲染空白页 */}
              <Route path="*" element={<Navigate to="/" replace />} />
            </Routes>
          </Suspense>
        </main>

        {/* 页脚（跟随主体宽度，不跨侧栏） */}
        <footer className="border-t border-border">
          <div className="flex w-full min-w-0 items-center px-[var(--page-gutter)] py-4 text-xs text-muted-foreground">
            <span>Botbattle · 多游戏 Bot 竞赛平台（德州 / 五子棋 / 点格棋）</span>
          </div>
        </footer>
      </div>
    </div>
  )
}

export type { NavItem }
