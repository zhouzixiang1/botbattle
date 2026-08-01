/** Botzone Poker 前端 API 客户端。
 *
 * - 默认 credentials: 'include'（cookie 会话）
 * - 可选 Bearer token（localStorage）
 * - JSON body 自动序列化
 */

const TOKEN_KEY = 'bzplat_token'
const USER_KEY = 'bzplat_user'

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
  options: RequestInit = {},
): Promise<T> {
  const headers = new Headers(options.headers || {})
  const token = userToken.get()
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
    ...options,
    headers,
    body,
    credentials: 'include',
  })

  if (r.status === 401) {
    const detail = await readErrorDetail(r)
    // /me 探测与凭据接口：不跳登录页（避免未登录打开页面就刷错）
    const soft = path.includes('/api/auth/me') || isCredentialAuthPath(path)
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

export function apiGet<T = unknown>(path: string): Promise<T> {
  return apiFetch<T>(path, { method: 'GET' })
}

export function apiJson<T = unknown>(
  path: string,
  method: 'POST' | 'PUT' | 'PATCH' | 'DELETE',
  body?: unknown,
): Promise<T> {
  return apiFetch<T>(path, {
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
  return apiFetch<T>(path, { method, body: fd })
}

export function apiUpload<T = unknown>(
  path: string,
  file: File,
  fields: Record<string, string> = {},
  method: 'POST' | 'PUT' = 'POST',
): Promise<T> {
  return apiForm<T>(path, method, { ...fields, file })
}

export function errMsg(e: unknown, fallback = '操作失败'): string {
  if (e instanceof ApiError) return e.detail || fallback
  if (e instanceof Error) return e.message || fallback
  return fallback
}

export function isUnauthorized(e: unknown): boolean {
  return e instanceof UnauthorizedError || (e instanceof ApiError && e.status === 401)
}

/** 构造人类对战 WebSocket URL（带 token query 鉴权）。
 * 同源：根据当前 location 推断 ws/wss + host。 */
export function playWsUrl(matchId: string): string {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
  const token = userToken.get() || ''
  return `${proto}//${location.host}/api/matches/${matchId}/play?token=${encodeURIComponent(token)}`
}
