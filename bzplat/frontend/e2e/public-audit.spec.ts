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
  const abortedDetailRequests: string[] = []
  page.on('requestfailed', (request) => {
    if (
      request.method() === 'GET' &&
      new URL(request.url()).pathname === `/api/matches/${matchId}`
    ) {
      abortedDetailRequests.push(request.failure()?.errorText || '')
    }
  })
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
  await expect(page.getByRole('heading', { name: 'Bot 对战中心', exact: true })).toBeVisible()
  await expect(page.locator('main')).toContainText('上传 Linux x86_64 ELF Bot')
  expect(abortedDetailRequests.length).toBeLessThanOrEqual(3)
  await monitor.expectClean(abortedDetailRequests.map((errorText) => ({
    kind: 'requestfailed' as const,
    method: 'GET',
    pathname: `/api/matches/${matchId}`,
    errorText,
  })))
})

test('collapsed desktop sidebar keeps the Botbattle wordmark inside the navigation rail', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/#/')

  const sidebar = page.locator('aside[aria-label="站点导航"]')
  await expect(sidebar).toBeVisible()
  await expect(sidebar.getByText('Botbattle', { exact: true })).toBeVisible()

  await sidebar.getByRole('button', { name: '收起侧边栏' }).click()
  await expect(sidebar).toHaveAttribute('data-sidebar-collapsed', 'true')
  await expect(sidebar.getByText('Botbattle', { exact: true })).toHaveCount(0)
  const collapsedOverflow = await sidebar.evaluate((node) => node.scrollWidth - node.clientWidth)
  expect(collapsedOverflow).toBeLessThanOrEqual(1)

  await sidebar.getByRole('button', { name: '展开侧边栏' }).click()
  await expect(sidebar).toHaveAttribute('data-sidebar-collapsed', 'false')
  await expect(sidebar.getByText('Botbattle', { exact: true })).toBeVisible()
})

test('history identifies each user, Bot or human participant and the match nature', async ({ page }) => {
  const monitor = monitorBrowser(page)
  await page.route('**/api/matches?*', async (route) => {
    const url = new URL(route.request().url())
    if (url.pathname !== '/api/matches') return route.continue()
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        total: 3,
        limit: 20,
        offset: 0,
        matches: [
          {
            id: 'history-clear-challenge', status: 'completed', game_id: 'holdem',
            match_type: 'challenge', bot_a_id: 11, bot_b_id: 12,
            bot_a: { id: 11, name: 'alpha', display_name: 'Alpha Bot', owner_name: 'alice', owner_display: 'Alice', is_human: false },
            bot_b: { id: 12, name: 'beta', display_name: 'Beta Bot', owner_name: 'bob', owner_display: 'Bob', is_human: false },
            created_at: '2026-08-11T12:00:00+00:00',
          },
          {
            id: 'history-clear-human', status: 'running', game_id: 'gomoku',
            match_type: 'human', bot_a_id: 12, bot_b_id: 12,
            bot_a: { id: 12, name: 'beta', display_name: 'Beta Bot', owner_name: 'bot_owner', owner_display: 'Bot 主人', is_human: false },
            bot_b: { id: null, name: '真人小王', display_name: '真人小王', owner_name: 'human_wang', owner_display: '真人小王', is_human: true },
            created_at: '2026-08-11T12:01:00+00:00',
          },
          {
            id: 'history-clear-selfplay', status: 'completed', game_id: 'pencil',
            match_type: 'challenge', bot_a_id: 21, bot_b_id: 21,
            bot_a: { id: 21, name: 'self_bot', display_name: '自测 Bot', owner_name: 'self_owner', owner_display: '自测用户', is_human: false },
            bot_b: { id: 21, name: 'self_bot', display_name: '自测 Bot', owner_name: 'self_owner', owner_display: '自测用户', is_human: false },
            created_at: '2026-08-11T12:02:00+00:00',
          },
        ],
      }),
    })
  })

  await page.goto('/#/history')
  const challenge = page.locator('[data-testid="history-match-row"][data-match-type="challenge"]').filter({ hasText: 'Alpha Bot' })
  await expect(challenge).toContainText('Alpha Bot')
  await expect(challenge).toContainText('Alice')
  await expect(challenge).toContainText('@alice')
  await expect(challenge).toContainText('Beta Bot')
  await expect(challenge).toContainText('Bob')
  await expect(challenge.locator('[data-match-participants] [data-match-participant]')).toHaveCount(2)
  await expect(challenge.locator('[data-match-nature="challenge"]')).toHaveText('用户挑战')

  const human = page.locator('[data-testid="history-match-row"][data-match-type="human"]')
  await expect(human.locator('[data-participant-kind="bot"]')).toContainText('Beta Bot')
  await expect(human.locator('[data-participant-kind="bot"]')).toContainText('Bot 主人')
  await expect(human.locator('[data-participant-kind="human"]')).toContainText('真人小王')
  await expect(human.locator('[data-participant-kind="human"]')).toContainText('@human_wang')
  await expect(human.locator('[data-match-nature="human"]')).toHaveText('真人对战')
  await expect(human).not.toContainText('#12')

  const selfPlay = page.locator('[data-testid="history-match-row"]').filter({ hasText: '自测 Bot' })
  await expect(selfPlay.locator('[data-match-nature="self_play"]')).toHaveText('自博弈')
  await expect(selfPlay).toContainText('自测用户 · @self_owner')
  await monitor.expectClean()
})

