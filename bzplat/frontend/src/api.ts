/** Botzone Poker 前端 API 客户端。
 *
 * - 浏览器认证只使用同源 HttpOnly cookie，会话凭据不进入 JavaScript 存储
 * - 默认 credentials: 'include'
 * - JSON body 自动序列化
 */

const TOKEN_KEY = 'bzplat_token'
const USER_KEY = 'bzplat_user'
const SAFE_FAILURE_KEY = 'bzplat_last_safe_api_failure'
const AUTH_EPOCH_KEY = 'bzplat_auth_epoch'
const AUTH_EPOCH_CHANNEL = 'bzplat-auth-epoch-v1'

export interface SafeApiFailure {
  template: string
  status: number
  trace_id: string
}

export interface ApiRequestInit extends RequestInit {
  /** Treat a request as caller-isolated so its 401 cannot clear the shared UI session. */
  suppressAuth?: boolean
}

export interface ApiUploadProgress {
  loaded: number
  total: number | null
  percent: number | null
}

export interface ApiFormUploadOptions {
  onProgress?: (progress: ApiUploadProgress) => void
  onTransferComplete?: () => void
  signal?: AbortSignal
  /** Treat a request as caller-isolated so its 401 cannot clear the shared UI session. */
  suppressAuth?: boolean
}

const SAFE_API_TEMPLATES = [
  '/api/auth/*',
  '/api/bots/*',
  '/api/matches/*',
  '/api/contests/*',
  '/api/communications/*',
  '/api/feedback/bugs',
  '/api/notifications',
] as const

