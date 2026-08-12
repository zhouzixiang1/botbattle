import { expect, test, type Browser, type BrowserContext, type Page } from '@playwright/test'

import {
  loginThroughUi,
  monitorBrowser,
  runCleanupTasks,
  withCleanup,
  type BrowserMonitor,
} from './helpers'

const ORGANIZER = process.env.BZ_E2E_ORGANIZER || 'qa_organizer'
const ADMIN = process.env.BZ_E2E_ADMIN || 'qa_admin'
const USER = process.env.BZ_E2E_USER || 'tester1'
const OTHER_USER = process.env.BZ_E2E_OTHER_USER || 'tester2'

test.beforeAll(async ({ request }) => {
  const response = await request.get('/api/health')
  expect(response.status(), await response.text()).toBe(200)
  expect((await response.json() as { qa_instance?: boolean }).qa_instance).toBe(true)
})

function captureExactGetCancellations(page: Page, pathname: string, maxCount = 3) {
  const errors: string[] = []
  page.on('requestfailed', (request) => {
    if (
      request.method() === 'GET' &&
      new URL(request.url()).pathname === pathname
    ) {
      errors.push(request.failure()?.errorText || '')
    }
  })
  return () => {
    expect(errors.length, `${pathname} cancellation count`).toBeLessThanOrEqual(maxCount)
    for (const errorText of errors) {
      expect(
        errorText,
        `${pathname} failed for a reason other than a browser navigation/effect cancellation`,
      ).toMatch(/^(?:net::ERR_ABORTED|NS_BINDING_ABORTED|load request cancelled)$/i)
    }
    return errors.map((errorText) => ({
      kind: 'requestfailed' as const,
      method: 'GET',
      pathname,
      errorText,
    }))
  }
}

async function loggedInPage(
  browser: Browser,
  baseURL: string,
  username: string,
): Promise<{ context: BrowserContext; page: Page; monitor: BrowserMonitor }> {
  const context = await browser.newContext({ baseURL, viewport: { width: 1280, height: 720 } })
  const page = await context.newPage()
  const monitor = monitorBrowser(page)
  await loginThroughUi(page, username)
  return { context, page, monitor }
}

async function ensureCreatedContestSettled(
  browser: Browser,
  baseURL: string,
  contestId: number,
) {
  if (!browser.isConnected()) return
  const admin = await loggedInPage(browser, baseURL, ADMIN)
  try {
    const initial = await admin.page.request.get(`/api/contests/${contestId}`)
    expect(initial.status(), await initial.text()).toBe(200)
    const detail = await initial.json() as {
      contest: { status: string }
      pairings?: Array<{ id?: number; status?: string; match_id?: string | null }>
    }

    const remove = await admin.page.request.delete(`/api/admin/contests/${contestId}`)
    if (detail.contest.status === 'finished') {
      // Finished contests are immutable audit records by product contract. A QA
      // instance is disposable as a whole, so cleanup verifies protection rather
      // than bypassing it or corrupting the terminal history.
      expect(remove.status(), await remove.text()).toBe(409)
      const verify = await admin.page.request.get(`/api/contests/${contestId}`)
      expect(verify.status(), await verify.text()).toBe(200)
      return
    }
    if (remove.status() === 200) {
      expect(['draft', 'open', 'published', 'cancelled']).toContain(
        detail.contest.status,
      )
      expect(remove.status(), await remove.text()).toBe(200)
      const verify = await admin.page.request.get(`/api/contests/${contestId}`)
      expect(verify.status(), await verify.text()).toBe(404)
      return
    }

    // Never manufacture a terminal contest by aborting matches: an abort
    // intentionally requeues its pairing, so force-finishing immediately would
    // race the durable queue and destroy the very evidence needed to diagnose a
    // failed workflow. Preserve the isolated QA state and surface a bounded
    // snapshot; the whole disposable DB is reset between release runs.
    const runtime = await admin.page.request.get('/api/admin/settings/runtime')
    const runtimeBody = runtime.status() === 200 ? await runtime.json() : null
    throw new Error(JSON.stringify({
      contest: detail.contest,
      pairings: detail.pairings || [],
      delete_status: remove.status(),
      delete_body: await remove.text(),
      queue: runtimeBody?.queue || null,
    }))
  } finally {
    await admin.context.close()
  }
}

