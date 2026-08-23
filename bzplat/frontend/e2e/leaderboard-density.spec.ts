import { expect, test, type Browser, type BrowserContext, type Locator, type Page } from '@playwright/test'

import { loginThroughUi, monitorBrowser } from './helpers'

const USERS = {
  user: process.env.BZ_E2E_USER || 'tester1',
  organizer: process.env.BZ_E2E_ORGANIZER || 'qa_organizer',
  admin: process.env.BZ_E2E_ADMIN || 'qa_admin',
} as const

const EXPECTED_ROLES = {
  user: 'user',
  organizer: 'organizer',
  admin: 'admin',
} as const

const VIEWPORTS = [
  { name: 'desktop', width: 1440, height: 900 },
  { name: 'laptop', width: 1024, height: 768 },
  { name: 'tablet', width: 768, height: 900 },
  { name: 'mobile', width: 390, height: 844 },
] as const

type Role = 'guest' | keyof typeof USERS
type StorageState = Awaited<ReturnType<BrowserContext['storageState']>>

const roleStates: Partial<Record<Exclude<Role, 'guest'>, StorageState>> = {}

test.beforeAll(async ({ browser, baseURL }) => {
  // Authenticate each real role once and reuse its localStorage-backed bearer
  // session at every viewport. Repeating the login form for every role/viewport
  // combinations would test the login rate limiter instead of the leaderboard.
  for (const role of Object.keys(USERS) as Array<keyof typeof USERS>) {
    const context = await browser.newContext({ baseURL })
    const page = await context.newPage()
    await loginThroughUi(page, USERS[role])
    roleStates[role] = await context.storageState()
    await context.close()
  }
})

function mockedLeaderboard(gameId: string) {
  const rows = Array.from({ length: 25 }, (_, index) => {
    const eligible = index < 22
    const played = eligible ? 24 + index : 8 + (index - 22)
    const rank = eligible ? index + 1 : null
    return {
      rank,
      rank_total: 22,
      percentile: rank == null ? null : Number((100 * (22 - rank) / 21).toFixed(2)),
      bot_id: 80_000 + index,
      bot_name: `${gameId}_bot_${index + 1}`,
      bot_display: index === 0
        ? `超长排行榜 Bot 名称-${'长文本'.repeat(24)}`
        : `${gameId.toUpperCase()} Bot ${index + 1}`,
      owner_name: index === 0
        ? `owner_${'very_long_account_'.repeat(8)}`
        : `owner_${index + 1}`,
      rating: 2100 - index * 37,
      rd: 62 + index,
      confidence_low: 2100 - index * 37 - 1.96 * (62 + index),
      confidence_high: 2100 - index * 37 + 1.96 * (62 + index),
      wins: Math.max(0, played - 7),
      draws: index % 3,
      losses: Math.max(0, 7 - (index % 3)),
      rated_matches: played,
      unique_opponents: Math.min(played, 7 + index),
      rating_delta: index % 3 === 0 ? 12.4 : index % 3 === 1 ? -7.6 : 0,
      recent_delta_30d: index % 4 === 0 ? null : 18.6 - index,
      ranking_min_matches: 10,
      ranking_progress: Math.min(1, played / 10),
      ranking_eligible: eligible,
      last_match_id: index % 4 === 0 ? null : `${gameId}-validated-match-${index}`,
      last_match_at: index % 4 === 0 ? null : `2026-08-10T12:${String(index).padStart(2, '0')}:00`,
    }
  })
  return {
    leaderboard: rows,
    game_id: gameId,
    ranking_min_matches: 10,
    summary: {
      total: 25,
      eligible: 22,
      sample: 3,
      last_rated_at: '2026-08-10T12:30:00',
    },
    page: 1,
    per_page: 50,
    total: 25,
  }
}

