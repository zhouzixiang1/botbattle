import { expect, test, type Page } from '@playwright/test'

import { monitorBrowser } from './helpers'

const USER = {
  id: 42,
  username: 'queue_tester',
  email: 'queue_tester@example.test',
  role: 'user',
  display_name: 'Queue Tester',
  is_active: 1,
}

function job(overrides: Record<string, unknown> = {}) {
  return {
    public_id: 'public-job-1',
    source: 'human',
    status: 'running',
    game_id: 'holdem',
    match_type: 'human',
    match_id: 'human-public-match',
    sandbox_units: 1,
    rated: false,
    rating_reason: 'human',
    retryable: false,
    cancel_requested: false,
    reason: '',
    created_at: '2026-08-11T10:00:00Z',
    ...overrides,
  }
}

function queueSnapshot() {
  return {
    dispatcher: {
      state: 'starting',
      accepting: false,
      auto_enabled: true,
      pause_reason: '',
      retry_at: null,
    },
    capacity: {
      match_slots: { used: 1, capacity: 1 },
      sandbox_units: { used: 1, capacity: 2 },
      running_matches: 1,
    },
    active: [{
      ...job(),
      // A defensive UI projection must never render unexpected private fields.
      binary_path: '/private/bot_uploads/secret-bot',
      checksum: 'TOP-SECRET-CHECKSUM',
      owner_name: 'PRIVATE-OWNER-NAME',
    }],
    queued: [job({
      public_id: 'public-job-2',
      source: 'manual',
      status: 'queued',
      game_id: 'pencil',
      match_type: 'manual',
      match_id: null,
      sandbox_units: 2,
      rating_reason: 'same_owner',
    })],
    queued_count: 1,
  }
}

function requestSnapshot(
  status: 'queued' | 'interrupted' | 'cancelled',
  overrides: Record<string, unknown> = {},
  publicId = 'challenge-public-request',
) {
  return {
    public_id: publicId,
    request: job({
      public_id: publicId,
      source: 'manual',
      status,
      match_type: 'manual',
      match_id: null,
      sandbox_units: 2,
      retryable: status === 'interrupted',
      rating_reason: 'eligible',
      reason: status === 'interrupted' ? '平台恢复后需要重新排队' : '',
      ...overrides,
    }),
    ahead_jobs: status === 'queued' ? 2 : 0,
    ahead_sandbox_units: status === 'queued' ? 4 : 0,
    capacity: {
      match_slots: { used: 1, capacity: 1 },
      sandbox_units: { used: 2, capacity: 2 },
      running_matches: 1,
    },
    eta: {
      min_seconds: 10,
      max_seconds: 30,
      dynamic: true as const,
      note: '按当前容量动态估算',
    },
  }
}

async function mockBase(
  page: Page,
  loggedIn: boolean,
  activeUser: Record<string, unknown> = USER,
) {
  const unexpectedBackendRequests: string[] = []
  const forbiddenMainRequests: string[] = []
  page.on('request', (request) => {
    const url = new URL(request.url())
    if (url.port === '50380') forbiddenMainRequests.push(`${request.method()} ${request.url()}`)
  })
  // Keep the matrix deterministic and offline-safe; system fonts are sufficient
  // for these behavior/layout assertions.
  await page.route('https://fonts.googleapis.com/**', async (route) => {
    await route.fulfill({ status: 200, contentType: 'text/css', body: '' })
  })
  // Install the fallback first. Playwright evaluates later routes first, so the
  // feature-specific routes registered by each test take precedence.
  await page.route('**/api/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/notifications/unread-count') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: '{"count":0}' })
      return
    }
    unexpectedBackendRequests.push(`${route.request().method()} ${path}`)
    await route.fulfill({
      status: 418,
      contentType: 'application/json',
      body: JSON.stringify({ detail: `unmocked test endpoint: ${path}` }),
    })
  })
  await page.route('**/api/auth/me', async (route) => {
    await route.fulfill(loggedIn ? {
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ user: activeUser }),
    } : {
      status: 401,
      contentType: 'application/json',
      body: JSON.stringify({ detail: '未登录' }),
    })
  })
  if (loggedIn) {
    await page.addInitScript((user) => {
      localStorage.setItem('bzplat_token', 'queue-test-token')
      localStorage.setItem('bzplat_user', JSON.stringify(user))
    }, activeUser)
  }
  return { unexpectedBackendRequests, forbiddenMainRequests }
}

