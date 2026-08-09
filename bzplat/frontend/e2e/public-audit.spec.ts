import { expect, test } from '@playwright/test'

import { monitorBrowser } from './helpers'

const USER = process.env.BZ_E2E_USER || 'tester1'
let publicBotId = 0

test.beforeAll(async ({ request }) => {
  const response = await request.get('/api/health')
  expect(response.status(), await response.text()).toBe(200)
  expect((await response.json() as { qa_instance?: boolean }).qa_instance).toBe(true)

  const botResponse = await request.get(
    `/api/search?type=bots&q=${encodeURIComponent(`${USER}_holdem`)}`,
  )
  expect(botResponse.status(), await botResponse.text()).toBe(200)
  const botData = await botResponse.json() as { bots?: Array<{ id: number; name: string }> }
  const bot = botData.bots?.find((item) => item.name === `${USER}_holdem`)
  expect(
    bot,
    `E2E prerequisite missing: seed ${USER}_holdem in the isolated QA database`,
  ).toBeTruthy()
  publicBotId = bot!.id

  const matchesResponse = await request.get('/api/matches?limit=1')
  expect(matchesResponse.status(), await matchesResponse.text()).toBe(200)
  const matches = await matchesResponse.json() as { matches?: Array<{ id: string }> }
  expect(
    matches.matches?.length,
    'E2E prerequisite missing: isolated QA database needs at least one public match',
  ).toBeGreaterThan(0)

  const contestsResponse = await request.get('/api/contests?page=1&per_page=1')
  expect(contestsResponse.status(), await contestsResponse.text()).toBe(200)
  const contests = await contestsResponse.json() as { contests?: Array<{ id: number }> }
  expect(
    contests.contests?.length,
    'E2E prerequisite missing: isolated QA database needs at least one public contest',
  ).toBeGreaterThan(0)
})

