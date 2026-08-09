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
  alreadyDeleted: boolean,
) {
  const admin = await loggedInPage(browser, baseURL, ADMIN)
  try {
    const initial = await admin.page.request.get(`/api/contests/${contestId}`)
    if (alreadyDeleted) {
      expect(initial.status(), await initial.text()).toBe(404)
      return
    }
    expect(initial.status(), await initial.text()).toBe(200)
    let detail = await initial.json() as {
      contest: { status: string }
      pairings?: Array<{ match_id?: string | null }>
    }

    for (const matchId of new Set(
      (detail.pairings || []).map((pairing) => pairing.match_id).filter(Boolean) as string[],
    )) {
      const matchResponse = await admin.page.request.get(`/api/matches/${matchId}`)
      expect(matchResponse.status(), await matchResponse.text()).toBe(200)
      const match = await matchResponse.json() as { match: { status: string } }
      if (match.match.status === 'pending' || match.match.status === 'running') {
        const abort = await admin.page.request.patch(`/api/admin/matches/${matchId}`, {
          data: { status: 'aborted', reason: 'e2e-contest-cleanup' },
        })
        expect(abort.status(), await abort.text()).toBe(200)
      } else {
        expect(['completed', 'aborted']).toContain(match.match.status)
      }
    }

    const refreshed = await admin.page.request.get(`/api/contests/${contestId}`)
    expect(refreshed.status(), await refreshed.text()).toBe(200)
    detail = await refreshed.json() as typeof detail
    if (detail.contest.status === 'running' || detail.contest.status === 'rest') {
      const finish = await admin.page.request.patch(`/api/admin/contests/${contestId}`, {
        data: { status: 'finished' },
      })
      expect(finish.status(), await finish.text()).toBe(200)
    } else {
      expect(['draft', 'open', 'published', 'finished', 'cancelled']).toContain(
        detail.contest.status,
      )
    }

    const finalDetail = await admin.page.request.get(`/api/contests/${contestId}`)
    expect(finalDetail.status(), await finalDetail.text()).toBe(200)
    const finalStatus = ((await finalDetail.json()) as { contest: { status: string } }).contest.status
    const remove = await admin.page.request.delete(`/api/admin/contests/${contestId}`)
    if (finalStatus === 'finished') {
      // Finished contests are immutable audit records by product contract. A QA
      // instance is disposable as a whole, so cleanup verifies protection rather
      // than bypassing it or corrupting the terminal history.
      expect(remove.status(), await remove.text()).toBe(409)
      const verify = await admin.page.request.get(`/api/contests/${contestId}`)
      expect(verify.status(), await verify.text()).toBe(200)
    } else {
      expect(remove.status(), await remove.text()).toBe(200)
      const verify = await admin.page.request.get(`/api/contests/${contestId}`)
      expect(verify.status(), await verify.text()).toBe(404)
    }
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
  expect(baseURL).toBeTruthy()
  let contestId: number | null = null
  let contestDeleted = false
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
  }, { timeout: 60_000, intervals: [500, 1000, 2000] }).toBe('finished')

  const matchStatuses = await page.evaluate(async (currentContestId) => {
    const contestResponse = await fetch(`/api/contests/${currentContestId}`)
    const data = await contestResponse.json() as {
      pairings?: Array<{ status?: string; match_id?: string | null }>
    }
    return await Promise.all((data.pairings || []).map(async (pairing) => {
      if (!pairing.match_id) return { pairing: pairing.status, match: null }
      const matchResponse = await fetch(`/api/matches/${pairing.match_id}`)
      const matchData = await matchResponse.json() as { match?: { status?: string } }
      return { pairing: pairing.status, match: matchData.match?.status }
    }))
  }, contestId)
  expect(matchStatuses).toHaveLength(2)
  expect(matchStatuses).toEqual([
    { pairing: 'completed', match: 'completed' },
    { pairing: 'completed', match: 'completed' },
  ])

  await page.reload()
  await expect(page.getByRole('main').getByText('已结束', { exact: true })).toBeVisible()
  await page.getByRole('tab', { name: /对阵/ }).click()
  await expect(page.getByText(/2\/2/).first()).toBeVisible()
  await page.getByRole('tab', { name: /正式名次/ }).click()
  await expect(page.locator('main')).toContainText(`${USER}_gomoku`)
  await organizerMonitor.expectClean()

  const admin = await loggedInPage(browser, baseURL!, ADMIN)
  childContexts.add(admin.context)
  await admin.page.goto('/#/admin')
  await admin.page.getByRole('button', { name: '锦标赛', exact: true }).click()
  const row = admin.page.getByText(title, { exact: true }).locator('xpath=ancestor::tr[1]')
  await expect(row).toBeVisible()
  await expect(row.getByText('成绩已归档 · 只读', { exact: true })).toBeVisible()
  await expect(row.getByRole('button', { name: /删除/ })).toHaveCount(0)
  const forbiddenDelete = await admin.page.request.delete(`/api/admin/contests/${contestId}`)
  expect(forbiddenDelete.status(), await forbiddenDelete.text()).toBe(409)
  await admin.monitor.expectClean()
  await admin.context.close()
  childContexts.delete(admin.context)
  }, async () => {
    const tasks: Array<{ label: string; run: () => Promise<void> }> = []
    if (contestId !== null) {
      const createdContestId = contestId
      tasks.push({
        label: `delete contest ${createdContestId}`,
        run: () => ensureCreatedContestSettled(
          browser,
          baseURL!,
          createdContestId,
          contestDeleted,
        ),
      })
    }
    for (const context of childContexts) {
      tasks.push({ label: 'close child browser context', run: () => context.close() })
    }
    await runCleanupTasks(tasks)
  })
})