test('admin seat one picker exposes all public runnable Bots without an owner filter', async ({ page }) => {
  const monitor = monitorBrowser(page)
  const admin = {
    ...USER,
    id: 99,
    username: 'queue_admin',
    email: 'queue_admin@example.test',
    role: 'admin',
  }
  const network = await mockBase(page, true, admin)
  const requests: URL[] = []
  const foreign = {
    id: 501,
    name: 'foreign-seat-one',
    display_name: 'Foreign Seat One',
    owner_id: 77,
    owner_name: 'foreign_owner',
    game_id: 'holdem',
    is_active: 1,
    runnable: true,
  }
  await page.route('**/api/bots/public?**', async (route) => {
    requests.push(new URL(route.request().url()))
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ bots: [foreign], total: 1 }),
    })
  })
  await page.route('**/api/bots/*/versions', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        current_version: 1,
        versions: [{ id: 9001, version: 1, runnable: true }],
      }),
    })
  })

  await page.goto('/#/challenge')
  const seatOneTrigger = page.getByRole('button', {
    name: '选择全站可用 Bot（管理员）',
    exact: true,
  })
  await expect(seatOneTrigger).toBeVisible()
  await seatOneTrigger.click()
  const picker = page.getByRole('dialog', { name: /选择对手/ })
  await expect(picker.getByRole('button', { name: /Foreign Seat One/ })).toBeVisible()
  expect(requests.some((url) => !url.searchParams.has('owner_id'))).toBe(true)
  await picker.getByRole('button', { name: /Foreign Seat One/ }).click()
  await expect(page.locator('main')).toContainText('Foreign Seat One')
  await expect(page.locator('main')).not.toContainText('座位 1 只能使用自己的 Bot')

  expect(network.unexpectedBackendRequests).toEqual([])
  expect(network.forbiddenMainRequests).toEqual([])
  await monitor.expectClean()
})

test('202 challenge request survives refresh, retries, and cancels exactly once', async ({ page }) => {
  test.setTimeout(60_000)
  await page.setViewportSize({ width: 390, height: 844 })
  const monitor = monitorBrowser(page)
  const network = await mockBase(page, true)

  const bots = [
    { id: 101, name: 'alpha', display_name: 'Alpha Bot', owner_id: USER.id, owner_name: USER.username },
    { id: 102, name: 'beta', display_name: 'Beta Bot', owner_id: 77, owner_name: 'opponent' },
  ]
  await page.route('**/api/bots/public?**', async (route) => {
    const ownerId = new URL(route.request().url()).searchParams.get('owner_id')
    const rows = ownerId ? bots.filter((bot) => String(bot.owner_id) === ownerId) : bots
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ bots: rows, total: rows.length }),
    })
  })
  await page.route('**/api/bots/*/versions', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ versions: [], current_version: 1 }),
    })
  })

  let current = requestSnapshot('queued')
  let challengePosts = 0
  let retries = 0
  let deletes = 0
  let delayNextGet = false
  let acceptedId = ''
  await page.route('**/api/matches/challenge', async (route) => {
    expect(route.request().method()).toBe('POST')
    challengePosts += 1
    const body = route.request().postDataJSON() as { request_id?: string }
    expect(body.request_id).toMatch(/^req_[A-Za-z0-9_-]{24}$/)
    acceptedId = body.request_id!
    current = requestSnapshot('queued', {}, acceptedId)
    await route.fulfill({
      status: 202,
      contentType: 'application/json',
      body: JSON.stringify(current),
    })
  })
  await page.route('**/api/execution-requests/**', async (route) => {
    const url = new URL(route.request().url())
    const method = route.request().method()
    if (url.pathname.endsWith('/retry')) {
      expect(method).toBe('POST')
      retries += 1
      current = requestSnapshot('queued', {}, current.public_id)
    } else if (method === 'DELETE') {
      deletes += 1
      current = requestSnapshot('queued', {
        status: 'running',
        match_id: 'cancel-race-match',
        cancel_requested: true,
      }, current.public_id)
    } else {
      expect(method).toBe('GET')
      if (delayNextGet) {
        delayNextGet = false
        await page.waitForTimeout(250)
      }
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(current),
    })
  })

  await page.goto('/#/challenge')
  const botToggle = page.getByRole('button', { name: '选 Bot', exact: true })
  const humanToggle = page.getByRole('button', { name: '我亲自上场', exact: true })
  await expect(botToggle).toHaveAttribute('aria-pressed', 'true')
  await expect(humanToggle).toHaveAttribute('aria-pressed', 'false')
  for (const control of [botToggle, humanToggle]) {
    const box = await control.boundingBox()
    expect(box?.height).toBeGreaterThanOrEqual(44)
  }

  await page.getByRole('button', { name: '选择我的 Bot', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: /Alpha Bot/ }).click()
  await page.getByRole('button', { name: '选择 Bot（搜索 / 我的 / 按用户）', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: /Beta Bot/ }).click()
  await page.getByRole('button', { name: '开始对局', exact: true }).click()

  const card = page.getByTestId('execution-request-card')
  await expect(card).toContainText('排队中')
  expect(challengePosts).toBe(1)
  expect(await page.evaluate(() => Object.entries(sessionStorage).find(
    ([key]) => key.startsWith('bzplat.challenge.execution.'),
  )?.[1])).toBe(acceptedId)

  current = requestSnapshot('interrupted', {}, acceptedId)
  delayNextGet = true
  await page.reload()
  await expect(page.getByTestId('execution-request-recovery')).toBeVisible()
  await expect(card).toContainText('已中断')
  await card.getByRole('button', { name: '重新排队', exact: true }).click()
  await expect(card).toContainText('排队中')
  expect(retries).toBe(1)

  await card.getByRole('button', { name: '取消排队', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: '确认取消', exact: true }).click()
  const cancelling = card.getByRole('button', { name: '正在取消…', exact: true })
  await expect(cancelling).toBeDisabled()
  expect(deletes).toBe(1)
  await expect(page).toHaveURL(/\/#\/challenge$/)
  await expect(card).toContainText('取消请求已记录')
  expect(await page.evaluate(() => Object.entries(sessionStorage).find(
    ([key]) => key.startsWith('bzplat.challenge.execution.'),
  )?.[1])).toBe(acceptedId)

  current = requestSnapshot('cancelled', {}, acceptedId)
  await expect(card).toContainText('请求已取消')
  await expect(card.getByRole('button', { name: '取消排队', exact: true })).toHaveCount(0)
  await expect(card.getByRole('button', { name: '发起新请求', exact: true })).toBeVisible()
  expect(deletes).toBe(1)

  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)
  expect(overflow).toBeLessThanOrEqual(1)
  expect(network.unexpectedBackendRequests).toEqual([])
  expect(network.forbiddenMainRequests).toEqual([])
  await monitor.expectClean()
})

