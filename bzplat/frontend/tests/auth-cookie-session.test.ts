import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

class MemoryStorage {
  readonly values = new Map<string, string>()
  readonly calls: Array<{ method: 'get' | 'set' | 'remove'; key: string }> = []

  getItem(key: string): string | null {
    this.calls.push({ method: 'get', key })
    return this.values.get(key) ?? null
  }

  setItem(key: string, value: string): void {
    this.calls.push({ method: 'set', key })
    this.values.set(key, value)
  }

  removeItem(key: string): void {
    this.calls.push({ method: 'remove', key })
    this.values.delete(key)
  }
}

test('browser authentication is cookie-only and keeps identity in memory', async () => {
  const local = new MemoryStorage()
  local.values.set('bzplat_token', 'legacy-secret')
  local.values.set('bzplat_user', JSON.stringify({ id: 7, email: 'private@example.test' }))
  const session = new MemoryStorage()
  Object.defineProperty(globalThis, 'localStorage', { value: local, configurable: true })
  Object.defineProperty(globalThis, 'sessionStorage', { value: session, configurable: true })
  Object.defineProperty(globalThis, 'location', {
    value: { hash: '#/messages', protocol: 'https:', host: 'example.test' },
    configurable: true,
  })

  const api = await import('../src/api.ts?auth-cookie-session-test')

  assert.equal(local.values.has('bzplat_token'), false)
  assert.equal(local.values.has('bzplat_user'), false)
  assert.deepEqual(
    local.calls.filter(({ key }) => key === 'bzplat_token' || key === 'bzplat_user'),
    [
      { method: 'remove', key: 'bzplat_token' },
      { method: 'remove', key: 'bzplat_user' },
    ],
  )
  assert.equal('userToken' in api, false)

  const user: api.CurrentUser = {
    id: 8,
    username: 'cookie-user',
    email: 'private@example.test',
    role: 'user',
    display_name: 'Cookie User',
    is_active: 1,
  }
  api.currentUserStore.set(user)
  assert.deepEqual(api.currentUserStore.get(), user)
  api.currentUserStore.clear()
  assert.equal(api.currentUserStore.get(), null)
  assert.equal(local.calls.some(({ method, key }) => (
    (method === 'get' || method === 'set') && key !== 'bzplat_auth_epoch'
  )), false)

  local.values.set('bzplat_token', 'must-never-be-read-or-sent')
  let sentInit: RequestInit | undefined
  Object.defineProperty(globalThis, 'fetch', {
    value: async (_path: string, init?: RequestInit) => {
      sentInit = init
      return new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      })
    },
    configurable: true,
  })
  await api.apiGet('/api/auth/me')
  assert.equal(sentInit?.credentials, 'include')
  assert.equal(new Headers(sentInit?.headers).has('Authorization'), false)
  assert.equal(local.calls.some(({ method, key }) => (
    (method === 'get' || method === 'set') && key !== 'bzplat_auth_epoch'
  )), false)

  const accountA = { ...user, id: 9, username: 'account-a', display_name: 'Account A' }
  const accountB = { ...user, id: 10, username: 'account-b', display_name: 'Account B' }
  api.currentUserStore.set(accountA)
  let releaseLateRequest: ((response: Response) => void) | undefined
  Object.defineProperty(globalThis, 'fetch', {
    value: () => new Promise<Response>((resolve) => { releaseLateRequest = resolve }),
    configurable: true,
  })
  const lateRequest = api.apiGet('/api/communications/inbox')
  await new Promise<void>((resolve) => setImmediate(resolve))
  api.currentUserStore.set(accountB)
  releaseLateRequest?.(new Response(JSON.stringify({ detail: '会话已失效' }), {
    status: 401,
    headers: { 'content-type': 'application/json' },
  }))
  await assert.rejects(lateRequest, api.IdentityChangedError)
  assert.deepEqual(api.currentUserStore.get(), accountB)
  assert.equal(location.hash, '#/messages')

  Object.defineProperty(globalThis, 'fetch', {
    value: async () => new Response(JSON.stringify({ detail: '会话已失效' }), {
      status: 401,
      headers: { 'content-type': 'application/json' },
    }),
    configurable: true,
  })
  await assert.rejects(api.apiGet('/api/communications/inbox'), api.UnauthorizedError)
  assert.equal(api.currentUserStore.get(), null)
  assert.match(location.hash, /^#\/login\?from=/)
})

