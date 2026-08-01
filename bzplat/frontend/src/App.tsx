import {
  HashRouter,
  Routes,
  Route,
  NavLink,
  Link,
  useNavigate,
} from 'react-router-dom'
import { AuthProvider, useAuth } from './components/useAuth'
import NotificationBell from './components/NotificationBell'
import { ThemeProvider } from './components/theme-provider'
import { ThemeToggle } from './components/theme-toggle'
import { TooltipProvider } from '@/components/ui/tooltip'
import { Toaster } from '@/components/ui/sonner'
import Admin from './pages/admin/Admin'
import ArenaWatch from './pages/ArenaWatch'
import Challenge from './pages/Challenge'
import ContestDetail from './pages/ContestDetail'
import Contests from './pages/Contests'
import DataDownload from './pages/DataDownload'
import History from './pages/History'
import Home from './pages/Home'
import HumanPlay from './pages/HumanPlay'
import Leaderboard from './pages/Leaderboard'
import Login from './pages/Login'
import MatchDetail from './pages/MatchDetail'
import BotDetail from './pages/BotDetail'
import MyBots from './pages/MyBots'
import Notifications from './pages/Notifications'
import Register from './pages/Register'
import ResetPassword from './pages/ResetPassword'
import Search from './pages/Search'
import Settings from './pages/Settings'
import UserProfile from './pages/UserProfile'
import VerifyEmail from './pages/VerifyEmail'
import Wiki from './pages/Wiki'

export default function App() {
  return (
    <ThemeProvider attribute="class" defaultTheme="light" enableSystem disableTransitionOnChange>
      <TooltipProvider>
        <AuthProvider>
          <HashRouter>
            <Shell />
          </HashRouter>
        </AuthProvider>
        <Toaster richColors position="top-center" />
      </TooltipProvider>
    </ThemeProvider>
  )
}

function Shell() {
  const navCls = ({ isActive }: { isActive: boolean }) =>
    `px-3 py-2 rounded-lg text-sm font-medium transition ${
      isActive
        ? 'bg-primary/10 text-primary'
        : 'text-muted-foreground hover:bg-accent hover:text-accent-foreground'
    }`

  const { user, isLoggedIn, loading, logout } = useAuth()
  const nav = useNavigate()

  const onLogout = async () => {
    await logout()
    nav('/')
  }

  return (
    <div className="min-h-screen bg-background font-sans text-foreground">
      <header className="sticky top-0 z-50 border-b border-border bg-background/85 backdrop-blur-md">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-2 px-4 py-3 lg:px-6">
          <Link
            to="/"
            className="mr-2 flex items-center gap-2 text-lg font-semibold tracking-tight text-foreground"
          >
            <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary text-sm font-bold text-primary-foreground shadow-soft">
              B
            </span>
            <span className="font-display">Botbattle</span>
          </Link>
          <nav className="flex flex-wrap items-center gap-1">
            <NavLink to="/" end className={navCls}>
              首页
            </NavLink>
            <NavLink to="/challenge" className={navCls}>
              挑战
            </NavLink>
            <NavLink to="/leaderboard" className={navCls}>
              排行榜
            </NavLink>
            <NavLink to="/contests" className={navCls}>
              比赛
            </NavLink>
            <NavLink to="/my-bots" className={navCls}>
              我的 Bot
            </NavLink>
            <NavLink to="/wiki" className={navCls}>
              Wiki
            </NavLink>
            <NavLink to="/data" className={navCls}>
              数据
            </NavLink>
            {(user?.role === 'admin' || user?.role === 'organizer') && (
              <NavLink to="/admin" className={navCls}>
                管理
              </NavLink>
            )}
          </nav>
          <form
            action="#/search"
            className="ml-2 hidden md:block"
            onSubmit={(e) => {
              e.preventDefault()
              const fd = new FormData(e.currentTarget)
              const q = String(fd.get('q') || '').trim()
              if (q) location.hash = `#/search?q=${encodeURIComponent(q)}&type=users`
            }}
          >
            <input
              name="q"
              placeholder="搜索…"
              className="w-40 rounded-lg border border-input bg-background px-3 py-1.5 text-sm text-foreground placeholder:text-muted-foreground focus:w-56 focus:outline-none focus:ring-1 focus:ring-ring"
            />
          </form>
          <div className="ml-auto flex items-center gap-2">
            <ThemeToggle />
            {loading ? (
              <span className="text-xs text-muted-foreground">…</span>
            ) : isLoggedIn && user ? (
              <>
                <NotificationBell />
                <Link
                  to={`/user/${encodeURIComponent(user.username)}`}
                  className="rounded-lg px-3 py-2 text-sm text-muted-foreground hover:bg-accent hover:text-accent-foreground"
                >
                  <span className="font-medium text-primary">
                    {user.display_name || user.username}
                  </span>
                  {user.role === 'admin' && (
                    <span className="ml-1 rounded-md bg-destructive/10 px-1.5 py-0.5 text-[10px] font-medium text-destructive">
                      admin
                    </span>
                  )}
                </Link>
                <button
                  type="button"
                  onClick={() => void onLogout()}
                  className="rounded-lg border border-input bg-background px-3 py-1.5 text-xs font-medium text-foreground hover:bg-accent"
                >
                  登出
                </button>
              </>
            ) : (
              <>
                <NavLink to="/login" className={navCls}>
                  登录
                </NavLink>
                <Link
                  to="/register"
                  className="rounded-lg bg-primary px-3 py-2 text-sm font-medium text-primary-foreground shadow-soft hover:bg-primary/90"
                >
                  注册
                </Link>
              </>
            )}
          </div>
        </div>
      </header>

      <main className="mx-auto min-h-[calc(100vh-8rem)] max-w-7xl">
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
          <Route path="/search" element={<Search />} />
          <Route path="/notifications" element={<Notifications />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="/login" element={<Login />} />
          <Route path="/register" element={<Register />} />
          <Route path="/verify-email" element={<VerifyEmail />} />
          <Route path="/reset-password" element={<ResetPassword />} />
          <Route path="/admin" element={<Admin />} />
        </Routes>
      </main>

      <footer className="border-t border-border px-4 py-4 text-center text-xs text-muted-foreground">
        Botbattle · 多游戏 Bot 竞赛平台（德州 / 五子棋 / 点格棋）
      </footer>
    </div>
  )
}