test('lost POST response recovers the committed request by its pre-persisted id', async ({ page }) => {
  test.setTimeout(60_000)
  const monitor = monitorBrowser(page)
  const network = await mockBase(page, true)
  const bots = [
    { id: 201, name: 'loss-alpha', display_name: 'Loss Alpha', owner_id: USER.id, owner_name: USER.username },
    { id: 202, name: 'loss-beta', display_name: 'Loss Beta', owner_id: 77, owner_name: 'opponent' },
  ]
  await page.route('**/api/bots/public?**', async (route) => {
    const ownerId = new URL(route.request().url()).searchParams.get('owner_id')
    const rows = ownerId ? bots.filter((bot) => String(bot.owner_id) === ownerId) : bots
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ bots: rows, total: rows.length }),
    })
  })
  await page.route('**/api/bots/*/versions', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ versions: [], current_version: 1 }),
    })
  })

  let committedId = ''
  let challengePosts = 0
  let detailGets = 0
  await page.route('**/api/matches/challenge', async (route) => {
    challengePosts += 1
    const body = route.request().postDataJSON() as { request_id?: string }
    expect(body.request_id).toMatch(/^req_[A-Za-z0-9_-]{24}$/)
    committedId = body.request_id!
    // Model a reverse proxy losing the successful upstream response after the
    // durable request has committed. The browser sees an ambiguous 5xx only.
    await route.fulfill({
      status: 504,
      contentType: 'application/json',
      body: JSON.stringify({ detail: 'upstream response lost after commit' }),
    })
  })
  await page.route('**/api/execution-requests/**', async (route) => {
    expect(route.request().method()).toBe('GET')
    const publicId = decodeURIComponent(new URL(route.request().url()).pathname.split('/').at(-1) || '')
    expect(publicId).toBe(committedId)
    detailGets += 1
    if (detailGets === 1) {
      // The first visibility read may race the commit/replica boundary; it must
      // not make the client discard the already-persisted id.
      await route.fulfill({
        status: 404,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'not visible yet' }),
      })
      return
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(requestSnapshot('queued', {}, committedId)),
    })
  })

  await page.goto('/#/challenge')
  await page.getByRole('button', { name: '选择我的 Bot', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: /Loss Alpha/ }).click()
  await page.getByRole('button', { name: '选择 Bot（搜索 / 我的 / 按用户）', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: /Loss Beta/ }).click()
  await page.getByRole('button', { name: '开始对局', exact: true }).click()

  const recovery = page.getByTestId('execution-request-recovery')
  const alert = recovery.locator('[role="alert"][tabindex="-1"]')
  await expect(recovery.getByRole('alert')).toHaveCount(1)
  await expect(alert).toContainText('正在确认受理状态')
  await expect(alert).toBeFocused()
  await expect(page.getByTestId('execution-request-card')).toContainText('排队中', { timeout: 12_000 })
  expect(challengePosts).toBe(1)
  expect(detailGets).toBeGreaterThanOrEqual(2)
  expect(await page.evaluate(() => Object.entries(sessionStorage).find(
    ([key]) => key.startsWith('bzplat.challenge.execution.'),
  )?.[1])).toBe(committedId)
  await page.waitForTimeout(500)
  expect(challengePosts).toBe(1)

  expect(network.unexpectedBackendRequests).toEqual([])
  expect(network.forbiddenMainRequests).toEqual([])
  await monitor.expectClean([
    {
      kind: 'http',
      method: 'POST',
      status: 504,
      pathname: '/api/matches/challenge',
    },
    {
      kind: 'http',
      method: 'GET',
      status: 404,
      pathname: `/api/execution-requests/${committedId}`,
    },
  ])
})