function mockedExecutionQueue() {
  const job = (
    id: number,
    source: 'manual' | 'human' | 'contest' | 'auto',
    status: 'queued' | 'running',
    gameId: string,
  ) => ({
    public_id: `public-${id}`,
    source,
    status,
    game_id: gameId,
    match_type: source,
    match_id: status === 'running' ? `${gameId}-${source}-active-match` : null,
    sandbox_units: source === 'human' ? 1 : 2,
    rated: source === 'auto',
    rating_reason: source === 'human' ? 'human' : source === 'contest' ? 'contest' : 'eligible',
    retryable: false,
    cancel_requested: false,
    reason: '',
    created_at: '2026-08-10T12:30:00',
  })
  return {
    dispatcher: {
      state: 'running',
      accepting: true,
      auto_enabled: true,
      pause_reason: '',
      retry_at: null,
    },
    capacity: {
      match_slots: { used: 2, capacity: 2 },
      sandbox_units: { used: 3, capacity: 4 },
      running_matches: 2,
    },
    active: [
      job(1, 'human', 'running', 'holdem'),
      job(5, 'contest', 'running', 'pencil'),
    ],
    queued: [
      job(2, 'auto', 'queued', 'gomoku'),
      job(3, 'contest', 'queued', 'pencil'),
      job(4, 'manual', 'queued', 'holdem'),
    ],
    queued_count: 3,
  }
}

async function openAs(browser: Browser, baseURL: string, role: Role, viewport: { width: number; height: number }) {
  const context = await browser.newContext({
    baseURL,
    viewport,
    storageState: role === 'guest' ? undefined : roleStates[role],
  })
  const page = await context.newPage()
  return { context, page }
}

async function assertNoHorizontalOverflow(page: Page, label: string) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - window.innerWidth,
  )
  expect(overflow, `${label} overflows by ${overflow}px`).toBeLessThanOrEqual(1)
}

function verticallyOverlaps(
  first: { y: number; height: number },
  second: { y: number; height: number },
) {
  return Math.max(first.y, second.y) < Math.min(first.y + first.height, second.y + second.height)
}

async function assertOwnsCenterHit(locator: Locator, label: string) {
  await locator.scrollIntoViewIfNeeded()
  const ownsCenterHit = await locator.evaluate((element) => {
    const rect = element.getBoundingClientRect()
    const x = Math.max(0, Math.min(window.innerWidth - 1, rect.left + rect.width / 2))
    const y = Math.max(0, Math.min(window.innerHeight - 1, rect.top + rect.height / 2))
    const hit = document.elementFromPoint(x, y)
    return hit != null && element.contains(hit)
  })
  expect(ownsCenterHit, `${label} must not be covered at its center point`).toBe(true)
}

async function assertTopRankingRowsUnobscured(desktop: Locator, label: string) {
  const headerBox = await desktop.getByRole('columnheader', { name: '名次', exact: true }).boundingBox()
  expect(headerBox, `${label} table header must have layout`).not.toBeNull()

  const rows = [1, 2, 3].map((rank) => ({
    rank,
    cell: desktop.getByRole('cell', { name: `#${rank}`, exact: true }),
    locator: desktop.getByRole('cell', { name: `#${rank}`, exact: true }).locator('xpath=..'),
  }))

  for (const row of rows) {
    await expect(row.cell, `${label} rank #${row.rank} must have one rank cell`).toHaveCount(1)
    await expect(row.locator, `${label} rank #${row.rank} must have one row`).toHaveCount(1)
    const rowBox = await row.locator.boundingBox()
    expect(rowBox, `${label} rank #${row.rank} must have layout`).not.toBeNull()
    expect(
      verticallyOverlaps(headerBox!, rowBox!),
      `${label} table header must not overlap rank #${row.rank}`,
    ).toBe(false)
  }

  for (const row of rows) {
    await assertOwnsCenterHit(row.locator, `${label} rank #${row.rank}`)
  }
}