test('owned browser callers contain no persisted bearer fallback', async () => {
  const apiSource = await readFile(new URL('../src/api.ts', import.meta.url), 'utf8')
  const sources = await Promise.all([
    '../src/components/useAuth.tsx',
    '../src/pages/Messages.tsx',
    '../src/pages/Feedback.tsx',
    '../src/pages/admin/EmailTab.tsx',
  ].map(async (path) => [path, await readFile(new URL(path, import.meta.url), 'utf8')] as const))

  for (const [path, source] of sources) {
    assert.doesNotMatch(source, /\buserToken\b/, path)
    assert.doesNotMatch(source, /Authorization\s*['"`]/, path)
    assert.doesNotMatch(source, /Bearer\s+\$?/, path)
  }
  assert.doesNotMatch(apiSource, /localStorage\.(?:getItem|setItem)\((?:TOKEN_KEY|USER_KEY|['"]bzplat_(?:token|user)['"])/)
  assert.doesNotMatch(apiSource, /export const userToken/)
  assert.doesNotMatch(apiSource, /headers\.set\(['"]Authorization['"]/)
  assert.match(apiSource, /localStorage\.removeItem\(legacyKey\)/)
  assert.match(apiSource, /localStorage\.setItem\(AUTH_EPOCH_KEY, epoch\)/)
  const useAuthSource = sources.find(([path]) => path.endsWith('/useAuth.tsx'))?.[1] || ''
  assert.equal(
    useAuthSource.match(/currentUserStore\.revision\(\) !== storeRevision/g)?.length,
    2,
    'refresh success and failure must both reject a superseded store projection',
  )
  assert.match(
    useAuthSource,
    /const logout = useCallback[\s\S]*const generation = \+\+authGenerationRef\.current[\s\S]*await logoutCurrentSession\(\)/,
  )
  assert.match(
    useAuthSource,
    /const confirmServerInvalidatedSession = useCallback[\s\S]*authGenerationRef\.current \+= 1[\s\S]*commitServerInvalidatedSession\(\)/,
  )
})

test('logout keeps the projected identity until the server confirms success', async () => {
  const local = new MemoryStorage()
  const session = new MemoryStorage()
  Object.defineProperty(globalThis, 'localStorage', { value: local, configurable: true })
  Object.defineProperty(globalThis, 'sessionStorage', { value: session, configurable: true })
  Object.defineProperty(globalThis, 'location', {
    value: { hash: '#/settings', protocol: 'https:', host: 'example.test' },
    configurable: true,
  })

  const api = await import('../src/api.ts?logout-confirmation-test')
  const user: api.CurrentUser = {
    id: 41,
    username: 'logout-owner',
    email: 'owner@example.test',
    role: 'user',
    display_name: 'Logout Owner',
    is_active: 1,
  }
  api.currentUserStore.set(user)

  Object.defineProperty(globalThis, 'fetch', {
    value: async () => new Response(JSON.stringify({ detail: 'temporary failure' }), {
      status: 500,
      headers: { 'content-type': 'application/json' },
    }),
    configurable: true,
  })
  await assert.rejects(api.logoutCurrentSession(), api.ApiError)
  assert.deepEqual(api.currentUserStore.get(), user)
  assert.equal(location.hash, '#/settings')

  Object.defineProperty(globalThis, 'fetch', {
    value: async () => { throw new TypeError('network unavailable') },
    configurable: true,
  })
  await assert.rejects(api.logoutCurrentSession(), TypeError)
  assert.deepEqual(api.currentUserStore.get(), user)
  assert.equal(location.hash, '#/settings')

  Object.defineProperty(globalThis, 'fetch', {
    value: async () => new Response(JSON.stringify({ detail: 'request rejected' }), {
      status: 401,
      headers: { 'content-type': 'application/json' },
    }),
    configurable: true,
  })
  await assert.rejects(api.logoutCurrentSession(), api.UnauthorizedError)
  assert.deepEqual(api.currentUserStore.get(), user)
  assert.equal(location.hash, '#/settings')

  Object.defineProperty(globalThis, 'fetch', {
    value: async () => new Response(null, { status: 204 }),
    configurable: true,
  })
  await api.logoutCurrentSession()
  assert.equal(api.currentUserStore.get(), null)
  const signal = local.values.get('bzplat_auth_epoch') || ''
  assert.notEqual(signal, '')
  assert.match(signal, /^[A-Za-z0-9._:-]{1,128}$/)
  assert.doesNotMatch(signal, /logout-owner|owner@example\.test/)
  assert.notEqual(signal, String(user.id))
})

test('a cross-tab cookie identity change is reconciled before a private write', async () => {
  const local = new MemoryStorage()
  local.values.set('bzplat_auth_epoch', 'epoch-account-a')
  const session = new MemoryStorage()
  Object.defineProperty(globalThis, 'localStorage', { value: local, configurable: true })
  Object.defineProperty(globalThis, 'sessionStorage', { value: session, configurable: true })
  Object.defineProperty(globalThis, 'location', {
    value: { hash: '#/settings', protocol: 'https:', host: 'example.test' },
    configurable: true,
  })

  const api = await import('../src/api.ts?cross-tab-preflight-test')
  const accountA: api.CurrentUser = {
    id: 51,
    username: 'account-a',
    email: 'a@example.test',
    role: 'user',
    display_name: 'Account A',
    is_active: 1,
  }
  const accountB: api.CurrentUser = {
    ...accountA,
    id: 52,
    username: 'account-b',
    email: 'b@example.test',
    display_name: 'Account B',
  }
  api.currentUserStore.set(accountA)

  // A second tab has changed the shared HttpOnly cookie to account B and emits
  // only an opaque, non-sensitive generation marker.
  local.values.set('bzplat_auth_epoch', 'epoch-account-b')
  const requested: string[] = []
  Object.defineProperty(globalThis, 'fetch', {
    value: async (path: string) => {
      requested.push(path)
      if (path === '/api/auth/me') {
        return new Response(JSON.stringify({ user: accountB }), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        })
      }
      return new Response(JSON.stringify({ saved_for: accountB.id }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      })
    },
    configurable: true,
  })

  await assert.rejects(
    api.apiJson('/api/notification-prefs', 'PUT', { email_match_done: true }),
    api.IdentityChangedError,
  )
  assert.deepEqual(requested, ['/api/auth/me'])
  assert.deepEqual(api.currentUserStore.get(), accountB)

  const result = await api.apiGet<{ saved_for: number }>('/api/notification-prefs')
  assert.equal(result.saved_for, accountB.id)
  assert.deepEqual(requested, ['/api/auth/me', '/api/notification-prefs'])
})

test('an old /me response cannot overwrite a cross-tab reconciliation', async () => {
  const local = new MemoryStorage()
  local.values.set('bzplat_auth_epoch', 'epoch-old-me-a')
  Object.defineProperty(globalThis, 'localStorage', { value: local, configurable: true })
  Object.defineProperty(globalThis, 'sessionStorage', { value: new MemoryStorage(), configurable: true })
  Object.defineProperty(globalThis, 'location', {
    value: { hash: '#/settings', protocol: 'https:', host: 'example.test' },
    configurable: true,
  })

  const api = await import('../src/api.ts?cross-tab-old-me-test')
  const accountA: api.CurrentUser = {
    id: 61,
    username: 'old-me-a',
    email: 'old-me-a@example.test',
    role: 'user',
    display_name: 'Old Me A',
    is_active: 1,
  }
  const accountB: api.CurrentUser = {
    ...accountA,
    id: 62,
    username: 'old-me-b',
    email: 'old-me-b@example.test',
    display_name: 'Old Me B',
  }
  api.currentUserStore.set(accountA)
  let releaseOldMe: ((response: Response) => void) | undefined
  let meRequests = 0
  Object.defineProperty(globalThis, 'fetch', {
    value: async (path: string) => {
      assert.equal(path, '/api/auth/me')
      meRequests += 1
      if (meRequests === 1) {
        return new Promise<Response>((resolve) => { releaseOldMe = resolve })
      }
      return new Response(JSON.stringify({ user: accountB }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      })
    },
    configurable: true,
  })

  const oldProbe = api.apiGet<{ user: api.CurrentUser }>('/api/auth/me')
  await new Promise<void>((resolve) => setImmediate(resolve))
  local.values.set('bzplat_auth_epoch', 'epoch-old-me-b')
  releaseOldMe?.(new Response(JSON.stringify({ user: accountA }), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  }))
  await assert.rejects(oldProbe, api.IdentityChangedError)
  assert.equal(meRequests, 2)
  assert.deepEqual(api.currentUserStore.get(), accountB)
})

test('a slow old /me body cannot overwrite a completed account reconciliation', async () => {
  const local = new MemoryStorage()
  local.values.set('bzplat_auth_epoch', 'epoch-slow-body-a')
  Object.defineProperty(globalThis, 'localStorage', { value: local, configurable: true })
  Object.defineProperty(globalThis, 'sessionStorage', { value: new MemoryStorage(), configurable: true })
  Object.defineProperty(globalThis, 'location', {
    value: { hash: '#/settings', protocol: 'https:', host: 'example.test' },
    configurable: true,
  })

  const api = await import('../src/api.ts?cross-tab-slow-me-body-test')
  const accountA: api.CurrentUser = {
    id: 71,
    username: 'slow-body-a',
    email: 'slow-body-a@example.test',
    role: 'user',
    display_name: 'Slow Body A',
    is_active: 1,
  }
  const accountB: api.CurrentUser = {
    ...accountA,
    id: 72,
    username: 'slow-body-b',
    email: 'slow-body-b@example.test',
    display_name: 'Slow Body B',
  }
  api.currentUserStore.set(accountA)

  let slowController: ReadableStreamDefaultController<Uint8Array> | undefined
  const slowBody = new ReadableStream<Uint8Array>({
    start(controller) {
      slowController = controller
    },
  })
  const requested: string[] = []
  Object.defineProperty(globalThis, 'fetch', {
    value: async (path: string) => {
      requested.push(path)
      if (path === '/api/auth/me' && requested.length === 1) {
        // Headers resolve now; JSON bytes remain unavailable until after tab B
        // has changed the shared cookie and completed its own /me projection.
        return new Response(slowBody, {
          status: 200,
          headers: { 'content-type': 'application/json' },
        })
      }
      if (path === '/api/auth/me') {
        return new Response(JSON.stringify({ user: accountB }), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        })
      }
      return new Response(JSON.stringify({ saved_for: accountB.id }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      })
    },
    configurable: true,
  })

  const oldRefresh = api.apiGet<{ user: api.CurrentUser }>('/api/auth/me')
    .then((result) => {
      // This is the projection step performed by AuthProvider.refresh().
      api.currentUserStore.set(result.user)
      return result.user
    })
  await new Promise<void>((resolve) => setImmediate(resolve))
  assert.deepEqual(requested, ['/api/auth/me'])

  local.values.set('bzplat_auth_epoch', 'epoch-slow-body-b')
  await assert.rejects(
    api.apiJson('/api/notification-prefs', 'PUT', { email_match_done: true }),
    api.IdentityChangedError,
  )
  assert.deepEqual(requested, ['/api/auth/me', '/api/auth/me'])
  assert.deepEqual(api.currentUserStore.get(), accountB)

  slowController?.enqueue(new TextEncoder().encode(JSON.stringify({ user: accountA })))
  slowController?.close()
  await assert.rejects(oldRefresh, api.IdentityChangedError)
  assert.deepEqual(api.currentUserStore.get(), accountB)
  assert.deepEqual(requested, ['/api/auth/me', '/api/auth/me'])
})

test('a slow earlier login body cannot overwrite a later login cookie and projection', async () => {
  const local = new MemoryStorage()
  local.values.set('bzplat_auth_epoch', 'epoch-before-login-race')
  Object.defineProperty(globalThis, 'localStorage', { value: local, configurable: true })
  Object.defineProperty(globalThis, 'sessionStorage', { value: new MemoryStorage(), configurable: true })
  Object.defineProperty(globalThis, 'location', {
    value: { hash: '#/login', protocol: 'https:', host: 'example.test' },
    configurable: true,
  })

  const api = await import('../src/api.ts?slow-login-body-binding-test')
  const accountA: api.CurrentUser = {
    id: 75,
    username: 'slow-login-a',
    email: 'slow-login-a@example.test',
    role: 'user',
    display_name: 'Slow Login A',
    is_active: 1,
  }
  const accountB: api.CurrentUser = {
    ...accountA,
    id: 76,
    username: 'fast-login-b',
    email: 'fast-login-b@example.test',
    display_name: 'Fast Login B',
  }
  let cookieActor: 'guest' | 'A' | 'B' = 'guest'
  let slowController: ReadableStreamDefaultController<Uint8Array> | undefined
  const slowBody = new ReadableStream<Uint8Array>({
    start(controller) {
      slowController = controller
    },
  })
  Object.defineProperty(globalThis, 'fetch', {
    value: async (path: string, init?: RequestInit) => {
      if (path === '/api/auth/login') {
        const payload = JSON.parse(String(init?.body)) as { username?: string }
        if (payload.username === accountA.username) {
          // Browser applies A's Set-Cookie with these headers, but its JSON body
          // remains delayed. B's later headers must stay authoritative.
          cookieActor = 'A'
          return new Response(slowBody, {
            status: 200,
            headers: { 'content-type': 'application/json' },
          })
        }
        assert.equal(payload.username, accountB.username)
        cookieActor = 'B'
        return new Response(JSON.stringify({ user: accountB }), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        })
      }
      assert.equal(path, '/api/notification-prefs')
      return new Response(JSON.stringify({ actor: cookieActor }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      })
    },
    configurable: true,
  })

  const projectLogin = async (username: string) => {
    const result = await api.apiPost<{ user: api.CurrentUser }>(
      '/api/auth/login',
      'POST',
      { username, password: 'mock-password', captcha_id: 'mock', captcha_answer: 'mock' },
    )
    api.confirmAuthenticatedSession(result.user)
    return result.user
  }
  const slowLoginA = projectLogin(accountA.username)
  await new Promise<void>((resolve) => setImmediate(resolve))
  assert.equal(cookieActor, 'A')
  const accountAHeaderEpoch = local.values.get('bzplat_auth_epoch') || ''
  assert.notEqual(accountAHeaderEpoch, 'epoch-before-login-race')
  assert.equal(api.currentUserStore.get(), null)

  assert.deepEqual(await projectLogin(accountB.username), accountB)
  assert.equal(cookieActor, 'B')
  assert.notEqual(local.values.get('bzplat_auth_epoch'), accountAHeaderEpoch)
  assert.deepEqual(api.currentUserStore.get(), accountB)

  slowController?.enqueue(new TextEncoder().encode(JSON.stringify({ user: accountA })))
  slowController?.close()
  await assert.rejects(slowLoginA, api.IdentityChangedError)
  assert.equal(cookieActor, 'B')
  assert.deepEqual(api.currentUserStore.get(), accountB)

  const write = await api.apiJson<{ actor: string }>(
    '/api/notification-prefs',
    'PUT',
    { email_match_done: true },
  )
  assert.equal(write.actor, 'B')
})

test('a slow login revalidates the HttpOnly cookie when cross-tab signals are unavailable', async () => {
  const unavailableStorage = {
    getItem(): never { throw new DOMException('storage blocked', 'SecurityError') },
    setItem(): never { throw new DOMException('storage blocked', 'SecurityError') },
    removeItem(): never { throw new DOMException('storage blocked', 'SecurityError') },
  }
  Object.defineProperty(globalThis, 'localStorage', {
    value: unavailableStorage,
    configurable: true,
  })
  Object.defineProperty(globalThis, 'sessionStorage', {
    value: new MemoryStorage(),
    configurable: true,
  })
  Object.defineProperty(globalThis, 'location', {
    value: { hash: '#/login', protocol: 'https:', host: 'example.test' },
    configurable: true,
  })

  // Separate module instances model two tabs that cannot exchange either a
  // storage event or BroadcastChannel message while still sharing cookies.
  const apiA = await import('../src/api.ts?slow-login-no-signal-a')
  const apiB = await import('../src/api.ts?slow-login-no-signal-b')
  const accountA: apiA.CurrentUser = {
    id: 79,
    username: 'no-signal-a',
    email: 'no-signal-a@example.test',
    role: 'user',
    display_name: 'No Signal A',
    is_active: 1,
  }
  const accountB: apiA.CurrentUser = {
    ...accountA,
    id: 80,
    username: 'no-signal-b',
    email: 'no-signal-b@example.test',
    display_name: 'No Signal B',
  }
  let cookieActor: 'guest' | 'A' | 'B' = 'guest'
  let slowController: ReadableStreamDefaultController<Uint8Array> | undefined
  const slowBody = new ReadableStream<Uint8Array>({
    start(controller) {
      slowController = controller
    },
  })
  const requested: string[] = []
  Object.defineProperty(globalThis, 'fetch', {
    value: async (path: string, init?: RequestInit) => {
      requested.push(path)
      if (path === '/api/auth/login') {
        const payload = JSON.parse(String(init?.body)) as { username?: string }
        if (payload.username === accountA.username) {
          cookieActor = 'A'
          return new Response(slowBody, {
            status: 200,
            headers: { 'content-type': 'application/json' },
          })
        }
        assert.equal(payload.username, accountB.username)
        cookieActor = 'B'
        return new Response(JSON.stringify({ user: accountB }), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        })
      }
      if (path === '/api/auth/me') {
        const user = cookieActor === 'A' ? accountA : accountB
        return new Response(JSON.stringify({ user }), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        })
      }
      assert.equal(path, '/api/notification-prefs')
      return new Response(JSON.stringify({ actor: cookieActor }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      })
    },
    configurable: true,
  })

  const slowLoginA = apiA.apiPost<{ user: apiA.CurrentUser }>('/api/auth/login', 'POST', {
    username: accountA.username,
  }).then(({ user }) => {
    apiA.confirmAuthenticatedSession(user)
    return user
  })
  await new Promise<void>((resolve) => setImmediate(resolve))
  assert.equal(cookieActor, 'A')

  const resultB = await apiB.apiPost<{ user: apiA.CurrentUser }>('/api/auth/login', 'POST', {
    username: accountB.username,
  })
  apiB.confirmAuthenticatedSession(resultB.user)
  assert.deepEqual(apiB.currentUserStore.get(), accountB)
  assert.equal(cookieActor, 'B')

  slowController?.enqueue(new TextEncoder().encode(JSON.stringify({ user: accountA })))
  slowController?.close()
  await assert.rejects(slowLoginA, apiA.IdentityChangedError)
  assert.deepEqual(apiA.currentUserStore.get(), accountB)

  const write = await apiA.apiJson<{ actor: string }>(
    '/api/notification-prefs',
    'PUT',
    { email_match_done: true },
  )
  assert.equal(write.actor, 'B')
  assert.equal(requested.filter((path) => path === '/api/auth/me').length, 3)
})

for (const credentialPath of [
  '/api/auth/register',
  '/api/auth/verify-email',
  '/api/auth/reset-password',
] as const) {
  test(`${credentialPath} skips private preflight but keeps its body epoch-bound`, async () => {
    const credentialName = credentialPath.slice('/api/auth/'.length)
    const local = new MemoryStorage()
    local.values.set('bzplat_auth_epoch', `epoch-${credentialName}-a`)
    Object.defineProperty(globalThis, 'localStorage', { value: local, configurable: true })
    Object.defineProperty(globalThis, 'sessionStorage', {
      value: new MemoryStorage(),
      configurable: true,
    })
    Object.defineProperty(globalThis, 'location', {
      value: { hash: '#/register', protocol: 'https:', host: 'example.test' },
      configurable: true,
    })

    const api = await import(`../src/api.ts?credential-body-binding-${credentialName}`)
    const accountA: api.CurrentUser = {
      id: 77,
      username: 'credential-a',
      email: 'credential-a@example.test',
      role: 'user',
      display_name: 'Credential A',
      is_active: 1,
    }
    const accountB: api.CurrentUser = {
      ...accountA,
      id: 78,
      username: 'credential-b',
      email: 'credential-b@example.test',
      display_name: 'Credential B',
    }
    api.currentUserStore.set(accountA)

    let slowController: ReadableStreamDefaultController<Uint8Array> | undefined
    const slowBody = new ReadableStream<Uint8Array>({
      start(controller) {
        slowController = controller
      },
    })
    const requested: string[] = []
    Object.defineProperty(globalThis, 'fetch', {
      value: async (path: string) => {
        requested.push(path)
        if (path === credentialPath) {
          return new Response(slowBody, {
            status: 200,
            headers: { 'content-type': 'application/json' },
          })
        }
        assert.equal(path, '/api/auth/me')
        return new Response(JSON.stringify({ user: accountB }), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        })
      },
      configurable: true,
    })

    const oldCredentialResult = api.apiJson(credentialPath, 'POST', { subject: 'credential-a' })
    await new Promise<void>((resolve) => setImmediate(resolve))
    // Public credential submission is not preceded by a private /me request.
    assert.deepEqual(requested, [credentialPath])

    local.values.set('bzplat_auth_epoch', `epoch-${credentialName}-b`)
    slowController?.enqueue(new TextEncoder().encode(JSON.stringify({ user: accountA })))
    slowController?.close()

    await assert.rejects(oldCredentialResult, api.IdentityChangedError)
    assert.deepEqual(requested, [credentialPath, '/api/auth/me'])
    assert.deepEqual(api.currentUserStore.get(), accountB)
  })
}

for (const scenario of [
  {
    name: 'JSON error detail',
    status: 500,
    contentType: 'application/json',
    body: JSON.stringify({ detail: 'stale account A failure' }),
  },
  {
    name: 'non-JSON success',
    status: 200,
    contentType: 'text/plain',
    body: 'stale account A projection',
  },
] as const) {
  test(`a slow ${scenario.name} body remains bound to its original identity`, async () => {
    const local = new MemoryStorage()
    local.values.set('bzplat_auth_epoch', `epoch-slow-${scenario.status}-a`)
    const session = new MemoryStorage()
    Object.defineProperty(globalThis, 'localStorage', { value: local, configurable: true })
    Object.defineProperty(globalThis, 'sessionStorage', { value: session, configurable: true })
    Object.defineProperty(globalThis, 'location', {
      value: { hash: '#/messages', protocol: 'https:', host: 'example.test' },
      configurable: true,
    })

    const api = await import(`../src/api.ts?slow-${scenario.status}-body-binding-test`)
    const accountA: api.CurrentUser = {
      id: 81,
      username: 'body-binding-a',
      email: 'body-binding-a@example.test',
      role: 'user',
      display_name: 'Body Binding A',
      is_active: 1,
    }
    const accountB: api.CurrentUser = {
      ...accountA,
      id: 82,
      username: 'body-binding-b',
      email: 'body-binding-b@example.test',
      display_name: 'Body Binding B',
    }
    api.currentUserStore.set(accountA)

    let slowController: ReadableStreamDefaultController<Uint8Array> | undefined
    const slowBody = new ReadableStream<Uint8Array>({
      start(controller) {
        slowController = controller
      },
    })
    const requested: string[] = []
    Object.defineProperty(globalThis, 'fetch', {
      value: async (path: string) => {
        requested.push(path)
        if (path === '/api/communications/inbox') {
          return new Response(slowBody, {
            status: scenario.status,
            headers: { 'content-type': scenario.contentType },
          })
        }
        assert.equal(path, '/api/auth/me')
        return new Response(JSON.stringify({ user: accountB }), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        })
      },
      configurable: true,
    })

    const oldRequest = api.apiGet('/api/communications/inbox')
    await new Promise<void>((resolve) => setImmediate(resolve))
    assert.deepEqual(requested, ['/api/communications/inbox'])
    local.values.set('bzplat_auth_epoch', `epoch-slow-${scenario.status}-b`)
    await assert.rejects(
      api.apiJson('/api/notification-prefs', 'PUT', { email_match_done: true }),
      api.IdentityChangedError,
    )
    assert.deepEqual(api.currentUserStore.get(), accountB)

    slowController?.enqueue(new TextEncoder().encode(scenario.body))
    slowController?.close()
    await assert.rejects(oldRequest, api.IdentityChangedError)
    assert.deepEqual(api.currentUserStore.get(), accountB)
    assert.equal(api.lastSafeApiFailure(), null)
    assert.deepEqual(requested, ['/api/communications/inbox', '/api/auth/me'])
  })
}

test('XHR upload response processing rejects an account-switched projection', async () => {
  const local = new MemoryStorage()
  local.values.set('bzplat_auth_epoch', 'epoch-xhr-a')
  Object.defineProperty(globalThis, 'localStorage', { value: local, configurable: true })
  Object.defineProperty(globalThis, 'sessionStorage', { value: new MemoryStorage(), configurable: true })
  Object.defineProperty(globalThis, 'location', {
    value: { hash: '#/my-bots', protocol: 'https:', host: 'example.test' },
    configurable: true,
  })

  class FakeEventTarget {
    listeners = new Map<string, Array<() => void>>()
    addEventListener(type: string, listener: () => void): void {
      const current = this.listeners.get(type) || []
      current.push(listener)
      this.listeners.set(type, current)
    }
    emit(type: string): void {
      for (const listener of this.listeners.get(type) || []) listener()
    }
  }

  class FakeXMLHttpRequest extends FakeEventTarget {
    static latest: FakeXMLHttpRequest | null = null
    upload = new FakeEventTarget()
    status = 200
    statusText = 'OK'
    responseText = JSON.stringify({ bot: { id: 999, owner_id: 91 } })
    withCredentials = false
    sent = false
    constructor() {
      super()
      FakeXMLHttpRequest.latest = this
    }
    open(): void {}
    send(): void { this.sent = true }
    abort(): void { this.emit('abort') }
    getResponseHeader(name: string): string | null {
      return name.toLowerCase() === 'content-type' ? 'application/json' : null
    }
  }
  Object.defineProperty(globalThis, 'XMLHttpRequest', {
    value: FakeXMLHttpRequest,
    configurable: true,
  })

  const api = await import('../src/api.ts?xhr-account-binding-test')
  const accountA: api.CurrentUser = {
    id: 91,
    username: 'xhr-a',
    email: 'xhr-a@example.test',
    role: 'user',
    display_name: 'XHR A',
    is_active: 1,
  }
  const accountB: api.CurrentUser = {
    ...accountA,
    id: 92,
    username: 'xhr-b',
    email: 'xhr-b@example.test',
    display_name: 'XHR B',
  }
  api.currentUserStore.set(accountA)
  Object.defineProperty(globalThis, 'fetch', {
    value: async (path: string) => {
      assert.equal(path, '/api/auth/me')
      return new Response(JSON.stringify({ user: accountB }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      })
    },
    configurable: true,
  })

  const upload = api.apiFormWithProgress('/api/bots', { file: new Blob(['elf']) })
  await new Promise<void>((resolve) => setImmediate(resolve))
  assert.equal(FakeXMLHttpRequest.latest?.sent, true)

  local.values.set('bzplat_auth_epoch', 'epoch-xhr-b')
  FakeXMLHttpRequest.latest?.emit('load')
  await assert.rejects(upload, api.IdentityChangedError)
  assert.deepEqual(api.currentUserStore.get(), accountB)
})

test('logout navigation is success-only and password rotation uses its confirmed invalidation', async () => {
  const [shellSource, settingsSource] = await Promise.all([
    readFile(new URL('../src/components/shell/app-shell.tsx', import.meta.url), 'utf8'),
    readFile(new URL('../src/pages/Settings.tsx', import.meta.url), 'utf8'),
  ])

  assert.match(shellSource, /try\s*{[\s\S]*await logout\(\)[\s\S]*nav\('\/'\)[\s\S]*}\s*catch[\s\S]*toast\.error/)
  assert.match(settingsSource, /confirmServerInvalidatedSession\(\)[\s\S]*navigate\('\/login'/)
  assert.doesNotMatch(settingsSource, /changePassword[\s\S]*await logout\(\)/)
})
