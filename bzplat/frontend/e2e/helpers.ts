import {
  expect,
  type Locator,
  type Page,
  type Request,
  type Response,
  type Route,
} from '@playwright/test'

export const PASSWORD = process.env.BZ_E2E_PASSWORD || 'Test1234'
const QA_CAPTCHA_IMAGE = 'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=='

type BrowserIssue =
  | { kind: 'pageerror'; message: string }
  | { kind: 'console-error' | 'console-warning'; message: string }
  | { kind: 'http'; method: string; status: number; url: string }
  | { kind: 'requestfailed'; method: string; url: string; errorText: string }

export type ExpectedBrowserIssue =
  | {
      kind: 'console-warning'
      messageIncludes: string
      optional?: boolean
    }
  | {
      kind: 'http'
      method: string
      status: number
      pathname: string
      search?: string
      optional?: boolean
    }
  | {
      kind: 'requestfailed'
      method: string
      pathname: string
      search?: string
      errorText: string
      optional?: boolean
    }

export interface BrowserMonitor {
  settle: () => Promise<void>
  expectClean: (expected?: readonly ExpectedBrowserIssue[]) => Promise<void>
}

function isRealtimeRequest(request: Request): boolean {
  return request.resourceType() === 'eventsource' || request.resourceType() === 'websocket'
}

function issueText(issue: BrowserIssue): string {
  if (issue.kind === 'pageerror') return `pageerror: ${issue.message}`
  if (issue.kind === 'console-error' || issue.kind === 'console-warning') {
    return `${issue.kind}: ${issue.message}`
  }
  if (issue.kind === 'http') {
    return `${issue.status} ${issue.method} ${issue.url}`
  }
  return `requestfailed: ${issue.method} ${issue.url} ${issue.errorText}`
}

function matchesExpected(issue: BrowserIssue, expected: ExpectedBrowserIssue): boolean {
  if (issue.kind !== expected.kind) return false
  if (issue.kind === 'console-warning' && expected.kind === 'console-warning') {
    return issue.message.includes(expected.messageIncludes)
  }
  if (issue.kind === 'http' && expected.kind === 'http') {
    const url = new URL(issue.url)
    return issue.method === expected.method &&
      issue.status === expected.status &&
      url.pathname === expected.pathname &&
      url.search === (expected.search || '')
  }
  if (issue.kind === 'requestfailed' && expected.kind === 'requestfailed') {
    const url = new URL(issue.url)
    return issue.method === expected.method &&
      issue.errorText === expected.errorText &&
      url.pathname === expected.pathname &&
      url.search === (expected.search || '')
  }
  return false
}

/**
 * Collects browser evidence that a visual assertion alone would miss.
 * The initial anonymous `/api/auth/me` 401 is the AuthProvider's expected probe;
 * every other failed ordinary-HTTP response/request and every JS error fails the
 * test. EventSource/WebSocket lifetimes are asserted by their dedicated tests.
 */
export function monitorBrowser(page: Page): BrowserMonitor {
  const issues: BrowserIssue[] = []
  const pending = new Set<Request>()
  let activity = 0
  let applicationOrigin: string | undefined

  const tracksPending = (request: Request) => {
    if (isRealtimeRequest(request)) return false

    const currentUrl = page.url()
    if (!applicationOrigin && /^https?:\/\//.test(currentUrl)) {
      applicationOrigin = new URL(currentUrl).origin
    }
    // During the first document request page.url() can still be about:blank.
    // That top-level navigation establishes the application origin for all later
    // API/static requests without treating third-party resources as app traffic.
    if (
      !applicationOrigin &&
      request.isNavigationRequest() &&
      request.frame() === page.mainFrame()
    ) {
      applicationOrigin = new URL(request.url()).origin
    }
    return applicationOrigin === new URL(request.url()).origin
  }

  page.on('request', (request) => {
    if (!tracksPending(request)) return
    activity += 1
    pending.add(request)
  })
  page.on('requestfinished', (request) => {
    if (!tracksPending(request)) return
    if (!pending.delete(request)) return
    activity += 1
  })
  page.on('pageerror', (error) => issues.push({ kind: 'pageerror', message: error.message }))
  page.on('requestfailed', (request) => {
    if (isRealtimeRequest(request)) return
    if (tracksPending(request)) activity += 1
    pending.delete(request)
    issues.push({
      kind: 'requestfailed',
      method: request.method(),
      url: request.url(),
      errorText: request.failure()?.errorText || '',
    })
  })
  page.on('response', (response) => {
    if (isRealtimeRequest(response.request())) return
    // A normal HTTP response is sufficient for the network/status audit and the
    // page-specific DOM assertion proves its body was consumed.  Chromium can keep
    // a fetch's requestfinished event pending across hash back/forward navigation
    // even after the complete 200 response has arrived; waiting for that browser
    // bookkeeping made otherwise finished same-origin requests hang settle().
    if (pending.delete(response.request())) activity += 1
    if (response.status() < 400) return
    const url = new URL(response.url())
    const expectedAnonymousProbe =
      response.status() === 401 &&
      response.request().method() === 'GET' &&
      url.pathname === '/api/auth/me' &&
      url.search === ''
    if (!expectedAnonymousProbe) {
      issues.push({
        kind: 'http',
        method: response.request().method(),
        status: response.status(),
        url: response.url(),
      })
    }
  })
  page.on('console', (message) => {
    if (message.type() !== 'error' && message.type() !== 'warning') return
    // Chromium's generic resource line omits the request URL. The response and
    // requestfailed listeners above preserve method, URL and status/error instead,
    // so retaining this duplicate would make an exact endpoint whitelist impossible.
    if (/^Failed to load resource: (?:the server responded with a status of \d+.*|net::ERR_[A-Z_]+)$/.test(message.text())) return
    issues.push({ kind: `console-${message.type()}`, message: message.text() })
  })

  const settle = async () => {
    // Require a quiet window for same-origin app traffic, rather than only observing
    // pending.size === 0 once: React effects can enqueue another fetch immediately
    // after the prior response. Cross-origin resources still produce diagnostics in
    // the listeners above, but a slow third-party font cannot block this app settle.
    const deadline = Date.now() + 10_000
    while (Date.now() < deadline) {
      await expect.poll(
        () => pending.size,
        {
          message: `ordinary HTTP still pending:\n${[...pending].map((r) => `${r.method()} ${r.url()}`).join('\n')}`,
          timeout: Math.max(1, deadline - Date.now()),
          intervals: [25, 50, 100, 250],
        },
      ).toBe(0)
      const activityBeforeQuietWindow = activity
      await page.waitForTimeout(100)
      if (pending.size === 0 && activity === activityBeforeQuietWindow) return
    }
    throw new Error(
      `ordinary HTTP did not settle:\n${[...pending].map((r) => `${r.method()} ${r.url()}`).join('\n')}`,
    )
  }

  return {
    settle,
    expectClean: async (expected = []) => {
      await settle()
      const unmatched = [...issues]
      const missing: ExpectedBrowserIssue[] = []
      for (const allowed of expected) {
        const index = unmatched.findIndex((issue) => matchesExpected(issue, allowed))
        if (index === -1) {
          if (!allowed.optional) missing.push(allowed)
        }
        else unmatched.splice(index, 1)
      }
      const evidence = [
        ...unmatched.map((issue) => `unexpected: ${issueText(issue)}`),
        ...missing.map((issue) => `missing expected issue: ${JSON.stringify(issue)}`),
      ]
      expect(evidence, evidence.join('\n')).toEqual([])
    },
  }
}

