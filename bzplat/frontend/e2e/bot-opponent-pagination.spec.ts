import { expect, test, type Page } from '@playwright/test'

import { monitorBrowser } from './helpers'

const BOT_ID = 981001
const MIDDLE_BOT_ID = 981002
const NEXT_BOT_ID = 981003
const TOTAL_OPPONENTS = 45
const PER_PAGE = 20

interface MockOptions {
  failFirstOpponentRequest?: boolean
  delayedOpponentPages?: number[]
}

function opponentRows() {
  return Array.from({ length: TOTAL_OPPONENTS }, (_, index) => {
    const number = index + 1
    return {
      opponent_id: 982000 + number,
      opponent_name: `pagination_opponent_${number}`,
      opponent_display: `分页对手 ${String(number).padStart(2, '0')}`,
      game_id: 'gomoku',
      wins: number + 2,
      losses: number,
      draws: 2,
      samples: number * 2 + 4,
      last_played_at: `2026-08-${String((number % 18) + 1).padStart(2, '0')}T12:00:00+00:00`,
    }
  })
}

async function mockBotDetail(page: Page, options: MockOptions = {}) {
  const requests: URL[] = []
  let opponentErrorActive = Boolean(options.failFirstOpponentRequest)
  const delayedPages = new Set(options.delayedOpponentPages || [])
  const allOpponents = opponentRows()

  await page.route('**/api/comments?*', async (route) => {
    const url = new URL(route.request().url())
    const targetId = Number(url.searchParams.get('target_id'))
    if (
      url.searchParams.get('target_type') === 'bot' &&
      [BOT_ID, MIDDLE_BOT_ID, NEXT_BOT_ID].includes(targetId)
    ) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: '{"comments":[],"page":1,"per_page":20,"total":0}',
      })
      return
    }
    await route.continue()
  })

  await page.route(`**/api/bots/${BOT_ID}/**`, async (route) => {
    const url = new URL(route.request().url())
    if (url.pathname.endsWith('/profile')) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          profile: {
            id: BOT_ID,
            name: 'pagination_subject',
            display_name: '分页测试 Bot',
            description: '对手战绩分页回归样本',
            game_id: 'gomoku',
            owner_id: 981000,
            owner_name: 'pagination_owner',
            owner_display: '分页测试用户',
            is_active: 1,
            is_ranked: 1,
            current_version: 3,
            created_at: '2026-08-19T10:00:00+00:00',
            rating: 1650,
            rd: 70,
            wins: 70,
            losses: 30,
            draws: 10,
            rated_matches: 110,
            unique_opponents: TOTAL_OPPONENTS,
            confidence_low: 1510,
            confidence_high: 1790,
            rank: 4,
            rank_total: 30,
            percentile: 86.7,
            ranking_min_matches: 10,
            ranking_progress: 1,
            ranking_eligible: true,
            rating_delta: 9,
            recent_delta_30d: 18,
            normal_completion_rate: 1,
            technical_failures: 0,
          },
        }),
      })
      return
    }
    if (url.pathname.endsWith('/rating-history')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: '{"history":[]}' })
      return
    }
    if (url.pathname.endsWith('/matches')) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: '{"matches":[],"page":1,"per_page":30,"total":0}',
      })
      return
    }
    if (url.pathname.endsWith('/opponents')) {
      requests.push(url)
      if (opponentErrorActive) {
        await route.fulfill({
          status: 500,
          contentType: 'application/json',
          body: '{"detail":"对手战绩暂时不可用"}',
        })
        return
      }
      const requestedPage = Number(url.searchParams.get('page') || '1')
      const perPage = Number(url.searchParams.get('per_page') || String(PER_PAGE))
      if (delayedPages.has(requestedPage)) {
        await new Promise((resolve) => setTimeout(resolve, 400))
      }
      const start = (requestedPage - 1) * perPage
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          opponents: allOpponents.slice(start, start + perPage),
          page: requestedPage,
          per_page: perPage,
          total: allOpponents.length,
        }),
      })
      return
    }
    await route.continue()
  })

  return {
    requests,
    allowOpponentSuccess: () => {
      opponentErrorActive = false
    },
  }
}