test('public queue keeps stale data private and recovers from slow/error/offline at 390px', async ({ page, context }) => {
  test.setTimeout(60_000)
  await page.setViewportSize({ width: 390, height: 844 })
  const monitor = monitorBrowser(page)
  const network = await mockBase(page, false)

  await page.route('**/api/leaderboard?**', async (route) => {
    const gameId = new URL(route.request().url()).searchParams.get('game_id') || 'holdem'
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        leaderboard: [],
        game_id: gameId,
        ranking_min_matches: 10,
        summary: { total: 0, eligible: 0, sample: 0, last_rated_at: null },
        page: 1,
        per_page: 50,
        total: 0,
      }),
    })
  })

  let calls = 0
  let inFlight = 0
  let maxInFlight = 0
  await page.route('**/api/execution-queue', async (route) => {
    calls += 1
    inFlight += 1
    maxInFlight = Math.max(maxInFlight, inFlight)
    try {
      if (calls === 2) {
        await page.waitForTimeout(3_500)
        await route.fulfill({
          status: 503,
          contentType: 'application/json',
          body: JSON.stringify({ detail: 'temporary queue outage' }),
        })
        return
      }
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(queueSnapshot()),
      })
    } finally {
      inFlight -= 1
    }
  })

  await page.goto('/#/leaderboard')
  const panel = page.getByTestId('execution-queue-panel')
  await expect(panel).toBeVisible()
  const details = panel.locator('details')
  await expect(details).not.toHaveAttribute('open', '')
  const summary = panel.locator('summary')
  expect((await summary.boundingBox())?.height).toBeGreaterThanOrEqual(44)
  await summary.click()
  await expect(panel).toContainText('正在启动')
  const timestamp = panel.getByText(/^上次更新：/)
  await expect(timestamp).toBeVisible()
  expect(await timestamp.evaluate((element) => element.closest('[aria-live]'))).toBeNull()
  expect((await panel.locator('[aria-live]').allTextContents()).join(' ')).not.toContain('上次更新')
  await expect(panel.getByRole('link', { name: '进入观赛' })).toHaveAttribute(
    'href',
    '#/match/human-public-match',
  )
  await expect(panel).toContainText('人机对战不计评分')
  await expect(panel).toContainText('同一所有者，不计评分')
  await expect(page.getByText('/private/bot_uploads/secret-bot')).toHaveCount(0)
  await expect(page.getByText('TOP-SECRET-CHECKSUM')).toHaveCount(0)
  await expect(page.getByText('PRIVATE-OWNER-NAME')).toHaveCount(0)

  await expect(panel.getByText('temporary queue outage')).toBeVisible({ timeout: 12_000 })
  await expect(panel).toContainText('以下保留上次成功获取的数据')
  await expect(panel.getByRole('link', { name: '进入观赛' })).toBeVisible()
  expect(maxInFlight).toBe(1)

  await panel.getByRole('button', { name: '立即重试', exact: true }).click()
  await expect(panel.getByText('temporary queue outage')).toHaveCount(0)
  await context.setOffline(true)
  await expect(panel).toContainText('当前离线')
  await context.setOffline(false)
  await expect(panel.getByText('当前离线')).toHaveCount(0)

  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)
  expect(overflow).toBeLessThanOrEqual(1)
  expect(calls).toBeGreaterThanOrEqual(3)
  expect(network.unexpectedBackendRequests).toEqual([])
  expect(network.forbiddenMainRequests).toEqual([])
  await monitor.expectClean([{
    kind: 'http',
    method: 'GET',
    status: 503,
    pathname: '/api/execution-queue',
  }])
})