function safeApiTemplate(path: string): string | null {
  const pathname = path.split(/[?#]/, 1)[0]
  if (pathname.startsWith('/api/auth/')) return '/api/auth/*'
  if (pathname.startsWith('/api/bots/')) return '/api/bots/*'
  if (pathname.startsWith('/api/matches/')) return '/api/matches/*'
  if (pathname.startsWith('/api/contests/')) return '/api/contests/*'
  if (pathname.startsWith('/api/communications/')) return '/api/communications/*'
  if (pathname === '/api/feedback/bugs') return '/api/feedback/bugs'
  if (pathname === '/api/notifications') return '/api/notifications'
  return null
}

export function lastSafeApiFailure(): SafeApiFailure | null {
  try {
    const parsed = JSON.parse(sessionStorage.getItem(SAFE_FAILURE_KEY) || 'null') as SafeApiFailure | null
    if (!parsed || !SAFE_API_TEMPLATES.includes(parsed.template as never)) return null
    return parsed
  } catch {
    return null
  }
}

export interface CurrentUser {
  id: number
  username: string
  email: string
  role: 'user' | 'organizer' | 'admin'
  display_name: string
  is_active: number
  email_verified?: number
  created_at?: string
  last_login_at?: string
  bio?: string
  avatar?: string
  xp?: number
  level?: number
  real_name?: string
  phone?: string
  school?: string
  student_id?: string
}

// Older releases persisted a bearer and the complete user projection.  Clear
// both at module startup, but never read them: an injected/stale value must not
// become a browser credential even if storage deletion is unavailable.
for (const legacyKey of [TOKEN_KEY, USER_KEY]) {
  try {
    localStorage.removeItem(legacyKey)
  } catch {
    /* localStorage can be unavailable under hardened browser policies. */
  }
}

let currentUserMemory: CurrentUser | null = null
let currentIdentityGeneration = 0
type CurrentUserListener = (user: CurrentUser | null) => void
const currentUserListeners = new Set<CurrentUserListener>()

function emitCurrentUser(): void {
  for (const listener of currentUserListeners) listener(currentUserMemory)
}

export const currentUserStore = {
  get: (): CurrentUser | null => currentUserMemory,
  set: (user: CurrentUser): void => {
    currentUserMemory = user
    currentIdentityGeneration += 1
    emitCurrentUser()
  },
  clear: (): void => {
    currentUserMemory = null
    currentIdentityGeneration += 1
    emitCurrentUser()
  },
  revision: (): number => currentIdentityGeneration,
  subscribe: (listener: CurrentUserListener): (() => void) => {
    currentUserListeners.add(listener)
    return () => currentUserListeners.delete(listener)
  },
}

function validAuthEpoch(value: unknown): value is string {
  return typeof value === 'string' && /^[A-Za-z0-9._:-]{1,128}$/.test(value)
}

let authStorageAvailable = false
function readStoredAuthEpoch(): string | null {
  try {
    const value = localStorage.getItem(AUTH_EPOCH_KEY)
    authStorageAvailable = true
    return validAuthEpoch(value) ? value : null
  } catch {
    authStorageAvailable = false
    return null
  }
}

let observedAuthEpoch = readStoredAuthEpoch()
let authEpochStale = false
let externalAuthReconciliationPending = false
let authBroadcast: BroadcastChannel | null = null

function newAuthEpoch(): string {
  try {
    if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
      return crypto.randomUUID()
    }
  } catch {
    /* Fall through to a non-sensitive compatibility nonce. */
  }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`
}

function noteExternalAuthEpoch(value: unknown): boolean {
  if (!validAuthEpoch(value) || value === observedAuthEpoch) return false
  observedAuthEpoch = value
  authEpochStale = true
  externalAuthReconciliationPending = true
  // Never continue displaying the old account while the browser is already
  // carrying another tab's cookie.  /me repopulates the projection below.
  currentUserStore.clear()
  return true
}

function pollStoredAuthEpoch(): boolean {
  const value = readStoredAuthEpoch()
  return value !== null && noteExternalAuthEpoch(value)
}

if (typeof window !== 'undefined') {
  window.addEventListener('storage', (event) => {
    if (event.key !== AUTH_EPOCH_KEY || !noteExternalAuthEpoch(event.newValue)) return
    void reconcileCurrentIdentity().catch(() => undefined)
  })
  try {
    authBroadcast = new BroadcastChannel(AUTH_EPOCH_CHANNEL)
    authBroadcast.addEventListener('message', (event) => {
      if (!noteExternalAuthEpoch(event.data)) return
      void reconcileCurrentIdentity().catch(() => undefined)
    })
  } catch {
    authBroadcast = null
  }
}

function publishAuthEpoch(): void {
  const epoch = newAuthEpoch()
  observedAuthEpoch = epoch
  authEpochStale = false
  externalAuthReconciliationPending = false
  try {
    localStorage.setItem(AUTH_EPOCH_KEY, epoch)
    authStorageAvailable = true
  } catch {
    authStorageAvailable = false
  }
  try {
    authBroadcast?.postMessage(epoch)
  } catch {
    /* A closed/blocked channel falls back to storage polling or /me checks. */
    authBroadcast = null
  }
}

function authSignalTransportAvailable(): boolean {
  return authStorageAvailable || authBroadcast !== null
}

interface IdentitySnapshot {
  generation: number
  userId: number | null
  epoch: string | null
}

type IdentityBindingMode = 'none' | 'epoch' | 'full'

function currentIdentitySnapshot(): IdentitySnapshot {
  return {
    generation: currentIdentityGeneration,
    userId: currentUserMemory?.id ?? null,
    epoch: observedAuthEpoch,
  }
}

const PUBLIC_CREDENTIAL_AUTH_PATHS = new Set([
  '/api/auth/captcha',
  '/api/auth/login',
  '/api/auth/register',
  '/api/auth/verify-email',
  '/api/auth/resend-verify',
  '/api/auth/request-reset',
  '/api/auth/reset-password',
])

function requestPathname(path: string): string {
  return path.split(/[?#]/, 1)[0]
}

function isPublicCredentialAuthPath(path: string): boolean {
  return PUBLIC_CREDENTIAL_AUTH_PATHS.has(requestPathname(path))
}

/** 登录/注册等凭据接口：401 表示业务错误，不是会话过期。 */
function isCredentialAuthPath(path: string): boolean {
  const pathname = requestPathname(path)
  return isPublicCredentialAuthPath(path) || pathname === '/api/auth/change-password'
}

function isIdentityGuardExempt(path: string, options: ApiRequestInit): boolean {
  const pathname = requestPathname(path)
  if (!pathname.startsWith('/api/')) return true
  if (pathname === '/api/auth/me') return true
  if (options.suppressAuth && options.credentials === 'omit') return true
  return isPublicCredentialAuthPath(path)
}

function responseIdentityBindingMode(
  path: string,
  options: ApiRequestInit,
): IdentityBindingMode {
  const pathname = requestPathname(path)
  if (!pathname.startsWith('/api/')) return 'none'
  if (options.suppressAuth && options.credentials === 'omit') return 'none'
  // Public credential requests must not require a private /me preflight, but
  // their result still belongs to the auth epoch in which it was requested.
  // Ignore ordinary in-memory projection revisions here: an initial /me may
  // legitimately finish while a register/reset form is being submitted.
  if (isPublicCredentialAuthPath(path)) return 'epoch'
  return 'full'
}

interface IdentityReconciliation {
  beforeUserId: number | null
  afterUserId: number | null
  reconciledExternalEpoch: boolean
}

let identityReconciliation: Promise<IdentityReconciliation> | null = null

function validCurrentUserPayload(value: unknown): value is CurrentUser {
  if (!value || typeof value !== 'object') return false
  const candidate = value as Partial<CurrentUser>
  return (
    Number.isSafeInteger(candidate.id) && Number(candidate.id) > 0 &&
    typeof candidate.username === 'string' &&
    typeof candidate.email === 'string' &&
    typeof candidate.display_name === 'string' &&
    (candidate.role === 'user' || candidate.role === 'organizer' || candidate.role === 'admin') &&
    (candidate.is_active === 0 || candidate.is_active === 1)
  )
}

/**
 * Re-read the browser-owned cookie identity without going through apiFetch.
 * The single shared promise prevents a burst of stale page requests from
 * racing multiple /me projections into memory.
 */
async function reconcileCurrentIdentity(): Promise<IdentityReconciliation> {
  if (identityReconciliation) return identityReconciliation
  const beforeUserId = currentUserStore.get()?.id ?? null
  const startedStale = authEpochStale || externalAuthReconciliationPending
  if (startedStale) currentUserStore.clear()

  identityReconciliation = (async () => {
    for (let attempt = 0; attempt < 3; attempt += 1) {
      pollStoredAuthEpoch()
      const epochAtStart = observedAuthEpoch
      authEpochStale = false
      let response: Response
      try {
        response = await fetch('/api/auth/me', {
          method: 'GET',
          credentials: 'include',
          cache: 'no-store',
          referrerPolicy: 'no-referrer',
          headers: { Accept: 'application/json' },
        })
      } catch (cause) {
        authEpochStale = true
        throw cause
      }

      const changedDuringRequest = pollStoredAuthEpoch() || observedAuthEpoch !== epochAtStart
      if (changedDuringRequest || authEpochStale) continue

      let nextUser: CurrentUser | null
      try {
        if (response.status === 401) {
          nextUser = null
        } else if (response.ok) {
          const body = await response.json() as { user?: unknown }
          if (!validCurrentUserPayload(body.user)) {
            throw new Error('身份同步响应格式无效')
          }
          nextUser = body.user
        } else {
          throw new ApiError('/api/auth/me', response.status, await readErrorDetail(response))
        }
      } catch (cause) {
        const changedWhileReadingBody = (
          pollStoredAuthEpoch() || observedAuthEpoch !== epochAtStart || authEpochStale
        )
        if (changedWhileReadingBody) continue
        authEpochStale = true
        throw cause
      }

      const changedWhileReadingBody = (
        pollStoredAuthEpoch() || observedAuthEpoch !== epochAtStart || authEpochStale
      )
      if (changedWhileReadingBody) continue

      if (nextUser) currentUserStore.set(nextUser)
      else currentUserStore.clear()
      externalAuthReconciliationPending = false
      return {
        beforeUserId,
        afterUserId: nextUser?.id ?? null,
        reconciledExternalEpoch: startedStale,
      }
    }
    authEpochStale = true
    throw new IdentityChangedError('/api/auth/me')
  })().finally(() => {
    identityReconciliation = null
  })
  return identityReconciliation
}

async function guardIdentityBeforeRequest(
  path: string,
  options: ApiRequestInit,
): Promise<void> {
  if (isIdentityGuardExempt(path, options)) return
  const detectedExternalEpoch = (
    pollStoredAuthEpoch() || authEpochStale || externalAuthReconciliationPending
  )
  const fallbackRevalidation = !authSignalTransportAvailable()
  if (!detectedExternalEpoch && !fallbackRevalidation) return

  const reconciliation = await reconcileCurrentIdentity()
  if (
    detectedExternalEpoch ||
    reconciliation.reconciledExternalEpoch ||
    reconciliation.beforeUserId !== reconciliation.afterUserId
  ) {
    // The original action belongs to the old page projection.  Even after /me
    // has synchronized the UI store, require the user to repeat that action.
    throw new IdentityChangedError(path)
  }
}

async function guardIdentityAfterResponse(
  path: string,
  options: ApiRequestInit,
  sentIdentity: IdentitySnapshot,
  bindingMode = responseIdentityBindingMode(path, options),
): Promise<void> {
  if (bindingMode === 'none') return
  const externalEpoch = (
    pollStoredAuthEpoch() || authEpochStale || externalAuthReconciliationPending
  )
  const epochChanged = observedAuthEpoch !== sentIdentity.epoch
  const memoryChanged = bindingMode === 'full' && (
    currentIdentityGeneration !== sentIdentity.generation ||
    (currentUserStore.get()?.id ?? null) !== sentIdentity.userId
  )
  if (!externalEpoch && !epochChanged && !memoryChanged) return
  if (externalEpoch) await reconcileCurrentIdentity()
  // Never project a response issued under an identity that is no longer the
  // one rendered by this tab.  Unsafe requests are guarded before sending;
  // this also discards late read responses during an account switch.
  throw new IdentityChangedError(path)
}

async function readIdentityBoundBody<T>(
  path: string,
  options: ApiRequestInit,
  sentIdentity: IdentitySnapshot,
  bindingMode: IdentityBindingMode,
  reader: () => Promise<T>,
): Promise<T> {
  let value: T
  try {
    value = await reader()
  } catch (cause) {
    // Prefer the security boundary error if identity changed while a malformed
    // or truncated response body was being consumed.
    await guardIdentityAfterResponse(path, options, sentIdentity, bindingMode)
    throw cause
  }
  await guardIdentityAfterResponse(path, options, sentIdentity, bindingMode)
  return value
}

async function verifyLoginCookieIdentityWithoutSignals(
  path: string,
  value: unknown,
): Promise<void> {
  if (
    requestPathname(path) !== '/api/auth/login' ||
    authSignalTransportAvailable()
  ) return

  const responseUser = (
    value && typeof value === 'object' && 'user' in value
  ) ? (value as { user?: unknown }).user : null
  if (!validCurrentUserPayload(responseUser)) {
    throw new Error('登录响应格式无效')
  }

  // With both storage events and BroadcastChannel unavailable, another tab's
  // Set-Cookie cannot be observed locally. Confirm the browser-owned cookie
  // directly before allowing a login response to become a private projection.
  const reconciliation = await reconcileCurrentIdentity()
  if (reconciliation.afterUserId !== responseUser.id) {
    throw new IdentityChangedError(path)
  }
}

function isAuthPublicHash(): boolean {
  const h = location.hash
  return (
    h.startsWith('#/login') ||
    h.startsWith('#/register') ||
    h.startsWith('#/verify') ||
    h.startsWith('#/reset-password')
  )
}

async function readErrorDetail(r: Response): Promise<string> {
  let detail = `${r.status} ${r.statusText}`
  try {
    const j = (await r.clone().json()) as { detail?: unknown }
    if (j?.detail) {
      detail = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail)
    }
  } catch {
    /* 非 JSON */
  }
  return detail
}

export async function apiFetch<T = unknown>(
  path: string,
  options: ApiRequestInit = {},
): Promise<T> {
  await guardIdentityBeforeRequest(path, options)
  const { suppressAuth = false, ...requestOptions } = options
  const headers = new Headers(requestOptions.headers || {})
  const hasExplicitAuthorization = headers.has('Authorization')
  const sentIdentity = currentIdentitySnapshot()
  const sentIdentityIsCurrent = () => (
    !hasExplicitAuthorization &&
    currentIdentityGeneration === sentIdentity.generation &&
    (currentUserStore.get()?.id ?? null) === sentIdentity.userId &&
    observedAuthEpoch === sentIdentity.epoch
  )

  let body = options.body
  if (
    body &&
    typeof body === 'object' &&
    !(body instanceof FormData) &&
    !(body instanceof Blob) &&
    !(body instanceof ArrayBuffer) &&
    !headers.has('Content-Type')
  ) {
    headers.set('Content-Type', 'application/json')
    body = JSON.stringify(body)
  }

  const r = await fetch(path, {
    ...requestOptions,
    headers,
    body,
    credentials: requestOptions.credentials ?? 'include',
  })

  let responseIdentity = sentIdentity
  let responseBindingMode = responseIdentityBindingMode(path, options)
  if (requestPathname(path) === '/api/auth/login' && r.ok) {
    // Set-Cookie is applied before fetch exposes the response body. Linearize
    // that browser-owned credential transition at the response headers: the
    // old private projection is no longer safe to display, and every older
    // response body must now fail its identity binding.
    currentUserStore.clear()
    publishAuthEpoch()
    responseIdentity = currentIdentitySnapshot()
    responseBindingMode = 'epoch'
  }

  await guardIdentityAfterResponse(
    path,
    options,
    responseIdentity,
    responseBindingMode,
  )

  if (r.status === 401) {
    const detail = await readIdentityBoundBody(
      path,
      options,
      responseIdentity,
      responseBindingMode,
      () => readErrorDetail(r),
    )
    // /me 探测与凭据接口：不跳登录页（避免未登录打开页面就刷错）
    // Caller-isolated requests may carry a deliberately frozen identity.  A
    // late 401 from that identity must not clear or redirect a newer session.
    const soft = suppressAuth || path.includes('/api/auth/me') || isCredentialAuthPath(path)
    const mayMutateCurrentAuth = !suppressAuth && sentIdentityIsCurrent()
    if (!soft && mayMutateCurrentAuth) {
      currentUserStore.clear()
      if (!isAuthPublicHash()) {
        const back = encodeURIComponent(location.hash.replace(/^#/, '') || '/')
        location.hash = `#/login?from=${back}&reason=expired`
      }
    } else if (path.includes('/api/auth/me') && mayMutateCurrentAuth) {
      currentUserStore.clear()
    }
    throw new UnauthorizedError(path, detail)
  }

  if (!r.ok) {
    const detail = await readIdentityBoundBody(
      path,
      options,
      responseIdentity,
      responseBindingMode,
      () => readErrorDetail(r),
    )
    const template = safeApiTemplate(path)
    if (template) {
      sessionStorage.setItem(SAFE_FAILURE_KEY, JSON.stringify({
        template,
        status: r.status,
        trace_id: (r.headers.get('x-trace-id') || '').slice(0, 64),
      }))
    }
    throw new ApiError(path, r.status, detail)
  }

  if (r.status === 204) return undefined as unknown as T
  const ct = r.headers.get('content-type') || ''
  if (ct.includes('application/json')) {
    const value = await readIdentityBoundBody(
      path,
      options,
      responseIdentity,
      responseBindingMode,
      async () => await r.json() as T,
    )
    await verifyLoginCookieIdentityWithoutSignals(path, value)
    return value
  }
  return readIdentityBoundBody(
    path,
    options,
    responseIdentity,
    responseBindingMode,
    async () => await r.text() as unknown as T,
  )
}