async function mockNextBot(page: Page) {
  const opponentRequests: URL[] = []
  await page.route(`**/api/bots/${NEXT_BOT_ID}/**`, async (route) => {
    const url = new URL(route.request().url())
    if (url.pathname.endsWith('/profile')) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          profile: {
            id: NEXT_BOT_ID,
            name: 'pagination_next_subject',
            display_name: '切换目标 Bot',
            description: '验证切换 Bot 后回到第一页',
            game_id: 'gomoku',
            owner_id: 981000,
            owner_name: 'pagination_owner',
            owner_display: '分页测试用户',
            is_active: 1,
            is_ranked: 1,
            current_version: 1,
            created_at: '2026-08-19T11:00:00+00:00',
            rating: 1500,
            rd: 120,
            wins: 1,
            losses: 0,
            draws: 0,
            rated_matches: 1,
            unique_opponents: 1,
            confidence_low: 1260,
            confidence_high: 1740,
            rank: null,
            rank_total: 30,
            percentile: null,
            ranking_min_matches: 10,
            ranking_progress: 0.1,
            ranking_eligible: false,
            rating_delta: null,
            recent_delta_30d: null,
            normal_completion_rate: 1,
            technical_failures: 0,
          },
        }),
      })
      return
    }
    if (url.pathname.endsWith('/rating-history')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: '{"history":[]}' })
      return
    }
    if (url.pathname.endsWith('/matches')) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: '{"matches":[],"page":1,"per_page":30,"total":0}',
      })
      return
    }
    if (url.pathname.endsWith('/opponents')) {
      opponentRequests.push(url)
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          opponents: [{
            opponent_id: 983001,
            opponent_name: 'new_bot_opponent',
            opponent_display: '新 Bot 第一页对手',
            game_id: 'gomoku',
            wins: 1,
            losses: 0,
            draws: 0,
            samples: 1,
            last_played_at: '2026-08-19T12:00:00+00:00',
          }],
          page: 1,
          per_page: PER_PAGE,
          total: 1,
        }),
      })
      return
    }
    await route.continue()
  })
  return opponentRequests
}

async function mockDelayedMiddleBot(page: Page) {
  const profileRequests: URL[] = []
  await page.route(`**/api/bots/${MIDDLE_BOT_ID}/**`, async (route) => {
    const url = new URL(route.request().url())
    if (url.pathname.endsWith('/profile')) {
      profileRequests.push(url)
      await new Promise((resolve) => setTimeout(resolve, 600))
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          profile: {
            id: MIDDLE_BOT_ID,
            name: 'delayed_middle_subject',
            display_name: '延迟的中间 Bot',
            description: '这份旧资料不得覆盖后续 Bot',
            game_id: 'gomoku',
            owner_id: 981000,
            owner_name: 'pagination_owner',
            is_active: 1,
            is_ranked: 0,
            current_version: 1,
            rated_matches: 0,
            unique_opponents: 1,
            confidence_low: null,
            confidence_high: null,
            rank: null,
            rank_total: 0,
            percentile: null,
            ranking_min_matches: 10,
            ranking_progress: 0,
            ranking_eligible: false,
            rating_delta: null,
            recent_delta_30d: null,
            normal_completion_rate: null,
            technical_failures: 0,
          },
        }),
      })
      return
    }
    if (url.pathname.endsWith('/rating-history')) {
      await new Promise((resolve) => setTimeout(resolve, 600))
      await route.fulfill({ status: 200, contentType: 'application/json', body: '{"history":[]}' })
      return
    }
    if (url.pathname.endsWith('/matches')) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: '{"matches":[],"page":1,"per_page":30,"total":0}',
      })
      return
    }
    if (url.pathname.endsWith('/opponents')) {
      await new Promise((resolve) => setTimeout(resolve, 600))
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          opponents: [{
            opponent_id: 983002,
            opponent_name: 'delayed_old_opponent',
            opponent_display: '延迟旧对手',
            game_id: 'gomoku',
            wins: 1,
            losses: 0,
            draws: 0,
            samples: 1,
          }],
          page: 1,
          per_page: PER_PAGE,
          total: 1,
        }),
      })
      return
    }
    await route.continue()
  })
  return profileRequests
}

