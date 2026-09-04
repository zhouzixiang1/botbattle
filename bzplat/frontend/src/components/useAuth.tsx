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
  confirmAuthenticatedSession,
  confirmServerInvalidatedSession as commitServerInvalidatedSession,
  currentUserStore,
  IdentityChangedError,
  logoutCurrentSession,
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
  /** Only for a preceding 2xx operation that already revoked this session. */
  confirmServerInvalidatedSession: () => void
}

const AuthContext = createContext<AuthState | undefined>(undefined)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<CurrentUser | null>(() => currentUserStore.get())
  const [loading, setLoading] = useState(true)
  // Only the newest auth operation may project its result into module memory
  // and React state. In particular, the initial /me probe can finish after a
  // deliberate login or logout on a slow connection.
  const authGenerationRef = useRef(0)
  const renderedStoreRevisionRef = useRef(currentUserStore.revision())

  useEffect(() => {
    const projectUser = (nextUser: CurrentUser | null) => {
      setUser(nextUser)
      setLoading(false)
    }
    const unsubscribe = currentUserStore.subscribe(projectUser)
    // Close the render-to-effect window without turning the ordinary initial
    // anonymous /me probe into an already-finished loading state.
    if (currentUserStore.revision() !== renderedStoreRevisionRef.current) {
      projectUser(currentUserStore.get())
    }
    return unsubscribe
  }, [])

  const refresh = useCallback(async (): Promise<CurrentUser | null> => {
    const generation = ++authGenerationRef.current
    const storeRevision = currentUserStore.revision()
    try {
      const d = await apiGet<{ user: CurrentUser }>('/api/auth/me')
      if (
        authGenerationRef.current !== generation ||
        currentUserStore.revision() !== storeRevision
      ) return currentUserStore.get()
      currentUserStore.set(d.user)
      setUser(d.user)
      setLoading(false)
      return d.user
    } catch (cause) {
      if (
        authGenerationRef.current !== generation ||
        currentUserStore.revision() !== storeRevision
      ) return currentUserStore.get()
      if (cause instanceof IdentityChangedError) {
        setLoading(false)
        return currentUserStore.get()
      }
      currentUserStore.clear()
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
        const d = await apiPost<{ user: CurrentUser }>(
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
        confirmAuthenticatedSession(d.user)
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
    await logoutCurrentSession()
    if (authGenerationRef.current !== generation) return
    setUser(null)
    setLoading(false)
  }, [])

  const confirmServerInvalidatedSession = useCallback((): void => {
    authGenerationRef.current += 1
    commitServerInvalidatedSession()
    setUser(null)
    setLoading(false)
  }, [])

  return (
    <AuthContext.Provider
      value={{
        user,
        loading,
        isLoggedIn: !!user,
        login,
        logout,
        refresh,
        confirmServerInvalidatedSession,
      }}
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