for (const viewport of VIEWPORTS) {
  test(`leaderboard stays dense, unobscured and role-neutral (${viewport.name})`, async ({ browser, baseURL }) => {
    test.setTimeout(120_000)
    for (const role of ['guest', 'user', 'organizer', 'admin'] as const) {
      const { context, page } = await openAs(browser, baseURL!, role, viewport)
      const monitor = monitorBrowser(page)
      const leaderboardRequests = new Map<string, number>()
      let queueRequests = 0

      await page.route('**/api/leaderboard?**', async (route) => {
        const url = new URL(route.request().url())
        const gameId = url.searchParams.get('game_id') || ''
        expect(['holdem', 'gomoku', 'pencil']).toContain(gameId)
        expect(url.searchParams.has('game_id')).toBe(true)
        leaderboardRequests.set(gameId, (leaderboardRequests.get(gameId) ?? 0) + 1)
        // Keep the second game's response in flight long enough to prove that
        // switching tabs clears the first game's rows instead of relabeling them.
        if (gameId === 'gomoku') await page.waitForTimeout(200)
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify(mockedLeaderboard(gameId)),
        })
      })
      await page.route('**/api/execution-queue', async (route) => {
        queueRequests += 1
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify(mockedExecutionQueue()),
        })
      })
      const authProbe = page.waitForResponse((response) => {
        const url = new URL(response.url())
        return response.request().method() === 'GET' && url.pathname === '/api/auth/me'
      })
      await page.goto('/#/leaderboard')
      const authResponse = await authProbe
      if (role === 'guest') {
        expect(authResponse.status(), 'guest must remain anonymous').toBe(401)
        expect(await page.evaluate(() => ({
          token: localStorage.getItem('bzplat_token'),
          user: localStorage.getItem('bzplat_user'),
        }))).toEqual({ token: null, user: null })
      } else {
        expect(authResponse.status(), `${role} /api/auth/me`).toBe(200)
        const authBody = await authResponse.json() as {
          user?: { username?: string; role?: string }
        }
        expect(authBody.user?.username).toBe(USERS[role])
        expect(authBody.user?.role).toBe(EXPECTED_ROLES[role])
      }
      const main = page.locator('main')
      await expect(main.getByRole('heading', { name: '排行榜', exact: true })).toBeVisible()
      await expect(main).toContainText('每款游戏独立使用 Glicko-2 数值评分')
      await expect(main).toContainText('Rating 由 Glicko-2 根据对手实力与不确定度更新，不等于胜场累计')
      await expect(main).toContainText('赛事积分不进入平台 Rating')
      await expect(main.locator('[data-slot="summary-strip"]')).toHaveCount(0)
      await expect(main.getByText('Bot 总数', { exact: true })).toHaveCount(0)
      await expect(main.getByText('最近更新', { exact: true })).toHaveCount(0)
      const queuePanel = page.getByTestId('execution-queue-panel')
      await expect(queuePanel).toBeVisible()
      if (viewport.name === 'mobile') {
        await queuePanel.locator('summary').click()
      }
      await expect(queuePanel).toContainText('正在执行')
      await expect(queuePanel).toContainText('等待执行')
      await expect(queuePanel).toContainText('全站当前对局槽上限 2 场')
      await expect(queuePanel.getByRole('link', { name: '进入观赛' })).toHaveCount(2)
      await expect(queuePanel.getByRole('link', { name: '进入观赛' }).first()).toHaveAttribute(
        'href',
        '#/match/holdem-human-active-match',
      )
      await expect(queuePanel).toContainText('人机对战，不计平台排行榜')
      const holdemTab = page.getByRole('tab', { name: '德州扑克', exact: true })
      const gomokuTab = page.getByRole('tab', { name: '五子棋', exact: true })
      await expect(holdemTab).toHaveAttribute('aria-selected', 'true')
      await expect(gomokuTab).toHaveAttribute('aria-selected', 'false')
      const activeLayout = viewport.name === 'mobile'
        ? page.getByTestId('leaderboard-mobile')
        : page.getByTestId('leaderboard-desktop')
      await expect(activeLayout.getByText('计分样本（无公开名次）', { exact: false })).toBeVisible()
      await expect(main).not.toContainText('elf/linux-amd64')
      await expect(main.getByRole('columnheader', { name: '平台', exact: true })).toHaveCount(0)
      await expect(main.getByRole('columnheader', { name: '游戏', exact: true })).toHaveCount(0)
      await assertNoHorizontalOverflow(page, `${role}/${viewport.name}/initial`)

      if (viewport.name === 'mobile') {
        await expect(page.getByTestId('leaderboard-mobile')).toBeVisible()
        await expect(page.getByTestId('leaderboard-desktop')).toBeHidden()
        const firstItem = activeLayout.getByRole('listitem').first()
        await expect(firstItem).toContainText('超长排行榜 Bot 名称-')
        await expect(firstItem.getByText('#1', { exact: true })).toBeVisible()
        await assertOwnsCenterHit(firstItem, `${role}/${viewport.name} rank #1`)
      } else {
        await expect(page.getByTestId('leaderboard-desktop')).toBeVisible()
        await expect(page.getByTestId('leaderboard-mobile')).toBeHidden()
        // The original regression came from page-sticky being applied inside
        // DataTable's horizontal overflow viewport.  Geometry guards the user-
        // visible result; this attribute assertion also makes restoring the
        // exact offending `sticky="page"` prop fail deterministically.
        await expect(
          activeLayout.locator('[data-slot="table-header"]'),
        ).not.toHaveAttribute('data-sticky-region')
        await expect(main.getByRole('columnheader', { name: 'Bot / 所有者', exact: true })).toBeVisible()
        await expect(main.getByRole('columnheader', { name: '最近对局', exact: true })).toBeVisible()
        await assertTopRankingRowsUnobscured(activeLayout, `${role}/${viewport.name}`)
      }

      const longName = main.getByRole('link', { name: /^超长排行榜 Bot 名称-/ }).first()
      await expect(longName).toBeVisible()
      const longBox = await longName.boundingBox()
      expect(longBox).not.toBeNull()
      expect(longBox!.x + longBox!.width).toBeLessThanOrEqual(viewport.width + 1)

      await page.evaluate(() => window.scrollTo({ top: 720, behavior: 'instant' }))
      await page.waitForTimeout(100)
      const tabsBox = await page.getByTestId('leaderboard-game-tabs').boundingBox()
      expect(tabsBox).not.toBeNull()
      expect(tabsBox!.y).toBeGreaterThanOrEqual(viewport.width < 1024 ? 54 : -1)
      expect(tabsBox!.y).toBeLessThan(viewport.height)
      if (viewport.name !== 'mobile') {
        await expect(
          activeLayout.locator('[data-slot="table-header"]'),
        ).not.toHaveAttribute('data-sticky-region')
      }
      await assertNoHorizontalOverflow(page, `${role}/${viewport.name}/scrolled`)

      const gomokuResponse = page.waitForResponse((response) => {
        const url = new URL(response.url())
        return url.pathname === '/api/leaderboard' && url.searchParams.get('game_id') === 'gomoku'
      })
      // Click while the list is scrolled: the sticky tabs must remain operable.
      await gomokuTab.click()
      await expect(activeLayout).toBeHidden()
      await gomokuResponse
      await expect(gomokuTab).toHaveAttribute('aria-selected', 'true')
      await expect(holdemTab).toHaveAttribute('aria-selected', 'false')
      await expect(activeLayout.getByText('GOMOKU Bot 2', { exact: true })).toBeVisible()
      await expect(queuePanel).toContainText('德州扑克')
      await expect(queuePanel).toContainText('五子棋')
      await assertNoHorizontalOverflow(page, `${role}/${viewport.name}/queue-switched`)
      await monitor.settle()

      expect(leaderboardRequests.get('holdem')).toBeGreaterThanOrEqual(1)
      expect(leaderboardRequests.get('gomoku')).toBeGreaterThanOrEqual(1)
      expect(queueRequests).toBeGreaterThanOrEqual(1)
      await monitor.expectClean()
      await context.close()
    }
  })
}