export class ApiError extends Error {
  status: number
  detail: string
  constructor(path: string, status: number, detail: string) {
    super(`${path}: ${detail}`)
    this.status = status
    this.detail = detail
  }
}

export class UnauthorizedError extends ApiError {
  constructor(path: string, detail = '未登录或会话过期') {
    super(path, 401, detail)
    this.name = 'UnauthorizedError'
  }
}

export class IdentityChangedError extends Error {
  path: string
  constructor(path: string) {
    super('账号会话已在其他标签页变化，身份已重新同步，请重试当前操作')
    this.name = 'IdentityChangedError'
    this.path = path
  }
}

export function apiGet<T = unknown>(
  path: string,
  options: Omit<ApiRequestInit, 'method'> = {},
): Promise<T> {
  return apiFetch<T>(path, { ...options, method: 'GET' })
}

export function apiJson<T = unknown>(
  path: string,
  method: 'POST' | 'PUT' | 'PATCH' | 'DELETE',
  body?: unknown,
  options: Omit<ApiRequestInit, 'method' | 'body'> = {},
): Promise<T> {
  return apiFetch<T>(path, {
    ...options,
    method,
    body: body === undefined ? undefined : (body as BodyInit),
  })
}

export const apiPost = apiJson

/** Project the user from a login response already bound by apiFetch. */
export function confirmAuthenticatedSession(user: CurrentUser): void {
  currentUserStore.set(user)
}