test('public deep links, refresh, back/forward, search, and fallback routes work', async ({ page }) => {
  const monitor = monitorBrowser(page)

  await page.goto(`/#/bot/${publicBotId}`)
  await expect(page.locator('main')).toContainText(`${USER}_holdem`)

  await page.goto(`/#/user/${USER}`)
  await expect(page.locator('main')).toContainText(USER)

  await page.goto(`/#/search?q=${encodeURIComponent(USER)}&type=users`)
  await expect(page.locator(`a[href="#/user/${USER}"]`).first()).toBeVisible()
  await page.getByRole('tab', { name: 'Bot', exact: true }).click()
  await expect(page.locator(`a[href="#/bot/${publicBotId}"]`).first()).toBeVisible()

  await page.goto('/#/history')
  await expect(page.getByRole('heading', { name: '对局历史', exact: true })).toBeVisible()
  const matchLink = page.locator('a[href^="#/match/"]').first()
  await expect(matchLink).toBeVisible()
  const matchHref = await matchLink.getAttribute('href')
  const matchId = matchHref?.match(/^#\/match\/(.+)$/)?.[1]
  expect(matchId, `unexpected public match href: ${matchHref}`).toBeTruthy()
  const waitForMatchDetail = () => page.waitForResponse((response) => {
    const request = response.request()
    const url = new URL(response.url())
    return request.method() === 'GET' &&
      url.pathname === `/api/matches/${matchId}` &&
      response.status() === 200
  })

  const initialDetail = waitForMatchDetail()
  await matchLink.click()
  await initialDetail
  await expect(page).toHaveURL(new RegExp(`${matchHref!.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}$`))
  await expect(page.getByRole('heading', { name: /^(?:实时观赛|对局详情)$/ })).toBeVisible()
  await monitor.settle()

  const reloadedDetail = waitForMatchDetail()
  await page.reload()
  await reloadedDetail
  await expect(page.getByRole('heading', { name: /^(?:实时观赛|对局详情)$/ })).toBeVisible()
  await monitor.settle()

  await page.goBack()
  await expect(page).toHaveURL(/#\/history$/)
  await expect(page.getByRole('heading', { name: '对局历史', exact: true })).toBeVisible()
  await monitor.settle()

  const forwardedDetail = waitForMatchDetail()
  await page.goForward()
  await forwardedDetail
  await expect(page).toHaveURL(/#\/match\//)
  await expect(page.getByRole('heading', { name: /^(?:实时观赛|对局详情)$/ })).toBeVisible()
  await monitor.settle()

  await page.goto('/#/contests')
  await expect(page.getByRole('heading', { name: '锦标赛', exact: true })).toBeVisible()
  const contestLink = page.locator('a[href^="#/contests/"]').first()
  await expect(contestLink).toBeVisible()
  await contestLink.click()
  await expect(page.getByRole('heading', { name: '锦标赛详情', exact: true })).toBeVisible()

  await page.goto('/#/this-route-does-not-exist')
  await expect(page).toHaveURL(/\/#\/$/)
  await expect(page.getByRole('heading', { name: '首页', exact: true })).toBeVisible()
  await expect(page.getByRole('heading', { name: '多游戏 Bot 竞赛平台', exact: true })).toBeVisible()
  await monitor.expectClean()
})

test('invalid login is single-submit and displays the server error', async ({ page }) => {
  const monitor = monitorBrowser(page)
  let loginPosts = 0
  page.on('request', (request) => {
    if (request.method() === 'POST' && new URL(request.url()).pathname === '/api/auth/login') {
      loginPosts += 1
    }
  })

  await page.goto('/#/login')
  const username = page.locator('#login-username')
  expect(await username.evaluate((input: HTMLInputElement) => input.checkValidity())).toBe(false)
  await username.fill('definitely_missing_user')
  await page.locator('#login-password').fill('bad-password')
  await page.getByPlaceholder('图中字符或算式结果').fill('skip')
  const responsePromise = page.waitForResponse(
    (response) => response.request().method() === 'POST' && new URL(response.url()).pathname === '/api/auth/login',
  )
  await page.getByRole('button', { name: '登录', exact: true }).dblclick()
  expect((await responsePromise).status()).toBe(401)
  await expect(page.getByText(/用户名或密码错误|invalid_credentials/)).toBeVisible()
  expect(loginPosts).toBe(1)
  await monitor.expectClean([{
    kind: 'http',
    method: 'POST',
    status: 401,
    pathname: '/api/auth/login',
  }])
})

test('history exposes a recoverable error and empty state after a network failure', async ({ page }) => {
  const monitor = monitorBrowser(page)
  let abortedMatchRequests = 0
  await page.route('**/api/matches?*', (route) => {
    const request = route.request()
    const url = new URL(request.url())
    expect(request.method()).toBe('GET')
    expect(url.pathname).toBe('/api/matches')
    expect(url.search).toBe('?limit=20&offset=0')
    abortedMatchRequests += 1
    return route.abort('failed')
  })
  await page.goto('/#/history')
  await expect(page.getByText(/Failed to fetch|网络|请求失败/)).toBeVisible()
  await expect(page.getByText('暂无对局', { exact: true })).toBeVisible()
  // React StrictMode may abort the probe effect and issue it once more. Keep the
  // allowance bounded to that exact behavior; zero or more than two is a failure.
  expect(abortedMatchRequests).toBeGreaterThanOrEqual(1)
  expect(abortedMatchRequests).toBeLessThanOrEqual(2)

  await page.unroute('**/api/matches?*')
  await page.reload()
  await expect(page.locator('a[href^="#/match/"]').first()).toBeVisible()
  const expectedFailures = Array.from({ length: abortedMatchRequests }, () => ({
    kind: 'requestfailed',
    method: 'GET',
    pathname: '/api/matches',
    search: '?limit=20&offset=0',
    errorText: 'net::ERR_FAILED',
  } as const))
  await monitor.expectClean(expectedFailures)
})