export async function loginThroughUi(
  page: Page,
  username: string,
  password = PASSWORD,
): Promise<Response> {
  const captchaPattern = '**/api/auth/captcha'
  const fulfillQaCaptcha = async (route: Route) => {
    const request = route.request()
    const url = new URL(request.url())
    if (
      request.method() !== 'GET' ||
      url.pathname !== '/api/auth/captcha' ||
      url.search !== ''
    ) {
      await route.fallback()
      return
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        captcha_id: 'e2e-skip-captcha',
        image_base64: QA_CAPTCHA_IMAGE,
        ttl: 300,
      }),
    })
  }

  // This helper is only valid against the isolated backend's
  // BZ_SKIP_CAPTCHA=1 capability. Avoid exhausting the production captcha GET
  // budget while keeping the real login POST, cookie rotation and CSRF path.
  // The route is scoped to this login and removed so endpoint-specific tests
  // continue to exercise the real captcha service.
  await page.route(captchaPattern, fulfillQaCaptcha)
  try {
    await page.goto('/#/login')
    await page.locator('#login-username').fill(username)
    await page.locator('#login-password').fill(password)
    await page.getByPlaceholder('图中字符或算式结果').fill('skip')
    const responsePromise = page.waitForResponse(
      (response) =>
        response.request().method() === 'POST' &&
        new URL(response.url()).pathname === '/api/auth/login',
    )
    await page.getByRole('button', { name: '登录', exact: true }).click()
    const response = await responsePromise
    expect(response.status(), await response.text()).toBe(200)
    await expect(page).toHaveURL(/\/#\/$/)
    return response
  } finally {
    await page.unroute(captchaPattern, fulfillQaCaptcha)
  }
}

/**
 * Playwright's context-bound APIRequestContext shares the browser cookie jar,
 * but it does not synthesize the browser Origin header for unsafe requests.
 * Keep direct cookie-authenticated fixture writes subject to the production
 * same-origin CSRF contract by deriving the exact origin from a navigated page.
 */
export function cookieOriginHeaders(page: Page): { Origin: string } {
  const url = new URL(page.url())
  if (url.protocol !== 'http:' && url.protocol !== 'https:') {
    throw new Error(`cookie-authenticated fixture page has no HTTP origin: ${page.url()}`)
  }
  return { Origin: url.origin }
}

export function versionRow(dialog: Locator, version: number) {
  return dialog.getByText(`v${version}`, { exact: true }).locator('xpath=ancestor::li[1]')
}

/**
 * Run cleanup even when the body fails, while retaining both failures if cleanup
 * also fails. A plain `finally` throw would hide the original assertion, which is
 * especially harmful for stateful E2E diagnostics.
 */
export async function withCleanup<T>(
  body: () => Promise<T>,
  cleanup: () => Promise<void>,
): Promise<T> {
  let value: T | undefined
  let bodyFailure: unknown
  try {
    value = await body()
  } catch (error) {
    bodyFailure = error
  }

  let cleanupFailure: unknown
  try {
    await cleanup()
  } catch (error) {
    cleanupFailure = error
  }

  if (bodyFailure && cleanupFailure) {
    throw new AggregateError(
      [bodyFailure, cleanupFailure],
      'E2E body and its fail-closed cleanup both failed',
    )
  }
  if (bodyFailure) throw bodyFailure
  if (cleanupFailure) throw cleanupFailure
  return value as T
}

/** Execute every cleanup in order and report all failures without skipping later entities. */
export async function runCleanupTasks(
  tasks: ReadonlyArray<{ label: string; run: () => Promise<void> }>,
): Promise<void> {
  const failures: unknown[] = []
  for (const task of tasks) {
    try {
      await task.run()
    } catch (error) {
      failures.push(new Error(`cleanup failed: ${task.label}`, { cause: error }))
    }
  }
  if (failures.length) {
    throw new AggregateError(failures, `${failures.length} E2E cleanup task(s) failed`)
  }
}
