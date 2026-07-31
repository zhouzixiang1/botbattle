/** 认证 Context：me / login / logout。 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from 'react'
import {
  apiGet,
  apiPost,
  currentUserStore,
  userToken,
  type CurrentUser,
} from '../api'

interface AuthState {
  user: CurrentUser | null
  loading: boolean
  isLoggedIn: boolean
  login: (
    username: string,
    password: string,
    captchaId: string,
    captchaAnswer: string,
  ) => Promise<CurrentUser>
  logout: () => Promise<void>
  refresh: () => Promise<CurrentUser | null>
}

const AuthContext = createContext<AuthState | undefined>(undefined)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<CurrentUser | null>(() => currentUserStore.get())
  const [loading, setLoading] = useState(true)

  const refresh = useCallback(async (): Promise<CurrentUser | null> => {
    try {
      const d = await apiGet<{ user: CurrentUser }>('/api/auth/me')
      currentUserStore.set(d.user)
      setUser(d.user)
      return d.user
    } catch {
      currentUserStore.clear()
      userToken.clear()
      setUser(null)
      return null
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const login = useCallback(
    async (
      username: string,
      password: string,
      captchaId: string,
      captchaAnswer: string,
    ): Promise<CurrentUser> => {
      const d = await apiPost<{ user: CurrentUser; token?: string }>(
        '/api/auth/login',
        'POST',
        {
          username,
          password,
          captcha_id: captchaId,
          captcha_answer: captchaAnswer,
        },
      )
      if (d.token) userToken.set(d.token)
      currentUserStore.set(d.user)
      setUser(d.user)
      return d.user
    },
    [],
  )

  const logout = useCallback(async (): Promise<void> => {
    try {
      await apiPost('/api/auth/logout', 'POST')
    } catch {
      /* 忽略服务端错误 */
    }
    userToken.clear()
    currentUserStore.clear()
    setUser(null)
  }, [])

  return (
    <AuthContext.Provider
      value={{ user, loading, isLoggedIn: !!user, login, logout, refresh }}
    >
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth 必须在 <AuthProvider> 内使用')
  return ctx
}
