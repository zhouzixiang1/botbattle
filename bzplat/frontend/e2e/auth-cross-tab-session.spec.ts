import { expect, test, type BrowserContext, type Route } from '@playwright/test'

const ACCOUNT_A = {
  id: 7_101,
  username: 'cross_tab_a',
  email: 'cross-tab-a@example.test',
  role: 'user' as const,
  display_name: 'Cross Tab A',
  is_active: 1,
  email_verified: 1,
}

const ACCOUNT_B = {
  ...ACCOUNT_A,
  id: 7_102,
  username: 'cross_tab_b',
  email: 'cross-tab-b@example.test',
  display_name: 'Cross Tab B',
}

const EMPTY_PREFS = {
  email_match_done: false,
  email_followed: false,
  email_contest: false,
  email_comment: false,
}

interface SlowLoginRaceWindow extends Window {
  __slowLoginHeadersReady?: boolean
  __releaseSlowLoginBody?: () => void
  __slowLoginResult?: Promise<{
    status: 'resolved' | 'rejected'
    errorName?: string
    projectedUserId: number | null
  }>
}

interface LateMeFailureWindow extends Window {
  __holdInitialMe?: boolean
  __lateMeRejectors?: Array<(cause?: unknown) => void>
}

async function installInitialSession(context: BrowserContext, baseURL: string) {
  await context.addCookies([{
    name: 'bz_session',
    value: 'account-a-session',
    url: baseURL,
    httpOnly: true,
    sameSite: 'Lax',
  }])
}

function json(route: Route, body: unknown, status = 200, headers: Record<string, string> = {}) {
  return route.fulfill({
    status,
    headers: { 'content-type': 'application/json', ...headers },
    body: JSON.stringify(body),
  })
}

