/** 认证 Context：me / login / logout。 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
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
  // Only the newest auth operation may project its result into shared storage
  // and React state.  In particular, the initial /me probe can finish after a
  // deliberate login or logout on a slow connection.
  const authGenerationRef = useRef(0)

  const refresh = useCallback(async (): Promise<CurrentUser | null> => {
    const generation = ++authGenerationRef.current
    try {
      const d = await apiGet<{ user: CurrentUser }>('/api/auth/me')
      if (authGenerationRef.current !== generation) return currentUserStore.get()
      currentUserStore.set(d.user)
      setUser(d.user)
      setLoading(false)
      return d.user
    } catch {
      if (authGenerationRef.current !== generation) return currentUserStore.get()
      currentUserStore.clear()
      userToken.clear()
      setUser(null)
      setLoading(false)
      return null
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
      const generation = ++authGenerationRef.current
      try {
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
        if (authGenerationRef.current !== generation) return currentUserStore.get() ?? d.user
        if (d.token) userToken.set(d.token)
        currentUserStore.set(d.user)
        setUser(d.user)
        return d.user
      } finally {
        if (authGenerationRef.current === generation) setLoading(false)
      }
    },
    [],
  )

  const logout = useCallback(async (): Promise<void> => {
    const generation = ++authGenerationRef.current
    try {
      await apiPost('/api/auth/logout', 'POST')
    } catch {
      /* 忽略服务端错误 */
    }
    if (authGenerationRef.current !== generation) return
    userToken.clear()
    currentUserStore.clear()
    setUser(null)
    setLoading(false)
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
