import { expect, test, type Browser, type BrowserContext, type Page } from '@playwright/test'

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
  { name: 'mobile', width: 390, height: 844 },
] as const

type Role = 'guest' | keyof typeof USERS
type StorageState = Awaited<ReturnType<BrowserContext['storageState']>>

const roleStates: Partial<Record<Exclude<Role, 'guest'>, StorageState>> = {}

test.beforeAll(async ({ browser, baseURL }) => {
  // Authenticate each real role once and reuse its localStorage-backed bearer
  // session at every viewport. Repeating the login form for all 12 role/viewport
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
  const rows = Array.from({ length: 12 }, (_, index) => {
    const placement = index >= 10
    const played = placement ? 8 + (index - 10) : 24 + index
    return {
      rank: placement ? null : index + 1,
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
      wins: Math.max(0, played - 7),
      draws: index % 3,
      losses: Math.max(0, 7 - (index % 3)),
      matches_played: played,
      rating_delta: index % 3 === 0 ? 12.4 : index % 3 === 1 ? -7.6 : 0,
      tier_name: index < 3 ? '专家' : '熟练',
      tier_key: index < 3 ? 'expert' : 'silver',
      is_placement: placement,
      placement_required: 10,
      placement_remaining: placement ? 10 - played : 0,
      last_match_id: index % 4 === 0 ? null : `${gameId}-validated-match-${index}`,
      last_match_at: index % 4 === 0 ? null : `2026-08-10T12:${String(index).padStart(2, '0')}:00`,
    }
  })
  return {
    leaderboard: rows,
    game_id: gameId,
    placement_required: 10,
    summary: {
      total: 12,
      ranked: 10,
      placement: 2,
      last_rated_at: '2026-08-10T12:30:00',
    },
    page: 1,
    per_page: 50,
    total: 12,
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

for (const viewport of VIEWPORTS) {
  test(`leaderboard stays dense, sticky and role-neutral (${viewport.name})`, async ({ browser, baseURL }) => {
    test.setTimeout(120_000)
    for (const role of ['guest', 'user', 'organizer', 'admin'] as const) {
      const { context, page } = await openAs(browser, baseURL!, role, viewport)
      const monitor = monitorBrowser(page)
      const leaderboardRequests = new Map<string, number>()
      const tierRequests = new Map<string, number>()

      await page.route('**/api/leaderboard?**', async (route) => {
        const url = new URL(route.request().url())
        const gameId = url.searchParams.get('game_id') || ''
        expect(['holdem', 'gomoku', 'pencil']).toContain(gameId)
        expect(url.searchParams.has('game_id')).toBe(true)
        leaderboardRequests.set(gameId, (leaderboardRequests.get(gameId) ?? 0) + 1)
        // Keep the second game's response in flight long enough to prove that
        // switching tabs clears the first game's summary instead of relabeling it.
        if (gameId === 'gomoku') await page.waitForTimeout(200)
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify(mockedLeaderboard(gameId)),
        })
      })
      await page.route('**/api/tiers?**', async (route) => {
        const gameId = new URL(route.request().url()).searchParams.get('game_id') || ''
        tierRequests.set(gameId, (tierRequests.get(gameId) ?? 0) + 1)
        await route.continue()
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
      await expect(main).toContainText('每款游戏独立使用 Glicko-2 评级')
      await expect(main).toContainText('Bot 总数')
      await expect(main).toContainText('正式榜')
      await expect(main).toContainText('定级中')
      await expect(main).toContainText('最近更新')
      const totalValue = main.getByText('Bot 总数', { exact: true }).locator('..').locator('dd')
      await expect(totalValue).toHaveText('12')
      const holdemTab = page.getByRole('tab', { name: '德州扑克', exact: true })
      const gomokuTab = page.getByRole('tab', { name: '五子棋', exact: true })
      await expect(holdemTab).toHaveAttribute('aria-selected', 'true')
      await expect(gomokuTab).toHaveAttribute('aria-selected', 'false')
      const activeLayout = viewport.name === 'mobile'
        ? page.getByTestId('leaderboard-mobile')
        : page.getByTestId('leaderboard-desktop')
      await expect(activeLayout.getByText('定级中（暂无正式名次）', { exact: false })).toBeVisible()
      await expect(main).not.toContainText('elf/linux-amd64')
      await expect(main.getByRole('columnheader', { name: '平台', exact: true })).toHaveCount(0)
      await expect(main.getByRole('columnheader', { name: '游戏', exact: true })).toHaveCount(0)
      await assertNoHorizontalOverflow(page, `${role}/${viewport.name}/initial`)

      if (viewport.name === 'mobile') {
        await expect(page.getByTestId('leaderboard-mobile')).toBeVisible()
        await expect(page.getByTestId('leaderboard-desktop')).toBeHidden()
      } else {
        await expect(page.getByTestId('leaderboard-desktop')).toBeVisible()
        await expect(page.getByTestId('leaderboard-mobile')).toBeHidden()
        await expect(main.getByRole('columnheader', { name: 'Bot / 所有者', exact: true })).toBeVisible()
        await expect(main.getByRole('columnheader', { name: '最近对局', exact: true })).toBeVisible()
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
      await assertNoHorizontalOverflow(page, `${role}/${viewport.name}/scrolled`)

      if (viewport.name !== 'mobile') {
        const headerBox = await page.getByRole('columnheader', { name: '名次', exact: true }).boundingBox()
        expect(headerBox).not.toBeNull()
        expect(headerBox!.y).toBeGreaterThanOrEqual(tabsBox!.y + tabsBox!.height - 2)
        expect(headerBox!.y).toBeLessThan(viewport.height)
      }

      const gomokuResponse = page.waitForResponse((response) => {
        const url = new URL(response.url())
        return url.pathname === '/api/leaderboard' && url.searchParams.get('game_id') === 'gomoku'
      })
      // Click while the list is scrolled: the sticky tabs must remain operable.
      await gomokuTab.click()
      await expect(totalValue).toHaveText('0')
      await expect(activeLayout).toBeHidden()
      await gomokuResponse
      await expect(gomokuTab).toHaveAttribute('aria-selected', 'true')
      await expect(holdemTab).toHaveAttribute('aria-selected', 'false')
      await expect(totalValue).toHaveText('12')
      await expect(activeLayout.getByText('GOMOKU Bot 2', { exact: true })).toBeVisible()
      await monitor.settle()

      expect(leaderboardRequests.get('holdem')).toBeGreaterThanOrEqual(1)
      expect(leaderboardRequests.get('gomoku')).toBeGreaterThanOrEqual(1)
      expect(tierRequests.get('holdem'), `${role} holdem tiers should be singleflight`).toBe(1)
      expect(tierRequests.get('gomoku'), `${role} gomoku tiers should be singleflight`).toBe(1)
      await monitor.expectClean()
      await context.close()
    }
  })
}