/**
 * Commit signed-out state after a server operation has already invalidated the
 * session (currently password rotation).  Callers must only invoke this from a
 * confirmed 2xx branch.
 */
export function confirmServerInvalidatedSession(): void {
  currentUserStore.clear()
  publishAuthEpoch()
}

/** A failed logout keeps both the in-memory identity and current navigation. */
export async function logoutCurrentSession(): Promise<void> {
  await apiPost('/api/auth/logout', 'POST', undefined, { suppressAuth: true })
  confirmServerInvalidatedSession()
}

/** multipart/form-data（credentials 已由 apiFetch 带上） */
export function apiForm<T = unknown>(
  path: string,
  method: 'POST' | 'PUT' | 'PATCH' = 'POST',
  fields: Record<string, string | Blob | File | boolean | number | undefined | null> = {},
  options: Omit<ApiRequestInit, 'method' | 'body'> = {},
): Promise<T> {
  const fd = new FormData()
  for (const [k, v] of Object.entries(fields)) {
    if (v === undefined || v === null) continue
    if (typeof v === 'boolean' || typeof v === 'number') {
      fd.append(k, String(v))
    } else {
      fd.append(k, v)
    }
  }
  return apiFetch<T>(path, { ...options, method, body: fd })
}

/**
 * multipart upload with browser-reported transfer progress.
 *
 * fetch does not expose upload progress, so Bot binaries use XHR for this one
 * transport concern. Authentication, credentials, 401 handling and safe failure
 * diagnostics intentionally mirror apiFetch. ``onTransferComplete`` marks only
 * that the request body reached the server; the response can still be waiting on
 * binary classification and canonical first-turn preflight.
 */
