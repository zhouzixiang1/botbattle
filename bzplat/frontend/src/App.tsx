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
import Admin from './pages/admin/Admin'
import ArenaWatch from './pages/ArenaWatch'
import Challenge from './pages/Challenge'
import ContestDetail from './pages/ContestDetail'
import Contests from './pages/Contests'
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
    <AuthProvider>
      <HashRouter>
        <Shell />
      </HashRouter>
    </AuthProvider>
  )
}

function Shell() {
  const navCls = ({ isActive }: { isActive: boolean }) =>
    `px-3 py-2 rounded-lg text-sm font-medium transition ${
      isActive
        ? 'bg-brand-50 text-brand-700'
        : 'text-slate-600 hover:bg-slate-100 hover:text-slate-900'
    }`

  const { user, isLoggedIn, loading, logout } = useAuth()
  const nav = useNavigate()

  const onLogout = async () => {
    await logout()
    nav('/')
  }

  return (
    <div className="min-h-screen font-sans text-slate-800">
      <header className="sticky top-0 z-50 border-b border-slate-200 bg-white/85 backdrop-blur-md">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-2 px-4 py-3 lg:px-6">
          <Link
            to="/"
            className="mr-2 flex items-center gap-2 text-lg font-semibold tracking-tight text-slate-900"
          >
            <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-slate-800 text-sm font-bold text-sky-300 shadow-soft ring-1 ring-slate-600/50">
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
              className="w-40 rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-sm text-slate-700 placeholder:text-slate-400 focus:w-56 focus:outline-none focus:ring-1 focus:ring-brand-300"
            />
          </form>
          <div className="ml-auto flex items-center gap-2">
            {loading ? (
              <span className="text-xs text-slate-400">…</span>
            ) : isLoggedIn && user ? (
              <>
                <NotificationBell />
                <Link
                  to={`/user/${encodeURIComponent(user.username)}`}
                  className="rounded-lg px-3 py-2 text-sm text-slate-600 hover:bg-slate-100"
                >
                  <span className="font-medium text-brand-700">
                    {user.display_name || user.username}
                  </span>
                  {user.role === 'admin' && (
                    <span className="ml-1 rounded-md bg-error-50 px-1.5 py-0.5 text-[10px] font-medium text-error-600">
                      admin
                    </span>
                  )}
                </Link>
                <button
                  type="button"
                  onClick={() => void onLogout()}
                  className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50"
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
                  className="rounded-lg bg-brand-600 px-3 py-2 text-sm font-medium text-white shadow-soft hover:bg-brand-500"
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

      <footer className="border-t border-slate-200 px-4 py-4 text-center text-xs text-slate-400">
        Botbattle · 多游戏 Bot 竞赛平台（德州 / 五子棋 / 点格棋）
      </footer>
    </div>
  )
}
