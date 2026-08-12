/** Botzone Poker 前端 API 客户端。
 *
 * - 默认 credentials: 'include'（cookie 会话）
 * - 可选 Bearer token（localStorage）
 * - JSON body 自动序列化
 */

const TOKEN_KEY = 'bzplat_token'
const USER_KEY = 'bzplat_user'
const SAFE_FAILURE_KEY = 'bzplat_last_safe_api_failure'

export interface SafeApiFailure {
  template: string
  status: number
  trace_id: string
}

export interface ApiRequestInit extends RequestInit {
  /** Do not inject the mutable global Bearer token into this request. */
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
  /** Do not inject the mutable global Bearer token into this request. */
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

export const userToken = {
  get: (): string | null => localStorage.getItem(TOKEN_KEY),
  set: (t: string) => localStorage.setItem(TOKEN_KEY, t),
  clear: () => localStorage.removeItem(TOKEN_KEY),
}

export const currentUserStore = {
  get: (): CurrentUser | null => {
    const raw = localStorage.getItem(USER_KEY)
    if (!raw) return null
    try {
      return JSON.parse(raw) as CurrentUser
    } catch {
      return null
    }
  },
  set: (u: CurrentUser) => localStorage.setItem(USER_KEY, JSON.stringify(u)),
  clear: () => localStorage.removeItem(USER_KEY),
}

/** 登录/注册等凭据接口：401 表示业务错误，不是会话过期。 */
function isCredentialAuthPath(path: string): boolean {
  return (
    path.includes('/api/auth/login') ||
    path.includes('/api/auth/register') ||
    path.includes('/api/auth/reset') ||
    path.includes('/api/auth/verify') ||
    path.includes('/api/auth/change-password')
  )
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
  const { suppressAuth = false, ...requestOptions } = options
  const headers = new Headers(requestOptions.headers || {})
  const token = suppressAuth ? null : userToken.get()
  if (token && !headers.has('Authorization')) {
    headers.set('Authorization', `Bearer ${token}`)
  }

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

  if (r.status === 401) {
    const detail = await readErrorDetail(r)
    // /me 探测与凭据接口：不跳登录页（避免未登录打开页面就刷错）
    // Caller-isolated requests may carry a deliberately frozen identity.  A
    // late 401 from that identity must not clear or redirect a newer session.
    const soft = suppressAuth || path.includes('/api/auth/me') || isCredentialAuthPath(path)
    if (!soft) {
      userToken.clear()
      currentUserStore.clear()
      if (!isAuthPublicHash()) {
        const back = encodeURIComponent(location.hash.replace(/^#/, '') || '/')
        location.hash = `#/login?from=${back}&reason=expired`
      }
    } else if (path.includes('/api/auth/me')) {
      userToken.clear()
      currentUserStore.clear()
    }
    throw new UnauthorizedError(path, detail)
  }

  if (!r.ok) {
    const template = safeApiTemplate(path)
    if (template) {
      sessionStorage.setItem(SAFE_FAILURE_KEY, JSON.stringify({
        template,
        status: r.status,
        trace_id: (r.headers.get('x-trace-id') || '').slice(0, 64),
      }))
    }
    throw new ApiError(path, r.status, await readErrorDetail(r))
  }

  if (r.status === 204) return undefined as unknown as T
  const ct = r.headers.get('content-type') || ''
  if (ct.includes('application/json')) return (await r.json()) as T
  return (await r.text()) as unknown as T
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
    const token = suppressAuth ? null : userToken.get()
    if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`)

    xhr.upload.addEventListener('progress', (event) => {
      const total = event.lengthComputable && event.total > 0 ? event.total : null
      options.onProgress?.({
        loaded: event.loaded,
        total,
        percent: total === null ? null : Math.min(100, Math.round((event.loaded / total) * 100)),
      })
    })
    xhr.upload.addEventListener('load', () => options.onTransferComplete?.())

    xhr.addEventListener('load', () => {
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

      if (status === 401) {
        const soft = suppressAuth || path.includes('/api/auth/me') || isCredentialAuthPath(path)
        if (!soft) {
          userToken.clear()
          currentUserStore.clear()
          if (!isAuthPublicHash()) {
            const back = encodeURIComponent(location.hash.replace(/^#/, '') || '/')
            location.hash = `#/login?from=${back}&reason=expired`
          }
        } else if (path.includes('/api/auth/me')) {
          userToken.clear()
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

    if (signal?.aborted) {
      finish(() => reject(new DOMException('上传已取消', 'AbortError')))
      return
    }
    signal?.addEventListener('abort', abort, { once: true })
    xhr.send(fd)
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