export function apiFormWithProgress<T = unknown>(
  path: string,
  fields: Record<string, string | Blob | File | boolean | number | undefined | null> = {},
  options: ApiFormUploadOptions = {},
): Promise<T> {
  const fd = new FormData()
  for (const [key, value] of Object.entries(fields)) {
    if (value === undefined || value === null) continue
    fd.append(key, typeof value === 'boolean' || typeof value === 'number' ? String(value) : value)
  }

  return new Promise<T>((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    const { signal, suppressAuth = false } = options
    let settled = false

    const cleanup = () => signal?.removeEventListener('abort', abort)
    const finish = (callback: () => void) => {
      if (settled) return
      settled = true
      cleanup()
      callback()
    }
    const abort = () => xhr.abort()

    xhr.open('POST', path)
    xhr.withCredentials = true
    // Freeze the in-memory identity generation associated with this request. A
    // large upload can finish after account switching; that stale response must
    // never mutate the newer global auth state. Authentication itself remains
    // exclusively in the browser-managed HttpOnly cookie.
    let sentIdentity = currentIdentitySnapshot()

    const sentIdentityIsCurrent = () => (
      currentIdentityGeneration === sentIdentity.generation &&
      (currentUserStore.get()?.id ?? null) === sentIdentity.userId &&
      observedAuthEpoch === sentIdentity.epoch
    )

    xhr.upload.addEventListener('progress', (event) => {
      const total = event.lengthComputable && event.total > 0 ? event.total : null
      options.onProgress?.({
        loaded: event.loaded,
        total,
        percent: total === null ? null : Math.min(100, Math.round((event.loaded / total) * 100)),
      })
    })
    xhr.upload.addEventListener('load', () => options.onTransferComplete?.())

    xhr.addEventListener('load', async () => {
      try {
        await guardIdentityAfterResponse(path, {
          method: 'POST',
          credentials: 'include',
          suppressAuth,
        }, sentIdentity)
      } catch (cause) {
        finish(() => reject(cause))
        return
      }
      const status = xhr.status
      const statusText = xhr.statusText || '请求失败'
      let parsed: unknown = xhr.responseText
      const contentType = xhr.getResponseHeader('content-type') || ''
      if (contentType.includes('application/json') && xhr.responseText) {
        try {
          parsed = JSON.parse(xhr.responseText)
        } catch {
          parsed = xhr.responseText
        }
      }

      let detail = `${status} ${statusText}`
      if (parsed && typeof parsed === 'object' && 'detail' in parsed) {
        const rawDetail = (parsed as { detail?: unknown }).detail
        if (rawDetail) detail = typeof rawDetail === 'string' ? rawDetail : JSON.stringify(rawDetail)
      }

      try {
        await guardIdentityAfterResponse(path, {
          method: 'POST',
          credentials: 'include',
          suppressAuth,
        }, sentIdentity)
      } catch (cause) {
        finish(() => reject(cause))
        return
      }

      if (status === 401) {
        const soft = suppressAuth || path.includes('/api/auth/me') || isCredentialAuthPath(path)
        // Only the request identity whose credentials failed may clear the
        // shared session.  In particular, a delayed 401 from account A cannot
        // sign out account B after an account switch.
        const mayMutateCurrentAuth = !suppressAuth && sentIdentityIsCurrent()
        if (!soft && mayMutateCurrentAuth) {
          currentUserStore.clear()
          if (!isAuthPublicHash()) {
            const back = encodeURIComponent(location.hash.replace(/^#/, '') || '/')
            location.hash = `#/login?from=${back}&reason=expired`
          }
        } else if (path.includes('/api/auth/me') && mayMutateCurrentAuth) {
          currentUserStore.clear()
        }
        finish(() => reject(new UnauthorizedError(path, detail)))
        return
      }

      if (status < 200 || status >= 300) {
        const template = safeApiTemplate(path)
        if (template) {
          sessionStorage.setItem(SAFE_FAILURE_KEY, JSON.stringify({
            template,
            status,
            trace_id: (xhr.getResponseHeader('x-trace-id') || '').slice(0, 64),
          }))
        }
        finish(() => reject(new ApiError(path, status, detail)))
        return
      }

      finish(() => {
        if (status === 204) resolve(undefined as T)
        else resolve(parsed as T)
      })
    })
    xhr.addEventListener('error', () => finish(() => reject(new TypeError('网络请求失败'))))
    xhr.addEventListener('abort', () => finish(() => reject(new DOMException('上传已取消', 'AbortError'))))

    void guardIdentityBeforeRequest(path, {
      method: 'POST',
      credentials: 'include',
      suppressAuth,
    }).then(() => {
      if (signal?.aborted) {
        finish(() => reject(new DOMException('上传已取消', 'AbortError')))
        return
      }
      sentIdentity = currentIdentitySnapshot()
      signal?.addEventListener('abort', abort, { once: true })
      xhr.send(fd)
    }).catch((cause: unknown) => finish(() => reject(cause)))
  })
}

export function apiUpload<T = unknown>(
  path: string,
  file: File,
  fields: Record<string, string> = {},
  method: 'POST' | 'PUT' = 'POST',
  options: Omit<ApiRequestInit, 'method' | 'body'> = {},
): Promise<T> {
  return apiForm<T>(path, method, { ...fields, file }, options)
}

export function errMsg(e: unknown, fallback = '操作失败'): string {
  if (e instanceof ApiError) return e.detail || fallback
  if (e instanceof Error) return e.message || fallback
  return fallback
}

export function isUnauthorized(e: unknown): boolean {
  return e instanceof UnauthorizedError || (e instanceof ApiError && e.status === 401)
}

/** 构造人类对战 WebSocket URL。
 * 同源 HttpOnly ``bz_session`` cookie 由浏览器在握手时自动携带；
 * 会话 token 不得进入 URL，避免泄漏到访问日志和诊断记录。 */
export function playWsUrl(matchId: string): string {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${location.host}/api/matches/${matchId}/play`
}