test('home, search, and global search share participant ownership and nature labels', async ({ page }) => {
  const monitor = monitorBrowser(page)
  const latest = {
    id: 'surface-latest', status: 'completed', game_id: 'holdem', match_type: 'ladder',
    bot_a_id: 31, bot_b_id: 32,
    bot_a: { id: 31, name: 'home_alpha', display_name: '首页 Alpha', owner_name: 'home_alice', owner_display: '首页 Alice', is_human: false },
    bot_b: { id: 32, name: 'home_beta', display_name: '首页 Beta', owner_name: 'home_bob', owner_display: '首页 Bob', is_human: false },
    created_at: '2026-08-11T12:10:00+00:00',
  }
  const popular = {
    ...latest,
    id: 'surface-popular', match_type: 'challenge', likes_count: 9, views_count: 21,
    bot_a: { id: 33, name: 'popular_alpha', display_name: '热门 Alpha', owner_name: 'popular_alice', owner_display: '热门 Alice', is_human: false },
    bot_b: { id: 34, name: 'popular_beta', display_name: '热门 Beta', owner_name: 'popular_bob', owner_display: '热门 Bob', is_human: false },
  }
  const searched = {
    ...latest,
    id: 'surface-search', game_id: 'gomoku', match_type: 'contest',
    bot_a: { id: 35, name: 'search_alpha', display_name: '搜索 Alpha', owner_name: 'search_alice', owner_display: '搜索 Alice', is_human: false },
    bot_b: { id: 36, name: 'search_beta', display_name: '搜索 Beta', owner_name: 'search_bob', owner_display: '搜索 Bob', is_human: false },
  }

  await page.route('**/api/matches?*', async (route) => {
    const url = new URL(route.request().url())
    if (url.pathname !== '/api/matches') return route.continue()
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ matches: [latest], total: 1 }),
    })
  })
  await page.route('**/api/matches/liked-top?*', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ matches: [popular] }),
    })
  })
  await page.route('**/api/search?*', async (route) => {
    const url = new URL(route.request().url())
    const type = url.searchParams.get('type')
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(type === 'matches' ? { matches: [searched] } : type === 'bots' ? { bots: [] } : { users: [] }),
    })
  })

  await page.goto('/#/')
  const latestTable = page.getByRole('table', { name: '最新对局' })
  const latestRow = latestTable.getByRole('row').filter({ hasText: '首页 Alpha' })
  await expect(latestRow).toContainText('首页 Alice · @home_alice')
  await expect(latestRow).toContainText('首页 Bob · @home_bob')
  await expect(latestRow.locator('[data-match-nature="ladder"]')).toHaveText('自动排位')
  const popularIdentity = page.locator('[data-match-participants]').filter({ hasText: '热门 Alpha' })
  await expect(popularIdentity).toContainText('热门 Alice · @popular_alice')
  await expect(popularIdentity).toContainText('热门 Bob · @popular_bob')
  await expect(popularIdentity.locator('..').locator('[data-match-nature="challenge"]')).toHaveText('用户挑战')

  await page.goto('/#/search?q=surface&type=matches')
  const searchTable = page.getByRole('table', { name: '搜索到的对局' })
  const searchRow = searchTable.getByRole('row').filter({ hasText: '搜索 Alpha' })
  await expect(searchRow).toContainText('搜索 Alice · @search_alice')
  await expect(searchRow).toContainText('搜索 Bob · @search_bob')
  await expect(searchRow.locator('[data-match-nature="contest"]')).toHaveText('锦标赛')

  await page.locator('button[aria-label="搜索"]:visible').first().click()
  await page.getByPlaceholder('搜索 Bot、用户、对局…').fill('surface')
  const commandMatch = page.getByRole('dialog').locator('[data-match-participants]').filter({ hasText: '搜索 Alpha' })
  await expect(commandMatch).toContainText('搜索 Alice · @search_alice')
  await expect(commandMatch).toContainText('搜索 Bob · @search_bob')
  await expect(commandMatch.locator('..').locator('[data-match-nature="contest"]')).toHaveText('锦标赛')
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

test('history distinguishes a recoverable network error from a genuine empty state', async ({ page }) => {
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
  await expect(page.getByText('当前条件下暂无对局', { exact: true })).toHaveCount(0)
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