test.beforeAll(async ({ request }) => {
  const health = await request.get('/api/health')
  expect(health.status(), await health.text()).toBe(200)
  expect((await health.json() as { qa_instance?: boolean }).qa_instance).toBe(true)
})

test('opponent history paginates all rows and replaces each page without stale content', async ({ page }) => {
  const { requests } = await mockBotDetail(page, { delayedOpponentPages: [2] })
  await page.setViewportSize({ width: 1024, height: 768 })
  await page.goto(`/#/bot/${BOT_ID}`)

  const opponentsTab = page.getByRole('tab', { name: /\u5bf9\u624b\u6218\u7ee9/ })
  await expect(opponentsTab).toContainText(String(TOTAL_OPPONENTS))
  await opponentsTab.click()

  const table = page.getByRole('table', { name: 'Bot 对手战绩' })
  await expect(table).toBeVisible()
  await expect(table.locator('tbody tr')).toHaveCount(PER_PAGE)
  await expect(table.getByText('分页对手 01', { exact: true })).toBeVisible()
  await expect(page.getByText('当前评分池计分交手 · 第 1 页 · 每页 20 个 · 共 45 个对手', { exact: true })).toBeVisible()

  const pagination = page.getByRole('navigation', { name: 'Bot 对手战绩分页' })
  const monitor = monitorBrowser(page)
  const secondPage = pagination.getByRole('button', { name: '第 2 页', exact: true })
  expect(await secondPage.getAttribute('type')).toBe('button')
  await secondPage.click()
  await expect(page.getByRole('status')).toContainText('正在加载对手战绩…')
  await expect(table).toBeVisible()
  await expect(table.locator('tbody tr')).toHaveCount(PER_PAGE)
  await expect(table.getByText('分页对手 21', { exact: true })).toBeVisible()
  await expect(table.getByText('分页对手 01', { exact: true })).toHaveCount(0)
  await expect(pagination.getByRole('button', { name: '第 2 页', exact: true })).toHaveAttribute('aria-current', 'page')

  const thirdPage = pagination.getByRole('button', { name: '第 3 页', exact: true })
  await thirdPage.focus()
  await expect(thirdPage).toBeFocused()
  await page.keyboard.press('Enter')
  await expect(table.locator('tbody tr')).toHaveCount(5)
  await expect(table.getByText('分页对手 41', { exact: true })).toBeVisible()
  await expect(pagination.getByRole('button', { name: '第 3 页', exact: true })).toHaveAttribute('aria-current', 'page')
  await expect(pagination.getByRole('button', { name: '下一页', exact: true })).toBeDisabled()

  const requestParams = [...new Set(
    requests.map((url) => `${url.searchParams.get('page')}/${url.searchParams.get('per_page')}`),
  )].sort()
  expect(requestParams).toEqual(['1/20', '2/20', '3/20'])
  await expect(page.getByTestId('bot-opponent-mobile-card').first()).toBeHidden()
  await monitor.expectClean()
})

test('mobile opponent cards and pagination stay touch-sized without root overflow', async ({ page }) => {
  await mockBotDetail(page)
  await page.setViewportSize({ width: 767, height: 844 })
  await page.goto(`/#/bot/${BOT_ID}`)
  await page.getByRole('tab', { name: /\u5bf9\u624b\u6218\u7ee9/ }).click()

  const cards = page.getByTestId('bot-opponent-mobile-card')
  await expect(cards).toHaveCount(PER_PAGE)
  const monitor = monitorBrowser(page)
  await expect(page.getByRole('table', { name: 'Bot 对手战绩' })).toBeHidden()
  const firstOpponentLink = cards.first().getByRole('link', { name: '分页对手 01', exact: true })
  expect((await firstOpponentLink.boundingBox())?.height).toBeGreaterThanOrEqual(44)

  const pagination = page.getByRole('navigation', { name: 'Bot 对手战绩分页' })
  await expect(pagination.getByRole('button', { name: '上一页', exact: true })).toBeDisabled()
  await expect(pagination.getByRole('button', { name: '第 1 页', exact: true })).toHaveAttribute('aria-current', 'page')
  for (const button of await pagination.getByRole('button').all()) {
    const box = await button.boundingBox()
    expect(box?.height).toBeGreaterThanOrEqual(44)
    expect(box?.width).toBeGreaterThanOrEqual(44)
  }
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1)

  await page.setViewportSize({ width: 390, height: 844 })
  await expect(cards.first()).toBeVisible()
  expect((await firstOpponentLink.boundingBox())?.height).toBeGreaterThanOrEqual(44)
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1)

  await page.setViewportSize({ width: 320, height: 568 })
  await expect(cards.first()).toBeVisible()
  expect((await firstOpponentLink.boundingBox())?.height).toBeGreaterThanOrEqual(44)
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1)
  await monitor.expectClean()
})