test('failed logout preserves identity and navigation until a 2xx confirmation', async ({
  context,
  page,
  baseURL,
}) => {
  await installInitialSession(context, baseURL!)
  let logoutStatus = 500
  await context.route('**/api/**', async (route) => {
    const request = route.request()
    const pathname = new URL(request.url()).pathname
    if (pathname === '/api/auth/me') return json(route, { user: ACCOUNT_A })
    if (pathname === '/api/auth/logout') {
      if (logoutStatus !== 204) return json(route, { detail: 'temporary logout failure' }, logoutStatus)
      return route.fulfill({
        status: 204,
        headers: { 'set-cookie': 'bz_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax' },
      })
    }
    if (pathname === '/api/notification-prefs') return json(route, { prefs: EMPTY_PREFS })
    if (pathname === '/api/auth/me/favorites') return json(route, { favorites: [] })
    if (pathname === '/api/notifications/unread-count') return json(route, { count: 0 })
    return json(route, {})
  })

  await page.goto('/#/settings')
  await expect(page.locator('#settings-display')).toHaveValue(ACCOUNT_A.display_name)

  await page.getByRole('button', { name: '登出', exact: true }).click()
  await expect(page).toHaveURL(/#\/settings$/)
  await expect(page.locator('#settings-display')).toHaveValue(ACCOUNT_A.display_name)
  await expect(page.getByText('temporary logout failure', { exact: true })).toBeVisible()

  logoutStatus = 204
  await page.getByRole('button', { name: '登出', exact: true }).click()
  await expect(page).toHaveURL(/\/#\/$/)
  await expect(page.getByRole('link', { name: '登录', exact: true }).first()).toBeVisible()
  const epoch = await page.evaluate(() => localStorage.getItem('bzplat_auth_epoch') || '')
  expect(epoch).not.toBe('')
  expect(epoch).not.toContain(ACCOUNT_A.username)
  expect(epoch).not.toContain(ACCOUNT_A.email)
})

test('two tabs reconcile the shared cookie identity before private projection and writes', async ({
  context,
  baseURL,
}) => {
  await installInitialSession(context, baseURL!)
  const tabA = await context.newPage()
  const tabB = await context.newPage()
  let switchedToB = false
  const accountBEvents: string[] = []
  const profileWrites: Array<{ cookie: string; body: unknown }> = []

  await context.route('**/api/**', async (route) => {
    const request = route.request()
    const pathname = new URL(request.url()).pathname
    const requestPage = request.frame().page()
    if (pathname === '/api/auth/me') {
      if (switchedToB && requestPage === tabA) accountBEvents.push('tab-a:/api/auth/me')
      return json(route, { user: switchedToB ? ACCOUNT_B : ACCOUNT_A })
    }
    if (pathname === '/api/auth/captcha') {
      return json(route, { captcha_id: 'captcha-cross-tab', image_base64: '' })
    }
    if (pathname === '/api/auth/login') {
      const payload = request.postDataJSON() as { username?: string }
      expect(payload.username).toBe(ACCOUNT_B.username)
      switchedToB = true
      accountBEvents.push('tab-b:/api/auth/login')
      // Playwright WebKit intentionally does not apply Set-Cookie from a
      // route.fulfill() mock.  Model the browser having accepted the server's
      // HttpOnly response cookie before the 2xx login body is released.
      await context.addCookies([{
        name: 'bz_session',
        value: 'account-b-session',
        url: baseURL!,
        httpOnly: true,
        sameSite: 'Lax',
      }])
      return json(route, { user: ACCOUNT_B, token: 'response-token-not-persisted' })
    }
    if (pathname === '/api/auth/profile' && request.method() === 'PUT') {
      accountBEvents.push('tab-a:/api/auth/profile')
      const cookie = (await context.cookies(baseURL!))
        .find(({ name }) => name === 'bz_session')
      profileWrites.push({
        cookie: cookie ? `${cookie.name}=${cookie.value}` : '',
        body: request.postDataJSON(),
      })
      return json(route, { ok: true })
    }
    if (pathname === '/api/notification-prefs') return json(route, { prefs: EMPTY_PREFS })
    if (pathname === '/api/auth/me/favorites') return json(route, { favorites: [] })
    if (pathname === '/api/notifications/unread-count') return json(route, { count: 0 })
    return json(route, {})
  })

  await tabA.goto('/#/settings')
  await tabB.goto('/#/login')
  await expect(tabA.locator('#settings-display')).toHaveValue(ACCOUNT_A.display_name)
  await tabB.locator('#login-username').fill(ACCOUNT_B.username)
  await tabB.locator('#login-password').fill('password-for-mocked-login')
  await tabB.getByPlaceholder('图中字符或算式结果').fill('skip')
  await tabB.getByRole('button', { name: '登录', exact: true }).click()
  await expect(tabB).toHaveURL(/\/#\/$/)

  // The storage/BroadcastChannel payload is only a nonce. Tab A drops account
  // A immediately, then obtains B from /me before rendering another private UI.
  await expect(tabA.locator('#settings-display')).toHaveValue(ACCOUNT_B.display_name)
  await expect(tabA.getByText(`@${ACCOUNT_B.username}`, { exact: true }).first()).toBeVisible()
  expect(accountBEvents).toContain('tab-a:/api/auth/me')
  const epoch = await tabA.evaluate(() => localStorage.getItem('bzplat_auth_epoch') || '')
  expect(epoch).not.toContain(ACCOUNT_A.username)
  expect(epoch).not.toContain(ACCOUNT_B.username)
  expect(epoch).not.toContain(ACCOUNT_A.email)
  expect(epoch).not.toContain(ACCOUNT_B.email)

  await tabA.locator('#settings-display').fill('Account B Updated')
  await tabA.getByRole('button', { name: '保存资料', exact: true }).click()
  await expect(tabA.getByText('资料已保存', { exact: true })).toBeVisible()
  expect(profileWrites).toHaveLength(1)
  expect(profileWrites[0].cookie).toContain('bz_session=account-b-session')
  expect(profileWrites[0].body).toMatchObject({ display_name: 'Account B Updated' })
  expect(accountBEvents.indexOf('tab-a:/api/auth/me')).toBeLessThan(
    accountBEvents.indexOf('tab-a:/api/auth/profile'),
  )

  const persisted = await context.storageState()
  expect(persisted.origins.flatMap((origin) => origin.localStorage).filter(
    ({ name }) => name === 'bzplat_token' || name === 'bzplat_user',
  )).toEqual([])
})

test('a streamed earlier login body cannot overwrite a later HttpOnly-cookie login', async ({
  context,
  baseURL,
}) => {
  await installInitialSession(context, baseURL!)
  const tabA = await context.newPage()
  const tabB = await context.newPage()
  let switchedToB = false
  const privateWriteCookies: string[] = []

  await context.route('**/api/**', async (route) => {
    const request = route.request()
    const pathname = new URL(request.url()).pathname
    if (pathname === '/api/auth/me') {
      return json(route, { user: switchedToB ? ACCOUNT_B : ACCOUNT_A })
    }
    if (pathname === '/api/auth/login') {
      const payload = request.postDataJSON() as { username?: string }
      expect(payload.username).toBe(ACCOUNT_B.username)
      await context.addCookies([{
        name: 'bz_session',
        value: 'account-b-session',
        url: baseURL!,
        httpOnly: true,
        sameSite: 'Lax',
      }])
      switchedToB = true
      return json(route, { user: ACCOUNT_B, token: 'response-token-not-persisted' })
    }
    if (pathname === '/api/auth/profile' && request.method() === 'PUT') {
      // WebKit hides Cookie from the intercepted request headers. Read the
      // same BrowserContext cookie jar used by the browser instead.
      const cookie = (await context.cookies(baseURL!))
        .find(({ name }) => name === 'bz_session')
      privateWriteCookies.push(cookie ? `${cookie.name}=${cookie.value}` : '')
      return json(route, { actor_id: ACCOUNT_B.id })
    }
    if (pathname === '/api/notification-prefs') return json(route, { prefs: EMPTY_PREFS })
    if (pathname === '/api/auth/me/favorites') return json(route, { favorites: [] })
    if (pathname === '/api/notifications/unread-count') return json(route, { count: 0 })
    return json(route, {})
  })

  await Promise.all([tabA.goto('/#/login'), tabB.goto('/#/login')])
  await expect.poll(async () => tabA.evaluate(async () => {
    const apiPath = '/src/api.ts'
    const api = await import(apiPath) as typeof import('../src/api')
    return api.currentUserStore.get()?.id ?? null
  })).toBe(ACCOUNT_A.id)
  await expect.poll(async () => tabB.evaluate(async () => {
    const apiPath = '/src/api.ts'
    const api = await import(apiPath) as typeof import('../src/api')
    return api.currentUserStore.get()?.id ?? null
  })).toBe(ACCOUNT_A.id)

  await tabA.evaluate((accountA) => {
    const raceWindow = window as SlowLoginRaceWindow
    const nativeFetch = window.fetch.bind(window)
    window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const requestUrl = typeof input === 'string'
        ? new URL(input, location.href)
        : input instanceof URL
          ? input
          : new URL(input.url)
      if (requestUrl.pathname !== '/api/auth/login') return nativeFetch(input, init)

      const payload = JSON.parse(String(init?.body)) as { username?: string }
      if (payload.username !== accountA.username) return nativeFetch(input, init)
      let controller: ReadableStreamDefaultController<Uint8Array> | undefined
      const body = new ReadableStream<Uint8Array>({
        start(nextController) {
          controller = nextController
        },
      })
      raceWindow.__releaseSlowLoginBody = () => {
        controller?.enqueue(new TextEncoder().encode(JSON.stringify({ user: accountA })))
        controller?.close()
      }
      raceWindow.__slowLoginHeadersReady = true
      return new Response(body, {
        status: 200,
        headers: { 'content-type': 'application/json' },
      })
    }

    const start = async () => {
      const apiPath = '/src/api.ts'
      const api = await import(apiPath) as typeof import('../src/api')
      try {
        const result = await api.apiPost<{ user: typeof accountA }>('/api/auth/login', 'POST', {
          username: accountA.username,
          password: 'mock-password-a',
          captcha_id: 'mock-a',
          captcha_answer: 'mock-a',
        })
        api.confirmAuthenticatedSession(result.user)
        return {
          status: 'resolved' as const,
          projectedUserId: api.currentUserStore.get()?.id ?? null,
        }
      } catch (cause) {
        return {
          status: 'rejected' as const,
          errorName: cause instanceof Error ? cause.name : 'unknown',
          projectedUserId: api.currentUserStore.get()?.id ?? null,
        }
      }
    }
    raceWindow.__slowLoginResult = start()
  }, ACCOUNT_A)

  await tabA.waitForFunction(() => (
    window as SlowLoginRaceWindow
  ).__slowLoginHeadersReady === true)
  const firstHeaderState = await tabA.evaluate(async () => {
    const apiPath = '/src/api.ts'
    const api = await import(apiPath) as typeof import('../src/api')
    return {
      projectedUserId: api.currentUserStore.get()?.id ?? null,
      epoch: localStorage.getItem('bzplat_auth_epoch') || '',
    }
  })
  expect(firstHeaderState.projectedUserId).toBeNull()
  expect(firstHeaderState.epoch).not.toBe('')

  const laterLogin = await tabB.evaluate(async (accountB) => {
    const apiPath = '/src/api.ts'
    const api = await import(apiPath) as typeof import('../src/api')
    const result = await api.apiPost<{ user: typeof accountB }>('/api/auth/login', 'POST', {
      username: accountB.username,
      password: 'mock-password-b',
      captcha_id: 'mock-b',
      captcha_answer: 'mock-b',
    })
    api.confirmAuthenticatedSession(result.user)
    return api.currentUserStore.get()?.id ?? null
  }, ACCOUNT_B)
  expect(laterLogin).toBe(ACCOUNT_B.id)

  await expect.poll(async () => tabA.evaluate(async () => {
    const apiPath = '/src/api.ts'
    const api = await import(apiPath) as typeof import('../src/api')
    return api.currentUserStore.get()?.id ?? null
  })).toBe(ACCOUNT_B.id)

  await tabA.evaluate(() => (
    window as SlowLoginRaceWindow
  ).__releaseSlowLoginBody?.())
  const earlierLogin = await tabA.evaluate(async () => {
    const result = await (window as SlowLoginRaceWindow).__slowLoginResult
    if (!result) throw new Error('slow login result was not installed')
    return result
  })
  expect(earlierLogin).toEqual({
    status: 'rejected',
    errorName: 'IdentityChangedError',
    projectedUserId: ACCOUNT_B.id,
  })

  const privateWrite = await tabA.evaluate(async () => {
    const apiPath = '/src/api.ts'
    const api = await import(apiPath) as typeof import('../src/api')
    return api.apiJson<{ actor_id: number }>('/api/auth/profile', 'PUT', {
      display_name: 'Account B Still Active',
    })
  })
  expect(privateWrite.actor_id).toBe(ACCOUNT_B.id)
  expect(privateWriteCookies).toEqual([expect.stringContaining('bz_session=account-b-session')])
})

test('late initial me network and abort failures cannot clear a newer cross-tab identity', async ({
  context,
  baseURL,
}) => {
  await installInitialSession(context, baseURL!)
  const tabA = await context.newPage()
  const tabB = await context.newPage()
  let switchedToB = false
  let failNextCurrentMe = false

  await tabA.addInitScript(() => {
    const raceWindow = window as LateMeFailureWindow
    const nativeFetch = window.fetch.bind(window)
    raceWindow.__holdInitialMe = true
    raceWindow.__lateMeRejectors = []
    window.fetch = (input: RequestInfo | URL, init?: RequestInit) => {
      const requestUrl = typeof input === 'string'
        ? new URL(input, location.href)
        : input instanceof URL
          ? input
          : new URL(input.url)
      if (requestUrl.pathname !== '/api/auth/me' || !raceWindow.__holdInitialMe) {
        return nativeFetch(input, init)
      }
      return new Promise<Response>((_resolve, reject) => {
        raceWindow.__lateMeRejectors?.push(reject)
      })
    }
  })

  await context.route('**/api/**', async (route) => {
    const request = route.request()
    const pathname = new URL(request.url()).pathname
    if (pathname === '/api/auth/me') {
      if (failNextCurrentMe) {
        failNextCurrentMe = false
        return route.abort('failed')
      }
      return json(route, { user: switchedToB ? ACCOUNT_B : ACCOUNT_A })
    }
    if (pathname === '/api/auth/captcha') {
      return json(route, { captcha_id: 'captcha-late-me', image_base64: '' })
    }
    if (pathname === '/api/auth/login') {
      switchedToB = true
      await context.addCookies([{
        name: 'bz_session',
        value: 'account-b-session',
        url: baseURL!,
        httpOnly: true,
        sameSite: 'Lax',
      }])
      return json(route, { user: ACCOUNT_B })
    }
    if (pathname === '/api/auth/profile' && request.method() === 'PUT') {
      failNextCurrentMe = true
      return json(route, { ok: true })
    }
    if (pathname === '/api/notification-prefs') return json(route, { prefs: EMPTY_PREFS })
    if (pathname === '/api/auth/me/favorites') return json(route, { favorites: [] })
    if (pathname === '/api/notifications/unread-count') return json(route, { count: 0 })
    return json(route, {})
  })

  await tabA.goto('/#/settings')
  // React StrictMode runs the initial effect twice in the Vite QA build. Hold
  // both probes so the later failure exercises the newest refresh generation.
  await expect.poll(() => tabA.evaluate(() => (
    window as LateMeFailureWindow
  ).__lateMeRejectors?.length ?? 0)).toBe(2)
  await tabA.evaluate(() => {
    (window as LateMeFailureWindow).__holdInitialMe = false
  })
  await tabB.goto('/#/login')
  await tabB.locator('#login-username').fill(ACCOUNT_B.username)
  await tabB.locator('#login-password').fill('password-for-mocked-login')
  await tabB.getByPlaceholder('图中字符或算式结果').fill('skip')
  await tabB.getByRole('button', { name: '登录', exact: true }).click()

  const projectedUserId = async () => tabA.evaluate(async () => {
    const apiPath = '/src/api.ts'
    const api = await import(apiPath) as typeof import('../src/api')
    return api.currentUserStore.get()?.id ?? null
  })
  await expect.poll(projectedUserId).toBe(ACCOUNT_B.id)

  await tabA.evaluate(() => {
    const rejectors = (window as LateMeFailureWindow).__lateMeRejectors || []
    rejectors[0]?.(new TypeError('late network failure'))
    rejectors[1]?.(new DOMException('late request aborted', 'AbortError'))
  })
  await tabA.waitForTimeout(100)
  expect(await projectedUserId()).toBe(ACCOUNT_B.id)
  const cookie = (await context.cookies(baseURL!)).find(({ name }) => name === 'bz_session')
  expect(cookie?.value).toBe('account-b-session')

  // A failure from a refresh that still owns both generations keeps the
  // existing fail-closed behavior: only superseded failures are ignored.
  await tabA.locator('#settings-display').fill('Trigger Current Refresh')
  await tabA.getByRole('button', { name: '保存资料', exact: true }).click()
  await expect.poll(projectedUserId).toBeNull()
})