async function registerContestBot(page: Page, contestId: number, botName: string) {
  await page.goto(`/#/contests/${contestId}`)
  const selector = page.getByRole('combobox')
  await expect(selector).toBeVisible()
  await selector.click()
  await page.getByRole('option', { name: botName, exact: true }).click()

  let posts = 0
  page.on('request', (request) => {
    if (
      request.method() === 'POST' &&
      new URL(request.url()).pathname === `/api/contests/${contestId}/register`
    ) posts += 1
  })
  const responsePromise = page.waitForResponse(
    (response) => response.request().method() === 'POST' && new URL(response.url()).pathname === `/api/contests/${contestId}/register`,
  )
  await page.getByRole('button', { name: '报名派遣', exact: true }).dblclick()
  expect((await responsePromise).status()).toBe(200)
  await expect(page.getByRole('button', { name: '报名派遣', exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '确认更换', exact: true })).toBeVisible()
  await page.waitForTimeout(250)
  expect(posts).toBe(1)
}

test('organizer contest lifecycle completes and preserves the terminal audit record', async ({
  page,
  browser,
  baseURL,
}) => {
  // Two real double-round-robin Gomoku matches run concurrently, while every
  // Traditional turn must serialize Docker create/start through the global
  // physical launch fence.  This is intentionally a real runtime proof rather
  // than a mocked fast path, so allow the measured ~90s healthy completion.
  test.setTimeout(240_000)
  expect(baseURL).toBeTruthy()
  let contestId: number | null = null
  const childContexts = new Set<BrowserContext>()
  await withCleanup(async () => {
    const title = `PW QA Gomoku ${Date.now()}`
    const organizerMonitor = monitorBrowser(page)
    await loginThroughUi(page, ORGANIZER)
    await page.goto('/#/contests')

  const form = page.locator('form')
  const createButton = form.getByRole('button', { name: '创建比赛', exact: true })
  await createButton.click()
  expect(await page.locator('#contest-title').evaluate((input: HTMLInputElement) => input.checkValidity())).toBe(false)

  const gameSelect = form.getByRole('combobox').nth(0)
  const templateSelect = form.getByRole('combobox').nth(1)
  await gameSelect.click()
  await page.getByRole('option', { name: '五子棋', exact: true }).last().click()
  await expect(templateSelect).toBeEnabled()
  await templateSelect.click()
  await page.getByRole('option', { name: /棋类：双循环/ }).click()
  await page.locator('#contest-title').fill(title)
  await page.locator('#contest-desc').fill('Playwright organizer → player → admin lifecycle regression')

  let createPosts = 0
  page.on('request', (request) => {
    if (request.method() === 'POST' && new URL(request.url()).pathname === '/api/contests') {
      createPosts += 1
    }
  })
  const createResponsePromise = page.waitForResponse(
    (response) => response.request().method() === 'POST' && new URL(response.url()).pathname === '/api/contests',
  )
  await createButton.dblclick()
  const createResponse = await createResponsePromise
  expect(createResponse.status(), await createResponse.text()).toBe(200)
  const created = await createResponse.json() as { contest: { id: number } }
  contestId = created.contest.id
  await page.waitForTimeout(250)
  expect(createPosts).toBe(1)
  await page.getByRole('link', { name: title, exact: true }).click()

  let openPosts = 0
  page.on('request', (request) => {
    if (request.method() === 'POST' && new URL(request.url()).pathname === `/api/contests/${contestId}/open`) {
      openPosts += 1
    }
  })
  const openResponsePromise = page.waitForResponse(
    (response) => response.request().method() === 'POST' && new URL(response.url()).pathname === `/api/contests/${contestId}/open`,
  )
  await page.getByRole('button', { name: '开放报名', exact: true }).dblclick()
  expect((await openResponsePromise).status()).toBe(200)
  await expect(page.getByText('报名中', { exact: true })).toBeVisible()
  await page.waitForTimeout(250)
  expect(openPosts).toBe(1)

  const player1 = await loggedInPage(browser, baseURL!, USER)
  childContexts.add(player1.context)
  await registerContestBot(player1.page, contestId, `${USER}_gomoku`)
  await player1.monitor.expectClean()
  await player1.context.close()
  childContexts.delete(player1.context)

  const player2 = await loggedInPage(browser, baseURL!, OTHER_USER)
  childContexts.add(player2.context)
  await registerContestBot(player2.page, contestId, `${OTHER_USER}_gomoku`)
  await player2.monitor.expectClean()
  await player2.context.close()
  childContexts.delete(player2.context)

  await page.reload()
  await page.getByRole('tab', { name: /选手/ }).click()
  await expect(page.getByRole('tab', { name: /选手\s*2/ })).toBeVisible()

  const publishResponse = page.waitForResponse(
    (response) => response.request().method() === 'POST' && new URL(response.url()).pathname === `/api/contests/${contestId}/publish`,
  )
  await page.getByRole('button', { name: '截止报名·出排期', exact: true }).click()
  expect((await publishResponse).status()).toBe(200)
  await expect(page.getByRole('main').getByText('排期已发布', { exact: true })).toBeVisible()

  const startResponse = page.waitForResponse(
    (response) => response.request().method() === 'POST' && new URL(response.url()).pathname === `/api/contests/${contestId}/start`,
  )
  await page.getByRole('button', { name: '立即开赛', exact: true }).click()
  expect((await startResponse).status()).toBe(200)

  await expect.poll(async () => {
    return await page.evaluate(async (currentContestId) => {
      const response = await fetch(`/api/contests/${currentContestId}`)
      const data = await response.json() as { contest?: { status?: string } }
      return data.contest?.status
    }, contestId)
  }, { timeout: 150_000, intervals: [500, 1000, 2000] }).toBe('finished')

  const matchStatuses = await page.evaluate(async (currentContestId) => {
    const contestResponse = await fetch(`/api/contests/${currentContestId}`)
    const data = await contestResponse.json() as {
      contest?: { official_results_ready?: number }
      pairings?: Array<{ status?: string; match_id?: string | null }>
    }
    const pairings = await Promise.all((data.pairings || []).map(async (pairing) => {
      if (!pairing.match_id) return { pairing: pairing.status, match: null, reason: null }
      const matchResponse = await fetch(`/api/matches/${pairing.match_id}`)
      const matchData = await matchResponse.json() as {
        match?: { status?: string; reason?: string }
      }
      return {
        pairing: pairing.status,
        match: matchData.match?.status,
        reason: matchData.match?.reason,
      }
    }))
    return { officialResultsReady: data.contest?.official_results_ready, pairings }
  }, contestId)
  expect(matchStatuses.officialResultsReady).toBe(1)
  expect(matchStatuses.pairings).toHaveLength(2)
  expect(matchStatuses.pairings).toEqual([
    { pairing: 'completed', match: 'completed', reason: 'five' },
    { pairing: 'completed', match: 'completed', reason: 'five' },
  ])

  const runtime = await page.request.get('/api/execution-queue')
  expect(runtime.status(), await runtime.text()).toBe(200)
  const queue = (await runtime.json()) as {
    dispatcher: { state: string; accepting: boolean }
  }
  expect(queue.dispatcher).toMatchObject({ state: 'running', accepting: true })

  await page.reload()
  await expect(page.getByRole('main').getByText('已结束', { exact: true })).toBeVisible()
  await page.getByRole('tab', { name: /对阵/ }).click()
  await expect(page.getByText(/2\/2/).first()).toBeVisible()
  await page.getByRole('tab', { name: /正式名次/ }).click()
  await expect(page.locator('main')).toContainText(`${USER}_gomoku`)
  await organizerMonitor.expectClean()

  const admin = await loggedInPage(browser, baseURL!, ADMIN)
  childContexts.add(admin.context)
  const expectedRuntimeCancellations = captureExactGetCancellations(
    admin.page,
    '/api/admin/settings/runtime',
  )
  const expectedStatsCancellations = captureExactGetCancellations(
    admin.page,
    '/api/admin/stats',
  )
  await admin.page.goto('/#/admin')
  const adminNavigation = admin.page.getByRole('navigation', { name: '管理控制台模块' })
  await expect(adminNavigation).toBeVisible()
  await adminNavigation.getByRole('button', { name: /^锦标赛/ }).click()
  const row = admin.page.getByText(title, { exact: true }).locator('xpath=ancestor::tr[1]')
  await expect(row).toBeVisible()
  await expect(row.getByText('成绩已归档 · 只读', { exact: true })).toBeVisible()
  await expect(row.getByRole('button', { name: /删除/ })).toHaveCount(0)
  const forbiddenDelete = await admin.page.request.delete(`/api/admin/contests/${contestId}`)
  expect(forbiddenDelete.status(), await forbiddenDelete.text()).toBe(409)
  await admin.monitor.expectClean([
    ...expectedRuntimeCancellations(),
    ...expectedStatsCancellations(),
  ])
  await admin.context.close()
  childContexts.delete(admin.context)
  }, async () => {
    const tasks: Array<{ label: string; run: () => Promise<void> }> = []
    for (const context of childContexts) {
      tasks.push({
        label: 'close child browser context',
        run: async () => {
          if (context.browser()?.isConnected()) await context.close()
        },
      })
    }
    if (contestId !== null) {
      const createdContestId = contestId
      tasks.push({
        label: `delete contest ${createdContestId}`,
        run: () => ensureCreatedContestSettled(
          browser,
          baseURL!,
          createdContestId,
        ),
      })
    }
    await runCleanupTasks(tasks)
  })
})