test('opponent request error stays local and retry restores the list', async ({ page }) => {
  const { requests, allowOpponentSuccess } = await mockBotDetail(page, {
    failFirstOpponentRequest: true,
    delayedOpponentPages: [1],
  })
  await page.setViewportSize({ width: 1024, height: 768 })
  await page.goto(`/#/bot/${BOT_ID}`)

  await expect(page.getByRole('heading', { name: '分页测试 Bot' })).toBeVisible()
  await page.getByRole('tab', { name: /\u5bf9\u624b\u6218\u7ee9/ }).click()
  await expect(page.getByRole('alert')).toContainText('对手战绩暂时不可用')
  await expect(page.getByText('分页测试用户', { exact: true })).toBeVisible()

  const monitor = monitorBrowser(page)
  allowOpponentSuccess()
  await page.getByRole('button', { name: '重试', exact: true }).click()
  await expect(page.getByRole('status')).toContainText('正在加载对手战绩…')
  await expect(page.getByRole('table', { name: 'Bot 对手战绩' })).toBeVisible()
  await expect(page.getByRole('alert')).toHaveCount(0)
  expect(requests.length).toBeGreaterThanOrEqual(2)
  expect(requests.at(-1)?.search).toBe('?page=1&per_page=20')
  await monitor.expectClean()
})

test('switching Bot resets opponent pagination and ignores the old delayed page', async ({ page }) => {
  const { requests } = await mockBotDetail(page, { delayedOpponentPages: [2] })
  const middleProfileRequests = await mockDelayedMiddleBot(page)
  const nextBotRequests = await mockNextBot(page)
  const monitor = monitorBrowser(page)
  await page.setViewportSize({ width: 1024, height: 768 })
  await page.goto(`/#/bot/${BOT_ID}`)
  await page.getByRole('tab', { name: /\u5bf9\u624b\u6218\u7ee9/ }).click()

  const pagination = page.getByRole('navigation', { name: 'Bot 对手战绩分页' })
  await pagination.getByRole('button', { name: '第 2 页', exact: true }).click()
  await expect(page.getByRole('status')).toContainText('正在加载对手战绩…')
  await page.evaluate((middleBotId) => {
    window.location.hash = `/bot/${middleBotId}`
  }, MIDDLE_BOT_ID)
  await expect.poll(() => middleProfileRequests.length).toBeGreaterThan(0)
  await page.evaluate((nextBotId) => {
    window.location.hash = `/bot/${nextBotId}`
  }, NEXT_BOT_ID)

  await expect(page.getByRole('heading', { name: '切换目标 Bot' })).toBeVisible()
  const nextOpponentsTab = page.getByRole('tab', { name: /\u5bf9\u624b\u6218绩/ })
  await expect(nextOpponentsTab).toContainText('1')
  await nextOpponentsTab.click()
  await expect(page.getByText('当前评分池计分交手 · 第 1 页 · 每页 20 个 · 共 1 个对手', { exact: true })).toBeVisible()
  await expect(
    page.getByRole('table', { name: 'Bot 对手战绩' })
      .getByRole('link', { name: '新 Bot 第一页对手', exact: true }),
  ).toBeVisible()

  await page.waitForTimeout(800)
  await expect(page.getByRole('heading', { name: '延迟的中间 Bot' })).toHaveCount(0)
  await expect(page.getByText('延迟旧对手', { exact: true })).toHaveCount(0)
  await expect(page.getByText('分页对手 21', { exact: true })).toHaveCount(0)
  expect(requests.some((url) => url.search === '?page=2&per_page=20')).toBe(true)
  expect(nextBotRequests.at(-1)?.search).toBe('?page=1&per_page=20')
  await monitor.expectClean()
})
